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

import logging
import math

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import chunk
from usd_chunks import chunk_gizmo
from usd_chunks import chunk_notice
from usd_chunks import chunk_registry
from usd_chunks import chunk_utils
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import get_chunk_prim_path

logger = logging.getLogger(__name__)


def _rbd_solver():
    """The rbd_solver chunk, found by chunk name so it resolves wherever the two live.

    A bare `import rbd_solver` worked only while every chunk sat in one folder on sys.path,
    and `import scripts.dynamics.rbd_solver` only inside this repo.
    """
    mod = chunk_registry.ChunkRegistry().get_chunk_module("rbd_solver")
    if mod.module is None:
        mod._exec_module()
    return mod.module


#: Attributes on the chunk prim that are params of the chunk, not joints.
_NOT_A_JOINT = ("inputs:chunk", "inputs:prim_path", "inputs:write_targets",
                "inputs:pose")

#: Params the chunk always has. Everything else on it is a lofted joint.
_FIXED = ("prim_path", "write_targets")


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
    return stage, _rbd_solver().read_articulations(prim)


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


def _saved_values(node):
    """Slider values recovered from the chunk's own prim.

    The sliders are created at runtime, so they do not exist yet when the loader restores
    param values -- but the values were written to USD all the same, as inputs:<joint> on
    the chunk prim. Reading them back from there is what lets a pose survive a save without
    keeping a second copy of the same numbers in a text param, which was only ever able to
    disagree with the first.
    """
    scene = node.get_scene()
    stage = scene.get_stage() if scene else None
    if stage is None:
        return {}
    # A chunk exists as a Python object before it exists as a prim, and this runs from a
    # notice callback -- so it can be asked for its prim path before there is one. Raising
    # here does not surface as an error message, it surfaces as a node the UI cannot build
    # a body for, which is a much worse way to find out.
    try:
        path = get_chunk_prim_path(node)
    except Exception:
        return {}
    if not path:
        return {}
    prim = stage.GetPrimAtPath(Sdf.Path(path))
    if not prim or not prim.IsValid():
        return {}
    values = {}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith("inputs:") or name in _NOT_A_JOINT:
            continue
        value = attr.Get()
        if isinstance(value, float):
            values[name[len("inputs:"):]] = float(value)
    return values


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

    pose = _saved_values(node)
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
    """The angle each slider is asking for."""
    return {p.name: float(p.value or 0.0) for p in _lofted(node)}


def _outermost(prims):
    """The prims to actually move: those with no ancestor in the same set.

    A link whose ancestor is also being moved inherits that move through the hierarchy, so
    moving it as well applies the rotation to it twice. A link with no moved ancestor gets
    nothing for free and has to be moved itself.

    This used to ask a narrower question -- "is this a direct child of the articulation
    root" -- which is true of everything urdf_import authors, because it deliberately
    authors moving links flat. It is not true of a scene: a room full of appliances puts
    each one under its own Xform, so every posable link is a grandchild of the root and the
    old test rejected all of them. 48 joints, sliders for every one, and nothing moved.
    """
    paths = {prim.GetPath() for prim in prims}
    keep = []
    for prim in prims:
        path = prim.GetPath().GetParentPath()
        while not path.IsAbsoluteRootPath():
            if path in paths:
                break
            path = path.GetParentPath()
        else:
            keep.append(prim)
    return keep


def _descendants(link):
    out = [link]
    for child in getattr(link, "children", []):
        out.extend(_descendants(child))
    return out


def _upstream_of(node, prim):
    """The composed local transform from everything the graph places before this chunk's
    own op on `prim` -- deliberately NOT GetLocalTransformation(), which reads EVERYTHING
    currently on the prim, including a chunk wired DOWNSTREAM of this one (an RBD Solver
    bake, say) if that chunk's layer currently outranks this one's, which it legitimately
    does, being closer to the view.

    apply_pose used to treat GetLocalTransformation() as the rest pose to build its own
    absolute answer from. Once something downstream also writes to the same prim, that read
    is contaminated: a downstream bake gets folded in as if it were part of the rest pose,
    and stays folded in even after this chunk's own next re-cook discards its own stale
    contribution, because the downstream one is untouched. Live-reported: posing a joint
    after running the RBD Solver corrupted the whole arm's placement, worse with every
    further pose change, fixed only by forcing a full recook.

    Reads only the ops chunk_gizmo.xform_op_order places before this chunk's own -- the
    same graph-derived order every other transform-authoring chunk in this codebase already
    uses to decide who's upstream of whom, applied here to ourselves instead of a caller.
    """
    my_op = chunk_utils.xform_op_name(node)
    order = chunk_gizmo.xform_op_order(node, prim, my_op)
    try:
        my_index = order.index(my_op)
    except ValueError:
        my_index = len(order)
    matrix = Gf.Matrix4d(1.0)
    for op_name in order[:my_index]:
        attr = prim.GetAttribute(op_name)
        if not attr or not attr.IsValid():
            continue
        matrix = UsdGeom.XformOp(attr).GetOpTransform(Usd.TimeCode.Default()) * matrix
    return matrix


def _set_local(prim, matrix, upstream, node):
    """Author this chunk's SHARE of the local transform, not the absolute placement.

    `matrix` is where the prim's local transform needs to end up; `upstream` is what the
    ops before this chunk's own already contribute (see _upstream_of). Writing `matrix`
    itself as this chunk's own op applies upstream's contribution a second time the moment
    it composes rather than replaces it -- the same mistake already fixed for the RBD
    Solver's own bake (see chunk_gizmo.xform_op_order's docstring: "the shared op is what
    let the solver evict everyone else"). Dividing upstream back out, and owning a uniquely
    named op via chunk_utils.xform_op_name rather than the bare "xformOp:transform" every
    chunk used to fight over, is what lets a downstream chunk (an RBD Solver bake) coexist
    with this one instead of silently excluding it from the order or being excluded by it.
    """
    my_op = chunk_utils.xform_op_name(node)
    xform = UsdGeom.Xformable(prim)
    order = chunk_gizmo.xform_op_order(node, prim, my_op)
    xform.GetXformOpOrderAttr().Set(order)

    attr = prim.GetAttribute(my_op)
    if not attr or not attr.IsValid():
        attr = prim.CreateAttribute(my_op, Sdf.ValueTypeNames.Matrix4d, custom=False)
    attr.Set(matrix * upstream.GetInverse())


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


def apply_pose(stage, roots, values, root_path, node, write_targets=True):
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
    upstream = {}
    for link in _posable(roots):
        for reachable in _outermost([d.prim for d in _descendants(link)]):
            if reachable not in local:
                # Captured once, kept unchanged: this is what _set_local later divides back
                # out, so it has to reflect what's genuinely upstream of this chunk, not the
                # (possibly further-touched) working value in `local`.
                seed = _upstream_of(node, reachable)
                local[reachable] = seed
                upstream[reachable] = seed

    posed = 0
    touched = set()
    for link in _posable(roots):
        value = values.get(link.name)
        # Not finite is a real case, not a defensive flourish: an asset whose joint limits
        # are nan produces a nan angle, and a nan reaches a quaternion and then every
        # transform downstream without ever raising -- the same way an uninitialised
        # Gf.Rotation did.
        if value is None or not math.isfinite(value) or abs(value) < 1e-12:
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

        for moved in _outermost([d.prim for d in _descendants(link)]):
            if moved not in local:
                continue
            local[moved] = local[moved] * step
            touched.add(moved)

        if write_targets and (prim := _joint_prim(link)) is not None:
            _write_target(prim, joint.kind, value)
        posed += 1

    for prim in touched:
        _set_local(prim, local[prim], upstream[prim], node)
    return posed


def execute(node):
    # Guarded so a cook cannot start a second one. A value change already cooks the chunk,
    # so calling execute() from the callback below as well made one slider move cook twice
    # -- and in the UI a cook is a full view recook, so the whole upstream chain paid it.
    if id(node) in _BUSY:
        return
    _BUSY.add(id(node))
    try:
        _execute(node)
    finally:
        _BUSY.discard(id(node))


def _execute(node):
    stage, roots = _roots(node)
    if stage is None or not roots:
        return

    # Re-loft here rather than relying on CHUNK_LOADED having fired. The sliders are created
    # at runtime, so a chunk that arrives without them poses nothing and says nothing -- the
    # asset simply opens in its rest pose. Depending on load-notice ordering to avoid that
    # is depending on something this chunk cannot see; asking whether the sliders are there
    # is something it can.
    if _posable(roots) and not _lofted(node):
        _sync_joints(node)

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
        posed = apply_pose(stage, roots, values, root_path, node,
                           write_targets=bool(targets.value) if targets else True)
    # Only when the shape of the work changes, not once per cook. A slider drag cooks this
    # chunk several times a second, and even at debug level each log call is free in a
    # terminal but not in a panel that captures stdout into a widget -- there it is a
    # repaint per line, which is felt as the slider being laggy rather than as the chunk
    # being chatty.
    last = getattr(node, "_pose_reported", None)
    now = (posed, str(root_path))
    if now != last:
        node._pose_reported = now
        logger.debug("posed %d joint(s) under %s", posed, root_path)


#: Nodes currently inside execute(), by id, so a cook cannot re-enter itself.
_BUSY = set()


def _param_changed(owner, notice, _sender):
    """Re-loft the sliders when the target changes. Cooking is the framework's job.

    Calling execute() here as well is the obvious thing to write and it is a second cook
    of the whole chain: a value change already cooks the chunk.
    """
    if id(owner) in _BUSY:
        return
    changed = getattr(notice, "data", None)
    if changed is not None and getattr(changed, "name", None) == "prim_path":
        _sync_joints(owner)


def _chunk_loaded(owner, _notice, _sender):
    # Neither the sliders nor their values survive a load on their own: the sliders are
    # created at runtime, and the loader restores values before they exist. The joints come
    # back from the stage and the angles from the chunk prim's own inputs: attributes.
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
        category=chunk.StandardCategory.RIGS,
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
