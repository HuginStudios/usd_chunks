"""Set Transform chunk — sets an absolute translate/rotate/scale on the selected prim(s).

Requires: pxr (OpenUSD), usd_chunks.
"""

from pxr import Gf, UsdGeom
from usd_chunks import chunk


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        return
    prim = prims[0]

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    f = chunk.is_param_t(float)
    tx = _chunk.get_param("tx", f).value
    ty = _chunk.get_param("ty", f).value
    tz = _chunk.get_param("tz", f).value
    rx = _chunk.get_param("rx", f).value
    ry = _chunk.get_param("ry", f).value
    rz = _chunk.get_param("rz", f).value
    sx = _chunk.get_param("sx", f).value
    sy = _chunk.get_param("sy", f).value
    sz = _chunk.get_param("sz", f).value

    # Callbacks already run inside the chunk's edit context (NoticeConnection).
    xform = UsdGeom.Xformable(prim)
    xform.ClearXformOpOrder()
    xform.AddTranslateOp().Set(Gf.Vec3d(tx, ty, tz))
    xform.AddRotateXYZOp().Set(Gf.Vec3f(rx, ry, rz))
    s = Gf.Vec3f(sx, sy, sz)
    if s != Gf.Vec3f(1.0, 1.0, 1.0):
        xform.AddScaleOp().Set(s)


def main():
    chunk_api = chunk.Chunk("Set Transform")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)
    prim_path.add_callback(_run)

    translate_grp = chunk.Group("Translate")
    translate_grp.add_param(chunk.Spinbox("tx", "Translate X", default_value=0.0))
    translate_grp.add_param(chunk.Spinbox("ty", "Translate Y", default_value=0.0))
    translate_grp.add_param(chunk.Spinbox("tz", "Translate Z", default_value=0.0))
    chunk_api.add_param(translate_grp)

    rotate_grp = chunk.Group("Rotate")
    rotate_grp.add_param(chunk.Spinbox("rx", "Rotate X", default_value=0.0))
    rotate_grp.add_param(chunk.Spinbox("ry", "Rotate Y", default_value=0.0))
    rotate_grp.add_param(chunk.Spinbox("rz", "Rotate Z", default_value=0.0))
    chunk_api.add_param(rotate_grp)

    scale_grp = chunk.Group("Scale")
    scale_grp.add_param(chunk.Spinbox("sx", "Scale X", default_value=1.0))
    scale_grp.add_param(chunk.Spinbox("sy", "Scale Y", default_value=1.0))
    scale_grp.add_param(chunk.Spinbox("sz", "Scale Z", default_value=1.0))
    chunk_api.add_param(scale_grp)

    for name in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"):
        chunk_api.get_param(name).add_callback(_run)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
