from pxr import UsdGeom, Gf
from usd_chunks import chunk, chunk_notice, chunk_core, chunk_utils


def execute(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    stage = owner.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = _chunk.get_param("primpath_location").value

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            return

        xformable = UsdGeom.Xformable(stage.OverridePrim(target_path))
        existing  = {op.GetOpName(): op for op in xformable.GetOrderedXformOps()}
        transform_op = existing.get("xformOp:transform") or xformable.AddTransformOp()

        rotation = (
            Gf.Rotation(Gf.Vec3d(1, 0, 0), _chunk.get_param("rx").value) *
            Gf.Rotation(Gf.Vec3d(0, 1, 0), _chunk.get_param("ry").value) *
            Gf.Rotation(Gf.Vec3d(0, 0, 1), _chunk.get_param("rz").value)
        )
        xform = Gf.Transform()
        xform.SetTranslation(Gf.Vec3d(
            _chunk.get_param("tx").value,
            _chunk.get_param("ty").value,
            _chunk.get_param("tz").value,
        ))
        xform.SetRotation(rotation)
        xform.SetScale(Gf.Vec3d(
            _chunk.get_param("sx").value or 1.0,
            _chunk.get_param("sy").value or 1.0,
            _chunk.get_param("sz").value or 1.0,
        ))
        transform_op.Set(xform.GetMatrix())


def main():
    chunk_api = chunk.Chunk("Transform")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    primpath.add_callback(execute)
    chunk_api.add_param(primpath)

    for grp_name, grp_label, axes, min_v, max_v, default in [
        ("translate_grp", "Translate", ("tx", "ty", "tz"), -1000.0, 1000.0, 0.0),
        ("rotate_grp",    "Rotate",    ("rx", "ry", "rz"),  -180.0,  180.0, 0.0),
        ("scale_grp",     "Scale",     ("sx", "sy", "sz"),   0.0,  10000.0, 1.0),
    ]:
        grp = chunk.Group(
            grp_name,
            grp_label,
            flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
        )
        if grp_name != "translate_grp":
            grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)
        for name, label in zip(axes, ("X", "Y", "Z")):
            s = chunk.Slider(
                name, label,
                min_value=min_v, max_value=max_v, default_value=default,
                flags=chunk.ChunkFlags.ParamFlags.LABELED,
            )
            s.add_callback(execute)
            grp.add_param(s)
        chunk_api.add_param(grp)

    chunk_api.add_callback(execute, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Transform chunk — applies translate, rotate (XYZ Euler, degrees), and
        scale xform ops to an existing prim at a given stage path.

        Parameters:
          - Prim Path: the stage path of the prim to transform. The prim must
            already exist (created by another chunk). An over is authored in
            the chunk layer so the original prim definition is untouched.
          - Translate (X/Y/Z): world-space translation in scene units (-1000–1000).
            Expanded by default.
          - Rotate (X/Y/Z): XYZ Euler rotation in degrees (-180–180). Collapsed
            by default.
          - Scale (X/Y/Z): per-axis scale (0.001–100, default 1.0). Collapsed
            by default.

        Each execution clears the chunk layer and re-authors all three ops,
        keeping authoring idempotent. AddTranslateOp / AddRotateXYZOp /
        AddScaleOp are used so the xformOpOrder is always consistent.
        """,
    )
