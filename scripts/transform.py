from pxr import UsdGeom, Gf
from usd_chunks import chunk, chunk_core, chunk_utils


def _rescale_translate_range(node, stage):
    """Keep the translate sliders' range at a constant ~10m-equivalent span regardless of
    the stage's declared scale, instead of a flat -1000..1000 in raw stage units. That flat
    range only felt right by accident for a centimeters-scale stage (metersPerUnit=0.01,
    USD's own default when unauthored -- 10 / 0.01 = 1000, exactly the old constant); a
    meters-scale stage (metersPerUnit=1) got the same +-1000 range applied to a scene where
    objects are typically single-digit units across, so a small drag (or typing "5") could
    move something many times its own size -- reported live as translate "flying away" and
    being unusably coarse to fine-tune."""
    meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
    if not meters_per_unit:
        return
    span = 10.0 / meters_per_unit
    for name in ("tx", "ty", "tz"):
        param = node.get_param(name)
        if param is None:
            continue
        param.min_value = -span
        param.max_value = span


def execute(node):
    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    # Before the primpath guard below -- the slider range should be correct as soon as the
    # chunk exists, not only once a target prim is set.
    _rescale_translate_range(node, stage)

    primpath_param = node.get_param("primpath_location")
    # get_param returns None while the chunk's params are being torn down (remove re-runs
    # execute via the DISABLED-unset). Bail rather than crash on None.value.
    if not primpath_param or not primpath_param.value:
        return

    rotation = (
        Gf.Rotation(Gf.Vec3d(1, 0, 0), node.get_param("rx").value) *
        Gf.Rotation(Gf.Vec3d(0, 1, 0), node.get_param("ry").value) *
        Gf.Rotation(Gf.Vec3d(0, 0, 1), node.get_param("rz").value)
    )
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(
        node.get_param("tx").value,
        node.get_param("ty").value,
        node.get_param("tz").value,
    ))
    xform.SetRotation(rotation)
    xform.SetScale(Gf.Vec3d(
        node.get_param("sx").value or 1.0,
        node.get_param("sy").value or 1.0,
        node.get_param("sz").value or 1.0,
    ))
    matrix = xform.GetMatrix()

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            continue

        xformable = UsdGeom.Xformable(stage.OverridePrim(target_path))
        existing = {op.GetOpName(): op for op in xformable.GetOrderedXformOps()}
        transform_op = existing.get("xformOp:transform") or xformable.AddTransformOp()
        transform_op.Set(matrix)


def main():
    chunk_api = chunk.Chunk("Transform")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(primpath)

    for grp_name, grp_label, axes, min_v, max_v, default in [
        ("translate_grp", "Translate", ("tx", "ty", "tz"), -100.0, 100.0, 0.0),
        ("rotate_grp",    "Rotate",    ("rx", "ry", "rz"),  -180.0,  180.0, 0.0),
        ("scale_grp",     "Scale",     ("sx", "sy", "sz"),   0.0,  100.0, 1.0),
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
            grp.add_param(s)
        chunk_api.add_param(grp)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        # Per-slot rather than a single matrix driver: the params really are nine separate
        # floats, and a matrix driver could not be bound half to this chunk and half to
        # another one. Rotations are Euler XYZ degrees, matching execute()'s composition --
        # chunk_gizmo.decompose is the inverse and has to stay that way.
        gizmos=[
            chunk.Gizmo(
                "body",
                type="transform",
                anchor="primpath_location",
                driver={
                    "translate": ("translate_grp.tx", "translate_grp.ty", "translate_grp.tz"),
                    "rotate": ("rotate_grp.rx", "rotate_grp.ry", "rotate_grp.rz"),
                    "scale": ("scale_grp.sx", "scale_grp.sy", "scale_grp.sz"),
                },
            ),
        ],
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Transform chunk — applies translate, rotate (XYZ Euler, degrees), and
        scale xform ops to one or more existing prims.

        Parameters:
          - Prim Path: comma-separated prim paths or Houdini-style glob patterns
            (*, **, ?) to transform. Each matched prim must already exist
            (created by another chunk). An over is authored per prim in the
            chunk layer so the original prim definitions are untouched.
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
