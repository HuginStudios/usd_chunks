"""Skel Pose: rotate a few joints of a skinned character, over whatever is animating it.

A rig arrives with an animation source -- a walk cycle referenced in, a clip set stacked
by Value Clips -- and the note from layout is "arm up". This chunk lofts one rotate
handle per joint you name and writes the pose as a *sparse array edit* on the animation's
`rotations`: `edit [write q to [i]]` for the joints you touched and nothing for the 100
you did not. Because it is one default value on the chunk's own layer, it sits over the
time samples underneath without baking anything -- the legs keep walking, the arm holds.

Each handle's rotation is in the character's space (the SkelRoot's local space), about
the joint, on top of the rest pose: grab the ring at the shoulder and turn it about X, and
the arm turns about the character's X through the shoulder, whatever the walk is doing to
the torso. It replaces the walk's rotation for that joint rather than adding to it, which
is what a held pose means.

Joints are not prims, so the handles cannot anchor to them. They anchor to the SkelRoot
and the chunk reports each joint's rest position as the handle offset -- see gizmo_offset.
"""
from __future__ import annotations

import fnmatch
import logging
import re

from pxr import Gf, Sdf, Usd, UsdGeom, UsdSkel, Vt

from usd_chunks import chunk, chunk_gizmo, chunk_notice, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import get_chunk_prim_path

logger = logging.getLogger(__name__)

#: Params the chunk always has. Everything else on it is a lofted joint handle.
_FIXED = ("primpath_location", "joints")


class _Rig:
    """What the chunk needs to know about the character under `primpath_location`.

    Rest transforms are in *skeleton* space, in the skeleton's joint order. The animation
    has its own joint order, and that is the index space of the `rotations` array the edits
    write into -- so both orders are kept and joints are matched by name.
    """

    def __init__(self, stage, root_prim, skel_query, anim_query):
        self.stage = stage
        self.root_prim = root_prim
        self.skel_query = skel_query
        self.anim_prim = anim_query.GetPrim()
        self.anim_index = {name: i for i, name in enumerate(anim_query.GetJointOrder())}
        skel_joints = list(skel_query.GetJointOrder())
        rest = skel_query.ComputeJointSkelTransforms(Usd.TimeCode.Default(), atRest=True)
        self.rest = dict(zip(skel_joints, rest))
        self.parent = {}
        topology = skel_query.GetTopology()
        for i, name in enumerate(skel_joints):
            parent = topology.GetParent(i)
            self.parent[name] = skel_joints[parent] if parent >= 0 else None

    def posable(self):
        """Joints the animation can drive and the skeleton knows, in animation order."""
        return [name for name in self.anim_index if name in self.rest]


def _rig(node) -> _Rig | None:
    stage = node.get_stage()
    param = node.get_param("primpath_location")
    path = (param.value if param else "") or ""
    if stage is None or not path:
        return None
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        return None
    root = UsdSkel.Root.Find(prim) if not prim.IsA(UsdSkel.Root) else UsdSkel.Root(prim)
    if not root:
        return None
    cache = UsdSkel.Cache()
    cache.Populate(root, Usd.PrimDefaultPredicate)
    for skeleton in _skeletons(root.GetPrim()):
        skel_query = cache.GetSkelQuery(skeleton)
        anim_query = skel_query.GetAnimQuery() if skel_query else None
        if anim_query:
            return _Rig(stage, root.GetPrim(), skel_query, anim_query)
    return None


def _skeletons(root_prim):
    """Skeletons the SkelRoot binds or contains, bound one first.

    Not ComputeSkelBindings: that pairs skeletons with the *meshes* they skin, so a rig
    with no skinned geometry yet -- or with skinning behind an unloaded payload -- has no
    bindings at all, while its skeleton and animation are right there.
    """
    found = []
    bound = UsdSkel.BindingAPI(root_prim).GetSkeleton()
    if bound:
        found.append(bound)
    for prim in Usd.PrimRange(root_prim):
        if prim.IsA(UsdSkel.Skeleton) and prim not in [s.GetPrim() for s in found]:
            found.append(UsdSkel.Skeleton(prim))
    return found


def _leaf(joint: str) -> str:
    return joint.rsplit("/", 1)[-1]


def _param_name(joint: str, taken: set) -> str:
    """A param name for a joint: its leaf name, or its whole path when leaves collide."""
    name = re.sub(r"\W+", "_", _leaf(joint))
    if name in taken:
        name = re.sub(r"\W+", "_", joint)
    return name


def _wanted(node, rig: _Rig) -> dict:
    """joint path -> param name, for every joint the patterns match."""
    param = node.get_param("joints")
    patterns = [p.strip() for p in re.split(r"[,\s]+", (param.value if param else "") or "") if p.strip()]
    if not patterns:
        return {}
    out = {}
    taken = set()
    for joint in rig.posable():
        leaf = _leaf(joint)
        # A pattern with a slash is a path pattern; without one it names leaves, so `h*`
        # means hips and hand, not everything under hips.
        if any(fnmatch.fnmatchcase(joint if "/" in p else leaf, p) for p in patterns):
            name = _param_name(joint, taken)
            taken.add(name)
            out[joint] = name
    return out


def _lofted(node):
    """The per-joint handles, which is every Vector on the chunk."""
    return [p for p in node.get_my_params() if isinstance(p, chunk.Vector)]


def _saved_values(node) -> dict:
    """Handle values recovered from the chunk's own prim.

    The handles are created at runtime, so they do not exist yet when the loader restores
    param values -- but the values were written to USD all the same, as inputs:<joint> on
    the chunk prim. Reading them back from there is what lets a pose survive a save.
    """
    stage = node.get_stage()
    if stage is None:
        return {}
    try:
        path = get_chunk_prim_path(node)
    except Exception:
        return {}
    prim = stage.GetPrimAtPath(Sdf.Path(path)) if path else None
    if not prim or not prim.IsValid():
        return {}
    values = {}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if not name.startswith("inputs:") or name[len("inputs:"):] in _FIXED:
            continue
        value = attr.Get()
        if isinstance(value, (Gf.Vec3f, Gf.Vec3d)):
            values[name[len("inputs:"):]] = tuple(float(v) for v in value)
    return values


def _sync_joints(node) -> None:
    """Rebuild one rotate handle per matched joint, and its gizmo on the chunk prim.

    The gizmos are authored here rather than by the framework: author_gizmos runs when the
    chunk prim is created, which is before any joint has been named, and nothing re-runs
    it for a param that arrives later.
    """
    rig = _rig(node)
    wanted = _wanted(node, rig) if rig else {}
    have = {p.name: p for p in _lofted(node)}
    names = set(wanted.values())
    removed = [name for name in have if name not in names]
    for name in removed:
        node.remove_param_or_group(have[name])

    saved = _saved_values(node)
    for joint, name in wanted.items():
        if name in have:
            continue
        node.add_param(chunk.Vector(
            name, _leaf(joint),
            param=chunk.Slider(min_value=-180.0, max_value=180.0), length=3, suffixes="XYZ",
            default_value=saved.get(name, (0.0, 0.0, 0.0)),
            gizmo="rotate",
            flags=ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.COLLAPSABLE,
            tooltip=f"{joint}: degrees about the character's X, Y, Z through this joint.",
        ))
    node._joint_for = {name: joint for joint, name in wanted.items()}
    _author_gizmos(node, removed)


def _author_gizmos(node, removed) -> None:
    stage = node.get_stage()
    if stage is None:
        return
    try:
        prim = stage.GetPrimAtPath(get_chunk_prim_path(node))
    except Exception:
        return
    if not prim or not prim.IsValid():
        return
    home = chunk_utils.get_bookkeeping_layer(stage, node)
    with Usd.EditContext(stage, home) if home else Usd.EditContext(stage, stage.GetSessionLayer()):
        # A handle for a joint that is no longer named would otherwise keep drawing, driving
        # an attribute that no longer exists.
        for prop in prim.GetProperties():
            pname = prop.GetName()
            if pname.startswith(chunk_gizmo.GIZMO_PREFIX):
                if pname[len(chunk_gizmo.GIZMO_PREFIX):].split(":")[0] in removed:
                    prim.RemoveProperty(pname)
        chunk_gizmo.author_gizmos(stage, prim, node)


def _euler(value) -> Gf.Matrix4d:
    """The same XYZ convention chunk_gizmo.decompose reads a rotate handle in."""
    rx, ry, rz = (float(v) for v in value)
    rotation = (Gf.Rotation(Gf.Vec3d(1, 0, 0), rx)
                * Gf.Rotation(Gf.Vec3d(0, 1, 0), ry)
                * Gf.Rotation(Gf.Vec3d(0, 0, 1), rz))
    return Gf.Matrix4d().SetRotate(rotation)


def _rotation_only(matrix: Gf.Matrix4d) -> Gf.Matrix4d:
    return Gf.Matrix4d(matrix.ExtractRotationMatrix(), Gf.Vec3d(0))


def _local_rotation(rig: _Rig, joint: str, delta: Gf.Matrix4d) -> Gf.Quatf:
    """The joint's local rotation after `delta` is applied to it in skeleton space.

    Row-vector maths, as Gf: skel = local * parent. Adding a rotation D after the joint's
    own skeleton-space rotation R gives R*D, and taking the parent back out leaves
    L * P * D * P^-1 -- the delta conjugated into the parent's frame. Rest transforms
    rather than bind: rest is the pose the animation's own values are relative to.
    """
    r_joint = _rotation_only(rig.rest[joint])
    parent = rig.parent.get(joint)
    r_parent = _rotation_only(rig.rest[parent]) if parent else Gf.Matrix4d(1.0)
    local = r_joint * r_parent.GetInverse()
    conjugated = r_parent * delta * r_parent.GetInverse()
    quat = (local * conjugated).ExtractRotationQuat()
    return Gf.Quatf(quat.GetReal(), Gf.Vec3f(quat.GetImaginary()))


def _execute(node) -> None:
    layer = node.get_edit_context()
    if layer is None:
        return
    # Cleared first, so a pose is absolute rather than cumulative: the edit written last
    # cook is this layer's only content, and the next one replaces it.
    layer.Clear()

    rig = _rig(node)
    if rig is None:
        return
    if _wanted(node, rig) and not _lofted(node):
        _sync_joints(node)

    joints = getattr(node, "_joint_for", {})
    builder = Vt.QuatfArrayEditBuilder()
    posed = 0
    for vector in _lofted(node):
        joint = joints.get(vector.name)
        value = vector.value
        if joint is None or value is None or not any(abs(float(v)) > 1e-9 for v in value):
            continue
        builder.Write(_local_rotation(rig, joint, _euler(value)), rig.anim_index[joint])
        posed += 1
    if not posed:
        return

    with Usd.EditContext(rig.stage, layer):
        prim = rig.stage.OverridePrim(rig.anim_prim.GetPath())
        attr = prim.CreateAttribute("rotations", Sdf.ValueTypeNames.QuatfArray, custom=False)
        attr.Set(builder.FinalizeAndReset())


#: Nodes currently inside execute(), by id, so a cook cannot re-enter itself.
_BUSY = set()


def execute(node):
    if id(node) in _BUSY:
        return
    _BUSY.add(id(node))
    try:
        _execute(node)
    finally:
        _BUSY.discard(id(node))


def gizmo_offset(node, gizmo):
    """Put each joint's handle on the joint, in the SkelRoot's space the handle anchors to."""
    rig = _rig(node)
    joint = getattr(node, "_joint_for", {}).get(gizmo)
    if rig is None or joint is None:
        return None
    skel_to_world = UsdGeom.Xformable(rig.skel_query.GetPrim()).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    root_to_world = UsdGeom.Xformable(rig.root_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    point = (skel_to_world * root_to_world.GetInverse()).Transform(rig.rest[joint].ExtractTranslation())
    return tuple(point)


def _param_changed(owner, notice, _sender):
    """Re-loft the handles when the character or the joint list changes."""
    if id(owner) in _BUSY:
        return
    changed = getattr(notice, "data", None)
    if changed is not None and getattr(changed, "name", None) in _FIXED:
        _sync_joints(owner)


def _chunk_loaded(owner, _notice, _sender):
    # Neither the handles nor their values survive a load on their own: the handles are
    # created at runtime, and the loader restores values before they exist.
    _sync_joints(owner)
    execute(owner)


def main():
    chunk_api = chunk.Chunk("Skel Pose")
    chunk_api.add_params([
        chunk.LineEdit(
            "primpath_location", "Character",
            flags=ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="A SkelRoot, or anything under one. Its skeleton must have an animation "
                    "source: that is the prim the pose is written onto.",
        ),
        chunk.LineEdit(
            "joints", "Joints",
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Joints to pose, by leaf name or path, with * wildcards: "
                    "RArm, RElbow, RHand -- or R*. One rotate handle is lofted per match.",
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
        Skel Pose -- holds a few joints of a UsdSkel character in a pose, over whatever
        is animating it.

        Name joints (RArm, RElbow, RHand, or a pattern like R*) and one rotate handle is
        lofted per match, anchored on the SkelRoot and drawn at the joint. Each handle
        is degrees about the character's X, Y and Z through that joint, on top of the
        rest pose; it replaces that joint's animated rotation rather than adding to it.

        Written as one sparse array edit -- `rotations = edit [write q to [i] ...]` --
        as the default value on the animation prim, in the chunk's own layer. A default
        in a stronger layer composes over time samples underneath, from a reference or
        from value clips, so nothing is baked: unnamed joints keep their animation and
        named ones hold. Clear the chunk's layer and the animation is untouched.

        Parameters:
          - Character: a SkelRoot or a prim beneath one. The first skeleton binding
            with an animation source is used.
          - Joints: comma or space separated names or globs, matched against the
            joint's leaf name and its full path in the animation's joint order.
          - <joint> (lofted): X, Y, Z in degrees, -180..180, with a rotate gizmo.
        """,
    )
