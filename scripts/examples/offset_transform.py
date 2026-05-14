from pxr import Gf, Usd, UsdGeom
from usd_chunks import chunk

_ROTATE_TYPES = frozenset([
    UsdGeom.XformOp.TypeRotateXYZ,
    UsdGeom.XformOp.TypeRotateXZY,
    UsdGeom.XformOp.TypeRotateYXZ,
    UsdGeom.XformOp.TypeRotateYZX,
    UsdGeom.XformOp.TypeRotateZXY,
    UsdGeom.XformOp.TypeRotateZYX,
])

# keyed by id(chunk_instance) → {"path": str, "t": tuple, "r": tuple, "s": tuple}
_last_applied: dict = {}


def execute(owner, notice, sender):
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

    prim_path = prim.GetPath()
    prim_path_str = str(prim_path)
    chunk_id = id(_chunk)

    xformable = UsdGeom.Xformable(prim)
    cur_t = Gf.Vec3d(0.0, 0.0, 0.0)
    cur_r = Gf.Vec3f(0.0, 0.0, 0.0)
    cur_s = Gf.Vec3f(1.0, 1.0, 1.0)
    for op in xformable.GetOrderedXformOps():
        v = op.Get(0)
        if v is None:
            continue
        op_type = op.GetOpType()
        if op_type == UsdGeom.XformOp.TypeTranslate:
            cur_t = Gf.Vec3d(float(v[0]), float(v[1]), float(v[2]))
        elif op_type in _ROTATE_TYPES:
            cur_r = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))
        elif op_type == UsdGeom.XformOp.TypeScale:
            cur_s = Gf.Vec3f(float(v[0]), float(v[1]), float(v[2]))

    last = _last_applied.get(chunk_id)
    if last and last["path"] == prim_path_str:
        lt, lr, ls = last["t"], last["r"], last["s"]
        base_t = Gf.Vec3d(cur_t[0] - lt[0], cur_t[1] - lt[1], cur_t[2] - lt[2])
        base_r = Gf.Vec3f(cur_r[0] - lr[0], cur_r[1] - lr[1], cur_r[2] - lr[2])
        base_s = Gf.Vec3f(
            cur_s[0] / ls[0] if ls[0] else cur_s[0],
            cur_s[1] / ls[1] if ls[1] else cur_s[1],
            cur_s[2] / ls[2] if ls[2] else cur_s[2],
        )
    else:
        base_t, base_r, base_s = cur_t, cur_r, cur_s

    otx = float(_chunk.get_param("tx").value or 0)
    oty = float(_chunk.get_param("ty").value or 0)
    otz = float(_chunk.get_param("tz").value or 0)
    orx = float(_chunk.get_param("rx").value or 0)
    ory = float(_chunk.get_param("ry").value or 0)
    orz = float(_chunk.get_param("rz").value or 0)
    osx = float(_chunk.get_param("sx").value or 1)
    osy = float(_chunk.get_param("sy").value or 1)
    osz = float(_chunk.get_param("sz").value or 1)

    new_t = Gf.Vec3d(base_t[0] + otx, base_t[1] + oty, base_t[2] + otz)
    new_r = Gf.Vec3f(base_r[0] + orx, base_r[1] + ory, base_r[2] + orz)
    new_s = Gf.Vec3f(base_s[0] * osx, base_s[1] * osy, base_s[2] * osz)

    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    with Usd.EditContext(stage, layer):
        xformable.ClearXformOpOrder()
        xformable.AddTranslateOp().Set(new_t)
        xformable.AddRotateXYZOp().Set(new_r)
        if new_s != Gf.Vec3f(1.0, 1.0, 1.0):
            xformable.AddScaleOp().Set(new_s)

    _last_applied[chunk_id] = {
        "path": prim_path_str,
        "t": (otx, oty, otz),
        "r": (orx, ory, orz),
        "s": (osx, osy, osz),
    }


def main():
    chunk_api = chunk.Chunk("Offset Transform")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)
    prim_path.add_callback(execute)

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
        chunk_api.get_param(name).add_callback(execute)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
