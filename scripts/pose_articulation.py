"""Pose an articulation: joint values in, transforms out.

An asset arrives in its rest pose, which is usually a configuration it is never actually
in. Everything downstream wants a different one: a solver wants an initial state to fall
from, Newton and MuJoCo read the pose at parse time and never look at the stage again, and
a training run wants a thousand randomised ones.

It reads the articulation out of UsdPhysics rather than out of a .urdf, so a robot arm is
only the obvious case. A drawer is a prismatic joint and a door is a revolute one -- the
same two kinds, so a kitchen opens exactly the way an elbow bends, and anything authored
with UsdPhysics joints is posable whether it came from URDF or was built by hand.

Two things are written per posed joint:

- the link transforms, because the robot has to actually move; and
- `drive:<axis>:physics:targetPosition`, because that is where the *intent* lives. The
  transforms say where the robot is; the drive target says where it is being asked to go,
  which is what an engine with actuators reads and what survives into Newton.
"""
from __future__ import annotations

import math

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import chunk
from usd_chunks import chunk_notice
from usd_chunks.chunk_flags import ChunkFlags

import rbd_solver

#: Params the chunk always has. Everything else on it is a lofted joint.
_FIXED = ("prim_path", "pose", "write_targets")


def _roots(node):
    """The articulation under the chunk's prim path, or an empty list."""
    scene = node.get_scene()
    stage = scene.get_stage() if scene else None
    param = node.get_param("prim_path")
    path = (param.value if param else "") or ""
    if stage is None or not path:
        return None, []
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        return stage, []
    return stage, rbd_solver.read_articulations(prim)


def _posable(roots):
    """Every link whose joint is a degree of freedom, parents before children.

    Order matters: posing a shoulder moves the elbow with it, so the shoulder has to be
    applied while the elbow still knows where it is relative to it.
    """
    found = []

    def walk(link):
        for child in link.children:
            joint = getattr(child, "joint", None)
            if joint is not None and joint.kind in ("revolute", "prismatic"):
                found.append(child)
            walk(child)

    for root in roots:
        walk(root)
    return found


def _parse_pose(text):
    """{joint name: value} from the pose spec, one `name value` per line.

    Text, and authoritative, for the reason the wedge's ranges are: a param lofted at
    runtime is gone when the file is reopened. The joints themselves can be recovered from
    the stage, but the angles cannot be recovered from anywhere -- so they live here, and
    the sliders are a view of them.
    """
    values = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) < 2:
            print(f"[Pose] cannot read {line!r}")
            continue
        try:
            values[parts[0]] = float(parts[1])
        except ValueError:
            print(f"[Pose] cannot read angle in {line!r}")
    return values


def _format_pose(values):
    return "\n".join(f"{name} {value:g}" for name, value in values.items())


def _lofted(node):
    """The per-joint params, which is everything that is not fixed and not a group."""
    return [p for p in node.get_my_params()
            if not isinstance(p, chunk.Group) and p.name not in _FIXED]


def _sync_joints(node):
    """Rebuild one slider per joint so the params match the articulation on the stage.

    Runs on load as well as on edit. The joint list is recovered from USD, its limits with
    it, so the sliders cannot drift out of step with the robot they pose.
    """
    _, roots = _roots(node)
    links = _posable(roots)
    wanted = {}
    for link in links:
        joint = link.joint
        lower, upper = joint.lower, joint.upper
        if joint.kind == "revolute":
            # USD authors rotational limits in degrees, and read_articulations converts
            # them to radians for MuJoCo. The slider is in degrees, which is what anyone
            # posing a robot by hand wants to type.
            lower = math.degrees(lower) if lower is not None else -180.0
            upper = math.degrees(upper) if upper is not None else 180.0
        else:
            lower = -1.0 if lower is None else lower
            upper = 1.0 if upper is None else upper
        wanted[link.name] = (joint.kind, float(lower), float(upper))

    have = {p.name: p for p in _lofted(node)}
    for name, param in have.items():
        if name not in wanted:
            node.remove_param_or_group(param)

    pose = _parse_pose(node.get_param("pose").value if node.get_param("pose") else "")
    flags = ChunkFlags.ParamFlags.LABELED
    for name, (kind, lower, upper) in wanted.items():
        if name in have:
            continue
        value = pose.get(name, 0.0)
        unit = "degrees" if kind == "revolute" else "stage units"
        node.add_param(chunk.Slider(
            name, name, default_value=min(max(value, lower), upper),
            min_value=lower, max_value=upper, flags=flags,
            tooltip=f"{kind} joint, {lower:g} to {upper:g} {unit}.",
        ))


def _harvest(node):
    """Slider values back into the spec, so what was posed survives the file."""
    values = {p.name: float(p.value or 0.0) for p in _lofted(node)}
    if not values:
        return {}
    spec = node.get_param("pose")
    if spec is not None:
        text = _format_pose(values)
        if spec.value != text:
            spec.value = text
    return values


def _flat(prim, root_path):
    """Is this link a direct child of the articulation root?

    Links the solver simulates are authored flat, and those are the ones that carry a pose
    of their own. A nested link is welded to its parent and follows it for free -- posing
    it directly would apply its parent's rotation to it twice.
    """
    return prim.GetPath().GetParentPath() == root_path


def _descendants(link):
    out = [link]
    for child in getattr(link, "children", []):
        out.extend(_descendants(child))
    return out


def _set_local(prim, matrix):
    """Author the whole local transform as one matrix op.

    Deliberately not decomposed back into the translate and orient ops the import wrote:
    the local transform is the ops multiplied in their authored order, and rebuilding that
    correctly means getting an order convention right for a second time in one file. A
    single xformOp:transform in a stronger layer has no order to get wrong.
    """
    xform = UsdGeom.Xformable(prim)
    op = None
    for existing in xform.GetOrderedXformOps():
        if existing.GetOpType() == UsdGeom.XformOp.TypeTransform:
            op = existing
            break
    if op is None:
        op = xform.AddTransformOp()
    op.Set(matrix)
    xform.SetXformOpOrder([op])


def _write_target(joint_prim, kind, value):
    """The pose as drive intent, not just as geometry."""
    axis = "angular" if kind == "revolute" else "linear"
    drive = UsdPhysics.DriveAPI.Apply(joint_prim, axis)
    drive.CreateTargetPositionAttr(float(value))


def _joint_prim(link):
    """A link's own joint prim -- the import authors it as a child of the child body."""
    for child in link.prim.GetChildren():
        if "Joint" in str(child.GetTypeName()):
            return child
    return None


def apply_pose(stage, roots, values, root_path, write_targets=True):
    """Move every posed joint, and everything hanging off it.

    Links are authored FLAT, so a rotation does not propagate down the hierarchy the way it
    would in a nested rig -- the elbow is the shoulder's sibling, not its child. Every
    descendant therefore has to be moved explicitly, which is the price of the flat
    authoring that keeps the mechanism from coming apart under the solver.

    Every transform is read ONCE, accumulated in memory, and written at the end. Reading
    each link back off the stage as the walk proceeds looks equivalent and is not: what a
    chunk authors is only visible to a read if its layer happens to win, and this one is
    overriding transforms authored upstream. Where it loses, every joint reads the rest
    pose, silently discards what the joint before it did, and the arm ends up holding only
    the last rotation -- which looks like a posing bug and is a composition one.
    """
    local = {}
    for link in _posable(roots):
        for reachable in _descendants(link):
            if reachable.prim not in local:
                local[reachable.prim] = UsdGeom.Xformable(
                    reachable.prim).GetLocalTransformation()

    posed = 0
    touched = set()
    for link in _posable(roots):
        value = values.get(link.name)
        if value is None or abs(value) < 1e-12:
            continue
        joint = link.joint

        here = local.get(link.prim)
        if here is None:
            continue
        # The joint's axis and position are expressed in the child body's own frame, so the
        # child's transform is what takes them into the space the links are posed in.
        axis = here.TransformDir(Gf.Vec3d(*joint.axis))
        if axis.GetLength() < 1e-9:
            continue
        axis = axis.GetNormalized()
        pivot = here.Transform(Gf.Vec3d(*joint.pos))

        if joint.kind == "revolute":
            step = (Gf.Matrix4d().SetTranslate(-pivot)
                    * Gf.Matrix4d().SetRotate(Gf.Rotation(axis, value))
                    * Gf.Matrix4d().SetTranslate(pivot))
        else:
            step = Gf.Matrix4d().SetTranslate(axis * value)

        for moved in _descendants(link):
            if not _flat(moved.prim, root_path) or moved.prim not in local:
                continue
            local[moved.prim] = local[moved.prim] * step
            touched.add(moved.prim)

        if write_targets and (prim := _joint_prim(link)) is not None:
            _write_target(prim, joint.kind, value)
        posed += 1

    for prim in touched:
        _set_local(prim, local[prim])
    return posed


def execute(node):
    stage, roots = _roots(node)
    if stage is None or not roots:
        return
    values = _harvest(node)
    if not values:
        return

    param = node.get_param("prim_path")
    root_path = Sdf.Path((param.value if param else "") or "/")
    targets = node.get_param("write_targets")
    layer = node.get_edit_context()

    # Cleared first, so a pose is absolute rather than cumulative. apply_pose reads the
    # composed transform, which includes whatever this layer wrote last cook -- so without
    # this each execute rotates again from where the last one stopped, and a slider dragged
    # twice puts the joint somewhere neither value asked for. A wedge sweeping the same
    # slider compounds it once per variant.
    layer.Clear()

    with Usd.EditContext(stage, layer):
        posed = apply_pose(stage, roots, values, root_path,
                           write_targets=bool(targets.value) if targets else True)
    print(f"[Pose] posed {posed} joint(s) under {root_path}")


#: Nodes currently inside execute(), by id. _harvest writes the pose spec, which is itself
#: a param change, so without this the first slider move recurses until the stack runs out.
_BUSY = set()


def _param_changed(owner, notice, _sender):
    if id(owner) in _BUSY:
        return
    changed = getattr(notice, "data", None)
    if changed is not None and getattr(changed, "name", None) == "prim_path":
        _sync_joints(owner)
    _BUSY.add(id(owner))
    try:
        execute(owner)
    finally:
        _BUSY.discard(id(owner))


def _chunk_loaded(owner, _notice, _sender):
    # The joints are recoverable from the stage and the angles from the spec; the sliders
    # themselves are not, so they are rebuilt from both.
    node = owner
    _sync_joints(node)
    execute(node)


def main():
    chunk_api = chunk.Chunk("Pose Articulation")
    chunk_api.add_params([
        chunk.LineEdit(
            "prim_path", "Articulation",
            flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The articulation root -- a robot, a cabinet, anything carrying "
                    "UsdPhysics joints. One slider is lofted per revolute or prismatic "
                    "joint found beneath it.",
        ),
        chunk.TextEdit(
            "pose", "Pose",
            tooltip="One `joint angle` per line, degrees for revolute joints. This is the "
                    "authoritative record -- the sliders are a view of it, because params "
                    "lofted at runtime do not survive a save and these angles could not be "
                    "recovered from anywhere else.",
        ),
        chunk.Checkbox(
            "write_targets", "Write Drive Targets", default_value=True,
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Also author drive:<axis>:physics:targetPosition on each joint. The "
                    "transforms say where the robot is; this says where it is being asked "
                    "to go, which is what an engine with actuators reads.",
        ),
    ])
    chunk_api.add_callback(_param_changed,
                           notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_CHANGED)
    chunk_api.add_callback(_chunk_loaded,
                           notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Robotics",
        description="""
        Poses an articulation: one slider per joint, read out of UsdPhysics rather than out
        of a .urdf, so a robot arm is only the obvious case. A drawer is a prismatic joint
        and a door is a revolute one, so a kitchen opens the same way an elbow bends.

        An asset arrives in its rest pose, which is a configuration it is mostly never in.
        An initial pose is what a solver falls from, what Newton and MuJoCo read at parse
        time and never revisit, and -- driven by a Wedge -- what turns one robot into the
        thousand randomised starting states a training run wants.

        Writes the link transforms and drive:<axis>:physics:targetPosition. The transforms
        are where the robot is; the target is where it is being asked to go, which is the
        half an engine with actuators reads.

        Links are authored flat rather than nested, so a rotation cannot propagate down a
        hierarchy: every descendant of a posed joint is moved explicitly.

        Wire it DOWNSTREAM of whatever authored the rest pose. Posing is an override, and an
        override only wins if its layer does: chunk layers are ranked by exec position, so
        an unwired pose chunk authors correct transforms into a weaker layer and they are
        composed away without a word.
        """,
    )
