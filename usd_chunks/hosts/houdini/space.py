"""A chunkgroup's network space: the up axis and units its network was authored in.

A network saved from Blender or usdview is often Z-up in centimetres; Houdini is Y-up in
metres. A chunk that builds geometry follows its stage's up axis, but a value typed into a
param does not -- a Transform's ``ty`` is "sideways" on one and "up" on the other. So the
chunks of a chunkgroup cook in the space their network was authored in, and the group's
result is turned into Houdini's space at its boundary:

- the chunkgroup node holds that space, as two parms (UP_PARM, MPU_PARM), filled from the
  file on import and written back to it on export;
- every chunk inside it cooks on a stage with that up axis and those units
  (lop._on_stage asks group_space);
- ``space_in``, a Python LOP after the group's input, turns what comes in from upstream
  into the network's space, so a chunk reading upstream geometry sees it the right way up;
- ``space_out``, before the group's output, turns everything back into Houdini's.

Both put one transform op in front of each top-level prim's own. An upstream prim gets
both, which cancel; a prim the group made gets only space_out's. A chunk that edits an
upstream prim in place (an over) is turned with it -- rare inside a group, and the price of
keeping each chunk cooking in its own space.
"""
from __future__ import annotations

import hou
from pxr import Gf, Usd, UsdGeom

#: The chunkgroup node's network space parms.
UP_PARM = "usdchunks_up_axis"
MPU_PARM = "usdchunks_meters_per_unit"
#: The Python LOPs at the group's boundary, by name.
IN_NODE = "space_in"
OUT_NODE = "space_out"
IN_CODE = "from usd_chunks.hosts.houdini import space\nspace.cook_in(hou.pwd())\n"
OUT_CODE = "from usd_chunks.hosts.houdini import space\nspace.cook_out(hou.pwd())\n"
#: The type of the chunkgroup node (lop.GROUP_TYPE, named here so this imports no lop).
_GROUP_TYPE = "usdchunks::chunkgroup"
_OP_SUFFIX = {IN_NODE: "usdchunks_to_network", OUT_NODE: "usdchunks_to_host"}


def stage_space(stage: Usd.Stage | None) -> tuple[str, float]:
    """(up axis, meters per unit) of a Houdini stage: Houdini's own for what it does not set.

    A Solaris stage usually authors neither. USD's fallback units are centimetres, but a
    stage in Houdini is in Houdini's space -- Y-up metres -- unless it says otherwise.
    """
    up, mpu = "Y", 1.0
    if stage is not None:
        if stage.HasAuthoredMetadata(UsdGeom.Tokens.upAxis):
            up = str(UsdGeom.GetStageUpAxis(stage)).upper()
        if stage.HasAuthoredMetadata(UsdGeom.Tokens.metersPerUnit):
            mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
    return up, mpu


def group_of(node: hou.Node) -> hou.Node | None:
    """The chunkgroup node node sits in, nearest first, or None."""
    parent = node.parent()
    while parent is not None:
        if parent.type().name() == _GROUP_TYPE:
            return parent
        parent = parent.parent()
    return None


def group_space(node: hou.Node) -> tuple[str, float] | None:
    """The network space of the chunkgroup node sits in, or None outside one.

    Read from its parms during a cook, so Houdini recooks what is inside when they change.
    """
    group = group_of(node)
    if group is None or group.parm(UP_PARM) is None:
        return None
    return group.evalParm(UP_PARM).upper(), float(group.evalParm(MPU_PARM))


def transform(source: tuple[str, float], target: tuple[str, float]) -> Gf.Matrix4d:
    """The matrix taking geometry in source space into target space.

    Scale first -- a length is source units * source meters-per-unit metres -- then the up
    axis: Z-up to Y-up is -90 degrees about X, which carries +Z onto +Y; Y-up to Z-up the
    reverse.
    """
    (source_up, source_mpu), (target_up, target_mpu) = source, target
    matrix = Gf.Matrix4d(1.0)
    if source_mpu > 0 and target_mpu > 0 and abs(source_mpu - target_mpu) > 1e-12:
        matrix = Gf.Matrix4d(1.0).SetScale(source_mpu / target_mpu)
    if source_up != target_up:
        angle = -90.0 if (source_up, target_up) == ("Z", "Y") else 90.0
        matrix = matrix * Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), angle))
    return matrix


def to_host(node: hou.Node) -> Gf.Matrix4d:
    """The matrix taking what node's stage holds into Houdini's world: what each chunkgroup
    it sits in turns its result by on the way out (space_out), innermost first."""
    matrix = Gf.Matrix4d(1.0)
    group = group_of(node)
    while group is not None:
        if group.parm(UP_PARM) is not None:
            inner = group.evalParm(UP_PARM).upper(), float(group.evalParm(MPU_PARM))
            inputs = group.inputs()
            host = stage_space(inputs[0].stage() if inputs and inputs[0] is not None else None)
            matrix = matrix * transform(inner, host)
        group = group_of(group)
    return matrix


def _xformable_roots(prim: Usd.Prim):
    """The topmost Xformable prims under prim: a Scope at the root is looked inside."""
    for child in prim.GetChildren():
        if child.IsA(UsdGeom.Xformable):
            yield child
        elif not child.IsA(UsdGeom.Imageable) or child.GetTypeName() == "Scope":
            yield from _xformable_roots(child)


def _apply(lop: hou.LopNode, matrix: Gf.Matrix4d) -> None:
    """Put matrix in front of the transform of every top-level prim on lop's stage."""
    if Gf.IsClose(matrix, Gf.Matrix4d(1.0), 1e-12):
        return
    stage = lop.editableStage()
    suffix = _OP_SUFFIX[lop.name()] if lop.name() in _OP_SUFFIX else "usdchunks_space"
    for prim in list(_xformable_roots(stage.GetPseudoRoot())):
        xformable = UsdGeom.Xformable(prim)
        ops = xformable.GetOrderedXformOps()
        op = xformable.AddTransformOp(opSuffix=suffix)
        op.Set(matrix)
        # In front: the first op in xformOpOrder is the outermost, applied last.
        xformable.SetXformOpOrder([op] + list(ops), xformable.GetResetXformStack())


def cook_in(lop: hou.LopNode) -> None:
    """space_in: what comes in from upstream, into the group's network space."""
    space = group_space(lop)
    if space is not None:
        stage = lop.editableStage()
        _apply(lop, transform(stage_space(stage), space))


def cook_out(lop: hou.LopNode) -> None:
    """space_out: the group's result, back into the space the group's input was in."""
    space = group_space(lop)
    group = group_of(lop)
    if space is None or group is None:
        return
    inputs = group.inputs()
    host = stage_space(inputs[0].stage() if inputs and inputs[0] is not None else None)
    _apply(lop, transform(space, host))


def ensure_nodes(group: hou.Node) -> tuple[hou.Node, hou.Node]:
    """The group's space_in and space_out, made and wired when it does not have them yet.

    space_in hangs off the group's input; space_out feeds its output.
    """
    space_in = group.node(IN_NODE)
    if space_in is None:
        space_in = group.createNode("pythonscript", IN_NODE)
        space_in.parm("python").set(IN_CODE)
        if group.indirectInputs():
            space_in.setInput(0, group.indirectInputs()[0])
    space_out = group.node(OUT_NODE)
    output = next((c for c in group.children() if c.type().name() == "output"), None)
    if space_out is None:
        space_out = group.createNode("pythonscript", OUT_NODE)
        space_out.parm("python").set(OUT_CODE)
        if output is not None:
            feeding = output.inputs()
            if feeding and feeding[0] is not None and feeding[0] != space_out:
                space_out.setInput(0, feeding[0])
            output.setInput(0, space_out)
    for node in (space_in, space_out):
        node.setColor(hou.Color(0.45, 0.45, 0.45))
        node.setComment("usd_chunks: the group's network space <-> Houdini's")
    return space_in, space_out


def is_space_node(node: hou.Node) -> bool:
    return node.type().name() == "pythonscript" and node.name() in (IN_NODE, OUT_NODE)
