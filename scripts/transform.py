from pxr import Sdf, Usd, UsdGeom, Gf
from usd_chunks import chunk, chunk_animation, chunk_core, chunk_utils


def _param_value_at(node, name: str, time_code: "Usd.TimeCode | None"):
    """Resolve a slider's value either live (time_code=None, the normal reactive-execute
    case) or at a specific frame by reading its own storage attribute directly (the
    bake() case) -- the slider is already keyframed by the animation feature by the time
    bake() runs, so this is just Usd.Attribute.Get(time_code), no recomputation needed."""
    param = node.get_param(name)
    if time_code is None:
        return param.value
    attr_path = getattr(param, "_param_attr_path", None)
    if attr_path:
        if attr := node.get_stage().GetAttributeAtPath(Sdf.Path(attr_path)):
            return attr.Get(time_code)
    return param.value


def _resolve_matrix(node, time_code: "Usd.TimeCode | None" = None) -> Gf.Matrix4d:
    def val(name: str):
        return _param_value_at(node, name, time_code)

    rotation = (
        Gf.Rotation(Gf.Vec3d(1, 0, 0), val("rx")) *
        Gf.Rotation(Gf.Vec3d(0, 1, 0), val("ry")) *
        Gf.Rotation(Gf.Vec3d(0, 0, 1), val("rz"))
    )
    xform = Gf.Transform()
    xform.SetTranslation(Gf.Vec3d(val("tx"), val("ty"), val("tz")))
    xform.SetRotation(rotation)
    xform.SetScale(Gf.Vec3d(val("sx") or 1.0, val("sy") or 1.0, val("sz") or 1.0))
    return xform.GetMatrix()


def execute(node):
    # Once this node's own inputs are animated, its output has already been baked (see
    # bake() below) -- real per-frame time samples on xformOp:transform that already
    # correctly resolve for ANY frame via pure USD composition, no recompute needed.
    # execute() still re-runs reactively on every frame-change tick while scrubbing
    # (ChunkAnimationManager.on_frame_changed pushes a new value into each animated
    # slider, which bubbles up through the DAG to here) -- if it did its normal thing,
    # its own chunk_layer.Clear() would wipe out the entire bake on the very next scrub
    # after baking, replacing it with a single plain Set() reflecting whatever frame the
    # timeline happened to be sitting on at the time. Skipping entirely here is what
    # keeps the bake authoritative. A manual edit to an already-animated slider still
    # gets a fresh full re-bake automatically (chunk_animation.bake_procedural_output is
    # called after every keyframe/clear-keyframe action) -- only an edit that bypasses
    # that (a plain slider drag relying on auto-key, not Set Keyframe/K) would leave the
    # baked output stale until the next explicit keyframe action.
    if chunk_animation.chunk_has_animated_params(node):
        return

    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    primpath_param = node.get_param("primpath_location")
    # get_param returns None while the chunk's params are being torn down (remove re-runs
    # execute via the DISABLED-unset). Bail rather than crash on None.value.
    if not primpath_param or not primpath_param.value:
        return

    matrix = _resolve_matrix(node)

    for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            continue

        xformable = UsdGeom.Xformable(stage.OverridePrim(target_path))
        existing = {op.GetOpName(): op for op in xformable.GetOrderedXformOps()}
        transform_op = existing.get("xformOp:transform") or xformable.AddTransformOp()
        transform_op.Set(matrix)


def bake(node, start_frame: float, end_frame: float):
    """Reserved bake hook (see usd_chunks/chunk_animation.py's bake_procedural_output,
    which calls this whenever this node's own sliders are animated). Authors real
    per-frame time samples on xformOp:transform for every currently-targeted prim,
    resolving each slider's value directly from its own already-keyframed storage
    attribute at each frame.

    Deliberately does NOT reuse execute()'s reactive path: no timeline scrubbing, no
    re-triggering the notice/DAG cascade, no dependency on execute()'s own per-run
    Clear() idiom. Just a plain, deterministic loop -- clear once, then write one time
    sample per frame per target.
    """
    stage = node.get_stage()
    primpath_param = node.get_param("primpath_location")
    if not primpath_param or not primpath_param.value:
        return

    chunk_layer = node.get_edit_context() or stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    with Usd.EditContext(stage, chunk_layer):
        transform_ops = []
        for target_path in chunk_utils.iter_prim_targets(stage, primpath_param):
            prim = stage.GetPrimAtPath(target_path)
            if not prim.IsValid():
                continue
            xformable = UsdGeom.Xformable(stage.OverridePrim(target_path))
            existing = {op.GetOpName(): op for op in xformable.GetOrderedXformOps()}
            transform_ops.append(existing.get("xformOp:transform") or xformable.AddTransformOp())

        for frame in range(int(start_frame), int(end_frame) + 1):
            time_code = Usd.TimeCode(frame)
            matrix = _resolve_matrix(node, time_code)
            for transform_op in transform_ops:
                transform_op.Set(matrix, time_code)


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
            grp.add_param(s)
        chunk_api.add_param(grp)

    return chunk_api


def config():
    return chunk.DefaulConfig(
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

        Keyframing any Translate/Rotate/Scale slider (K, or right-click ->
        Set Keyframe) triggers bake() -- a separate, explicit pass that authors
        real per-frame time samples on xformOp:transform across the current
        playback range, so downstream consumers reading genuine USD time
        samples (e.g. the RBD solver's mocap-collider detection) see this as
        animated. execute() itself is unchanged and stays purely reactive
        (current value only, no time codes) for ordinary live editing.
        """,
    )
