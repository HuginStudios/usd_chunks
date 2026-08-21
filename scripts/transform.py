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
        # Replace the op stack, don't append to it. AddTransformOp appends, so on a prim
        # that already has ops -- which every real asset does; an exported apple carries
        # translate + rotateXYZ + scale -- the composed order became
        # [translate, rotateXYZ, scale, transform] and our matrix was applied ON TOP of the
        # prim's own placement rather than instead of it. Reported as dragging a parent
        # prim suddenly showing rotate X -90 and scale 100: those are the asset's real
        # values, being applied twice. A scale of 100 came out as 10000.
        #
        # This chunk is absolute ("set the transform"), which is what makes replacing right;
        # offset_transform is the one that adds to what is already there. The order is
        # authored into this chunk's own layer, so the asset's ops are untouched and come
        # back the moment the chunk is deleted or muted.
        xformable.SetXformOpOrder([transform_op])


def main():
    # One handle for the whole chunk: in effect "host, run your native transform manipulator
    # and give me the result". It absorbs the three gizmo= groups below into a single
    # manipulator rather than leaving three that would fight over the same drag.
    chunk_api = chunk.Chunk("Transform", gizmo="transform")

    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value="/prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(primpath)

    # The last column is the whole gizmo declaration. A group already *is* the three params
    # its handle drives, so nothing needs listing -- and three separate handles is what a
    # viewport wants anyway, rather than one handle with modes.
    for grp_name, grp_label, axes, min_v, max_v, default, gizmo in [
        ("translate_grp", "Translate", ("tx", "ty", "tz"), -100.0, 100.0, 0.0, "translate"),
        ("rotate_grp",    "Rotate",    ("rx", "ry", "rz"),  -180.0,  180.0, 0.0, "rotate"),
        ("scale_grp",     "Scale",     ("sx", "sy", "sz"),   0.0,  100.0, 1.0, "scale"),
    ]:
        grp = chunk.Group(
            grp_name,
            grp_label,
            gizmo=gizmo,
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
        # No gizmos block: the handles are declared as gizmo= on the three groups in main().
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
