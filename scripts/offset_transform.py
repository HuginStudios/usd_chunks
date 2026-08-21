"""Offset Transform chunk — applies a relative translate/rotate/scale offset to the selected prim(s).

Requires: pxr (OpenUSD), usd_chunks.
"""

from pxr import Gf, UsdGeom
from usd_chunks import chunk, chunk_gizmo

_ROTATE_TYPES = frozenset(
    [
        UsdGeom.XformOp.TypeRotateXYZ,
        UsdGeom.XformOp.TypeRotateXZY,
        UsdGeom.XformOp.TypeRotateYXZ,
        UsdGeom.XformOp.TypeRotateYZX,
        UsdGeom.XformOp.TypeRotateZXY,
        UsdGeom.XformOp.TypeRotateZYX,
    ]
)

# keyed by id(chunk_instance) → {"path": str, "t": tuple, "r": tuple, "s": tuple}
_last_applied: dict = {}


def _clear_chunk_xform(owner):
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    layer = owner.get_edit_context()
    if not layer:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        return
    prim = prims[0]
    if not prim.IsValid() or prim.GetPath() == prim.GetPath().absoluteRootPath:
        return

    prim_path_str = str(prim.GetPath())
    chunk_id = id(_chunk)

    is_f = chunk.is_param_t(float)
    otx = _chunk.get_param("tx", is_f).value
    oty = _chunk.get_param("ty", is_f).value
    otz = _chunk.get_param("tz", is_f).value
    orx = _chunk.get_param("rx", is_f).value
    ory = _chunk.get_param("ry", is_f).value
    orz = _chunk.get_param("rz", is_f).value
    osx = _chunk.get_param("sx", is_f).value
    osy = _chunk.get_param("sy", is_f).value
    osz = _chunk.get_param("sz", is_f).value

    no_offset = otx == oty == otz == orx == ory == orz == 0.0 and osx == osy == osz == 1.0
    last = _last_applied.get(chunk_id)

    if last and last["path"] != prim_path_str:
        _clear_chunk_xform(owner)
        last = None
        _last_applied.pop(chunk_id, None)

    if no_offset:
        if last:
            _clear_chunk_xform(owner)
            _last_applied.pop(chunk_id, None)
        return

    if last:
        # Use the stored scene base — robust against the solver or any other chunk
        # overwriting xformOpOrder in the session layer between our runs.
        base_t = Gf.Vec3d(*last["base_t"])
        base_r = Gf.Vec3f(*last["base_r"])
        base_s = Gf.Vec3f(*last["base_s"])
    else:
        # First run for this prim — read the current scene transform before we
        # write anything to the session layer.
        xformable = UsdGeom.Xformable(prim)
        base_t = Gf.Vec3d(0.0, 0.0, 0.0)
        base_r = Gf.Vec3f(0.0, 0.0, 0.0)
        base_s = Gf.Vec3f(1.0, 1.0, 1.0)
        for op in xformable.GetOrderedXformOps():
            v = op.Get(0)
            if v is None:
                continue
            op_type = op.GetOpType()
            if op_type == UsdGeom.XformOp.TypeTransform:
                # A single matrix op, which the loop below cannot see: it only recognises
                # the separate translate/rotate/scale spellings. Anything upstream that
                # authors one -- a Transform chunk, or an asset exported with a baked
                # matrix -- was therefore read as identity, and the offset was applied to
                # nothing instead of to the placement actually in effect.
                parts = chunk_gizmo.decompose(Gf.Matrix4d(v))
                base_t = Gf.Vec3d(parts["translate"])
                base_r = Gf.Vec3f(*[float(x) for x in parts["rotate"]])
                base_s = Gf.Vec3f(*[float(x) for x in parts["scale"]])
            elif op_type == UsdGeom.XformOp.TypeTranslate:
                base_t = Gf.Vec3d(float(v[0]), float(v[1]), float(v[2]))
            elif op_type == UsdGeom.XformOp.TypeRotateX:
                base_r = Gf.Vec3f(float(v), base_r[1], base_r[2])
            elif op_type == UsdGeom.XformOp.TypeRotateY:
                base_r = Gf.Vec3f(base_r[0], float(v), base_r[2])
            elif op_type == UsdGeom.XformOp.TypeRotateZ:
                base_r = Gf.Vec3f(base_r[0], base_r[1], float(v))
            elif op_type in _ROTATE_TYPES:
                base_r = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))
            elif op_type == UsdGeom.XformOp.TypeScale:
                base_s = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))

    new_t = Gf.Vec3d(base_t[0] + otx, base_t[1] + oty, base_t[2] + otz)
    new_r = Gf.Vec3f(base_r[0] + orx, base_r[1] + ory, base_r[2] + orz)
    new_s = Gf.Vec3f(base_s[0] * osx, base_s[1] * osy, base_s[2] * osz)

    xformable = UsdGeom.Xformable(prim)
    _clear_chunk_xform(owner)
    # Callbacks already run inside the chunk's edit context (NoticeConnection).
    xformable.GetXformOpOrderAttr().Set([])
    xformable.AddTranslateOp().Set(new_t)
    xformable.AddRotateXYZOp().Set(new_r)
    if new_s != Gf.Vec3f(1.0, 1.0, 1.0):
        xformable.AddScaleOp().Set(new_s)

    _last_applied[chunk_id] = {
        "path": prim_path_str,
        "base_t": (base_t[0], base_t[1], base_t[2]),
        "base_r": (base_r[0], base_r[1], base_r[2]),
        "base_s": (base_s[0], base_s[1], base_s[2]),
    }


def execute(node: chunk.Chunk) -> None:
    """Reserved top-level reactive hook -- load_chunks calls this once per chunk after
    bulk-restoring its param values, so a reloaded Offset Transform actually reapplies its
    offset instead of just showing the right numbers with no effect on the stage (its real
    work lives in _run, an add_callback handler, which load's value-restore deliberately
    does not trigger)."""
    _run(node, None, None)


def main():
    # One handle for the whole chunk, as transform.py does: the three groups below carry the
    # parts, and gizmo="transform" says to present them as a single native manipulator
    # rather than three that would fight over the same drag.
    #
    # Declaring it also makes this chunk own its prim's placement as far as the gizmo layer
    # is concerned, so dragging a prim this chunk targets edits THESE params instead of
    # auto-creating a Transform chunk on top of it -- which is how a chunk opts out of that.
    chunk_api = chunk.Chunk("Offset Transform", gizmo="transform")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim to offset — pick it in the viewport or type a path.",
    )
    chunk_api.add_param(prim_path)
    prim_path.add_callback(_run)

    _t_tip = "Added to the prim's current {} translation, in scene units."
    translate_grp = chunk.Group("Translate", gizmo="translate")
    translate_grp.add_param(chunk.Spinbox("tx", "Translate X", default_value=0.0, tooltip=_t_tip.format("X")))
    translate_grp.add_param(chunk.Spinbox("ty", "Translate Y", default_value=0.0, tooltip=_t_tip.format("Y")))
    translate_grp.add_param(chunk.Spinbox("tz", "Translate Z", default_value=0.0, tooltip=_t_tip.format("Z")))
    chunk_api.add_param(translate_grp)

    _r_tip = "Added to the prim's current {} rotation, in degrees."
    rotate_grp = chunk.Group("Rotate", gizmo="rotate")
    rotate_grp.add_param(chunk.Spinbox("rx", "Rotate X", default_value=0.0, tooltip=_r_tip.format("X")))
    rotate_grp.add_param(chunk.Spinbox("ry", "Rotate Y", default_value=0.0, tooltip=_r_tip.format("Y")))
    rotate_grp.add_param(chunk.Spinbox("rz", "Rotate Z", default_value=0.0, tooltip=_r_tip.format("Z")))
    chunk_api.add_param(rotate_grp)

    _s_tip = "Multiplies the prim's current {} scale (1 = unchanged)."
    scale_grp = chunk.Group("Scale", gizmo="scale")
    scale_grp.add_param(chunk.Spinbox("sx", "Scale X", default_value=1.0, tooltip=_s_tip.format("X")))
    scale_grp.add_param(chunk.Spinbox("sy", "Scale Y", default_value=1.0, tooltip=_s_tip.format("Y")))
    scale_grp.add_param(chunk.Spinbox("sz", "Scale Z", default_value=1.0, tooltip=_s_tip.format("Z")))
    chunk_api.add_param(scale_grp)

    for name in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"):
        chunk_api.get_param(name).add_callback(_run)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Applies a relative translate/rotate/scale offset on top of a prim's existing\n"
            "transform (translate and rotate add, scale multiplies). Re-authors on each\n"
            "change and tracks the last applied offset, so dragging a value adjusts\n"
            "smoothly instead of stacking. Authors an over in the chunk layer."
        ),
    )
