import os
import json
import http.client
from pxr import Gf, Sdf, Usd, UsdGeom
from usd_chunks import chunk, chunk_core, chunk_gizmo, chunk_notice, chunk_utils

#: The shared op every chunk used to write, and what the freecam lock wrote until this chunk
#: owned a named one. Still read, so a camera saved before the change keeps its placement.
_LEGACY_OP = "xformOp:transform"

#: Set while `lock_to_freecam` owns the transform, so the first cook after it comes off knows
#: to take the flown position back into the params instead of authoring stale ones over it.
_FREECAM_OWNED = "_freecam_owned"


def _download_camera_types_cache(camera_cache):
    conn = http.client.HTTPSConnection("api.physicallybased.info")
    conn.request("GET", "/v2/cameras")

    res = conn.getresponse()
    data = json.loads(res.read().decode("utf-8"))
    with open(camera_cache, "w") as f:
        json.dump(data, f, indent=2)
    return data


def _get_camera_data():
    camera_cache = os.path.join(os.path.dirname(__file__), "cameras.json")
    if not os.path.exists(camera_cache):
        data = _download_camera_types_cache(camera_cache)
    else:
        with open(camera_cache, "r") as f:
            data = json.loads(f.read())
    cameras = data.get("data")
    return cameras


def set_format_list(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    selected_model = param.value
    cam_data = next((c for c in param.cameras if c.get("model") == selected_model), None)
    if cam_data is None:
        return
    sensor_size = cam_data.get("sensorSize")
    formats = _chunk.get_param("formats")
    formats.menu_items = [f.get("format") for f in sensor_size]
    set_preset_values(owner, notice, sender)


def set_preset_values(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    camera_presets = _chunk.get_param("camera_preset")
    selected_model = camera_presets.value
    cam_data = next((c for c in camera_presets.cameras if c.get("model") == selected_model), None)
    if cam_data is None:
        return
    formats = _chunk.get_param("formats")
    idx = formats.get_selected_index()
    sensor = cam_data["sensorSize"][idx]
    _chunk.get_param("focalLength").value = sensor.get("focalLength", [50.0])[0]
    _chunk.get_param("horizontalAperture").value = sensor["size"][0]
    _chunk.get_param("verticalAperture").value = sensor["size"][1]


def _apply_freecam_transform(owner, notice, sender):
    _chunk = owner.get_chunk()
    scene = _chunk.get_scene()
    do_lock = _chunk.get_param("lock_to_freecam").value
    if not do_lock:
        # Also reached on every camera move while the lock is off, so the host's release
        # must be (and is) idempotent. Here rather than only on the checkbox because the
        # checkbox callback is this same function.
        scene.release_viewport_camera()
        return

    gf_cam = scene.get_active_camera()
    if gf_cam is None:
        return
    stage = scene.get_stage()
    prim_path = _chunk.get_param("primpath_location").value
    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        return
    # Translate and rotate only -- the chunk keeps its own scale. The freecam is a viewpoint
    # and has no scale of its own to give, so taking its matrix whole silently replaced the
    # camera's scale with 1: the object visibly snapped back to unscaled while the sliders
    # went on reading what the user set, which is the worst way for the two to disagree.
    #
    # Through this chunk's own op, not MakeMatrixXform(). That resets xformOpOrder to a lone
    # `xformOp:transform`, which would wipe the named op the cook authors -- so unticking the
    # lock left the camera holding a transform from one system and an order naming the other.
    _author_matrix(_chunk, stage, prim, _with_local_scale(_chunk, gf_cam.transform))
    if not prim.GetAttribute("focusDistance").Get():
        _chunk.get_param("focusDistance").value = gf_cam.focusDistance
    # The camera just moved; the focus plane follows the point, not the camera.
    _refocus(_chunk)
    scene.set_viewport_camera(prim)


def _with_local_scale(node, matrix):
    """A placement from elsewhere, with this chunk's own scale kept.

    The freecam constrains where the camera *is* and which way it looks. It is a viewpoint and
    carries no scale worth having, so its matrix is taken for translate and rotate only and
    the chunk's scale params are re-applied underneath -- pre-multiplied, so the scale happens
    in the camera's own space and the placement is unaffected.
    """
    scale = _scale_of(node)
    if scale == Gf.Vec3d(1, 1, 1):
        return matrix
    return Gf.Matrix4d().SetScale(scale) * matrix


def _scale_of(node):
    return Gf.Vec3d(
        node.get_param("sx").value or 1.0,
        node.get_param("sy").value or 1.0,
        node.get_param("sz").value or 1.0,
    )


def _author_matrix(node, stage, prim, matrix):
    """Author `matrix` as this chunk's own named xformOp, replacing any it wrote before.

    Same shape as transform.py, and for the same two reasons.

    **A named op per chunk.** `xformOp:transform` is one attribute, so two chunks placing one
    prim competed for it and USD kept only the strongest. Owning a distinct name each, they
    compose -- so a Transform chunk on a camera now adds to it instead of annihilating it.

    **Authored outright**, not through `AddTransformOp()` / `GetOrderedXformOps()`. Both trust
    `xformOpOrder`, and that can name an op no layer defines with a type -- ordinary leftover
    state after a chunk is deleted and re-added, since the order and the attribute live in
    different layers and go away independently. Reproduced on a camera prim: 25.08 skips the
    untyped op, hands back an empty list, and `AddTransformOp()` then raises because the name
    *is* already in the order. 25.05 fails differently and also fails.
    """
    op_name = chunk_utils.xform_op_name(node)
    order = _without_legacy_op(node, stage, prim, chunk_gizmo.xform_op_order(node, prim, op_name))

    attr = prim.CreateAttribute(op_name, Sdf.ValueTypeNames.Matrix4d, custom=False)
    attr.Set(matrix)
    UsdGeom.Xformable(prim).CreateXformOpOrderAttr().Set(order)


def _without_legacy_op(node, stage, prim, order):
    """Drop the shared `xformOp:transform` this chunk wrote before it owned a named one.

    A camera saved before the change carries one holding the same placement the params do, so
    leaving it in the order would apply the camera's transform *twice* -- the doubling that
    turned a scale of 100 into 10000 when transform.py had the same problem.

    Only when the spec is in this chunk's own layer. A referenced camera that genuinely owns
    an `xformOp:transform` of its own keeps it, and keeps its place at the front of the order.
    """
    layer = stage.GetEditTarget().GetLayer()
    if layer is None or layer.GetAttributeAtPath(prim.GetPath().AppendProperty(_LEGACY_OP)) is None:
        return order

    legacy = prim.GetAttribute(_LEGACY_OP)
    if legacy and legacy.IsValid():
        legacy.Clear()
    return [op for op in order if op != _LEGACY_OP]


#: Purposes a selected prim's bound is measured with. Guides are left out: a light's or
#: camera's own guide geometry is not what anyone focuses on.
_FOCUS_PURPOSES = (UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy)


def focus_point_of(stage, prims, time=Usd.TimeCode.Default(), exclude=None):
    """The world-space point `prims` focus on, or None when nothing is measurable.

    Each prim counts from the centre of its world bound (its origin when it has no extent
    -- a bare Xform, a light); several average, so a selection of two houses focuses
    between them. `exclude` is the camera's own path: a camera is not its own subject.
    """
    cache = UsdGeom.BBoxCache(time, list(_FOCUS_PURPOSES), useExtentsHint=True)
    points = []
    for prim in prims:
        if not prim or not prim.IsValid() or (exclude is not None and prim.GetPath() == exclude):
            continue
        bound = cache.ComputeWorldBound(prim)
        if not bound.GetRange().IsEmpty():
            points.append(bound.ComputeCentroid())
        elif prim.IsA(UsdGeom.Xformable):
            points.append(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(time).ExtractTranslation())
    if not points:
        return None
    return sum(points, Gf.Vec3d(0, 0, 0)) / len(points)


def focus_depth(stage, camera_path, point, time=Usd.TimeCode.Default()):
    """The focus distance that puts world-space `point` in focus for the camera at `camera_path`.

    Depth along the camera's view axis, not the straight-line distance: focus is a plane
    parallel to the sensor, so a point off to the side of frame is as far away as the plane
    through it, however far the ray to it is. None when there is no camera to measure from.
    """
    camera = stage.GetPrimAtPath(camera_path) if camera_path else None
    if camera is None or not camera.IsValid():
        return None
    view = UsdGeom.Xformable(camera).ComputeLocalToWorldTransform(time)
    eye = view.ExtractTranslation()
    forward = view.TransformDir(Gf.Vec3d(0, 0, -1)).GetNormalized()
    return max(0.0, Gf.Dot(Gf.Vec3d(*point) - eye, forward))


def selection_focus_distance(stage, camera_path, prims, time=Usd.TimeCode.Default()):
    """The focus distance for `prims` -- focus_depth of their focus_point_of. None when
    nothing is measurable or there is no camera, so the caller leaves the value alone."""
    camera = stage.GetPrimAtPath(camera_path) if camera_path else None
    if camera is None or not camera.IsValid():
        return None
    point = focus_point_of(stage, prims, time, exclude=camera.GetPath())
    return None if point is None else focus_depth(stage, camera_path, point, time)


def _time(scene):
    try:
        return Usd.TimeCode(scene.get_current_frame())
    except NotImplementedError:
        return Usd.TimeCode.Default()


def _focus_on_selection(owner, notice, sender):
    """Remember the selection's focus point and start autofocusing on it.

    The point is stored, not the prims: what the camera pulled focus to is a place in the
    world, and it stays put if the prim is later moved, renamed or gone. Autofocus then
    keeps Focus Distance on it as the camera moves -- flown with the freecam lock, dragged
    with the handle, or typed. Untick Autofocus to freeze the distance where it is.
    """
    _chunk = owner.get_chunk()
    stage = _chunk.get_stage()
    scene = _chunk.get_scene()
    if not stage or scene is None:
        return
    camera_path = _chunk.get_param("primpath_location").value
    camera = stage.GetPrimAtPath(camera_path) if camera_path else None
    if camera is None or not camera.IsValid():
        return
    point = focus_point_of(stage, scene.get_selected_prims(), _time(scene), exclude=camera.GetPath())
    if point is None:
        return
    _chunk.get_param("focus_point").value = tuple(float(c) for c in point)
    _chunk.get_param("autofocus").value = True
    _refocus(_chunk)


def _refocus(node) -> None:
    """With Autofocus on, put Focus Distance back on the Focus Point from where the camera
    is now. Called after every authoring of the camera's transform. Writes only on a
    change: the write re-cooks the chunk, and the re-cook lands here again."""
    autofocus = node.get_param("autofocus")
    if autofocus is None or not autofocus.value:
        return
    stage = node.get_stage()
    scene = node.get_scene()
    if not stage or scene is None:
        return
    distance = focus_depth(
        stage, node.get_param("primpath_location").value, node.get_param("focus_point").value, _time(scene)
    )
    if distance is None:
        return
    param = node.get_param("focusDistance")
    if abs(float(param.value or 0.0) - distance) > 1e-6:
        param.value = distance


def _set_as_viewport_camera(owner, notice, sender):
    _chunk = owner.get_chunk()
    scene = _chunk.get_scene()
    stage = _chunk.get_stage()
    prim_path = _chunk.get_param("primpath_location").value
    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        return
    scene.set_viewport_camera(prim)


def gizmo_hidden(node, gizmo):
    """Hide the transform handle while the freecam owns the transform.

    `_apply_freecam_transform` re-authors the camera's transform from the DCC freecam on
    every CAMERA_CHANGED, so a drag would be overwritten a frame later. The handle would
    look draggable and silently snap back, which is worse than not offering it.

    Only the transform handle. Focus distance is untouched by the lock -- the freecam only
    seeds it once, when the prim has no value yet -- so that handle stays usable.
    """
    return gizmo == "transform" and node.get_param("lock_to_freecam").value


def _camera_matrix(node):
    """Compose the camera's transform params, in the same order transform.py uses.

    Deliberately identical: chunk_gizmo.decompose is the inverse of exactly this, so a
    transform handle that round-trips through one chunk must round-trip through the other.
    Diverging here would show up as the camera handle drifting while the transform chunk's
    behaves.
    """
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
    xform.SetScale(_scale_of(node))
    return xform.GetMatrix()


_TRANSFORM_PARAMS = ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz")


def _adopt_existing_transform(node, prim, force=False):
    """Seed the transform params from the prim, so a cook does not author over its placement.

    Without this, unticking `lock_to_freecam` snaps the camera back: the freecam placed the
    prim while the lock was on, our params still say something else, and the very next cook
    authors those params straight over it. Losing a camera position you just flew to is a bad
    way to find out the two systems disagree.

    Normally only when *every* param is still at its default, so it never overwrites a real
    edit -- a user who typed a position keeps it even if the prim carries something else.

    `force` is the exception, and it is the whole reason this takes an argument: while the
    lock is on the freecam *is* the authority, so on the tick the lock comes off there is no
    "real edit" to protect -- the params are stale by definition. Reported as: move the camera
    with the gizmo, lock, fly somewhere, unlock, and the camera jumps back to where the gizmo
    left it. The defaults-only guard was silently discarding the flight for any camera that
    had ever been touched.
    """
    if not force and any(
        node.get_param(n).value != node.get_param(n).default_value for n in _TRANSFORM_PARAMS
    ):
        return

    # This chunk's own op first, then the shared one a camera saved before the named-op
    # change carries. Read straight off the attribute rather than through
    # GetOrderedXformOps(), which silently drops an op that xformOpOrder names but no layer
    # types -- the state that made AddTransformOp() raise here in the first place.
    matrix = None
    for name in (chunk_utils.existing_xform_op_name(node), _LEGACY_OP):
        if not name:
            continue
        attr = prim.GetAttribute(name)
        if attr and attr.IsValid() and (value := attr.Get()) is not None:
            matrix = value
            break
    if matrix is None:
        return

    # decompose is the exact inverse of _camera_matrix, so the seeded params recompose to
    # the transform we just read rather than to something subtly rotated.
    parts = chunk_gizmo.decompose(matrix)
    for names, values in (
        (("tx", "ty", "tz"), parts["translate"]),
        (("rx", "ry", "rz"), parts["rotate"]),
        (("sx", "sy", "sz"), parts["scale"]),
    ):
        for name, value in zip(names, values):
            node.get_param(name).value = float(value)


def _author_camera_transform(node, stage):
    """Write the camera's own transform onto its own prim, in its own layer.

    The camera carries its transform rather than reaching across to an upstream Transform
    chunk -- the same shape Houdini's asset-reference node has, with a transform built in.
    That is what makes cross-chunk gizmo drivers unnecessary rather than merely unused: the
    handle drives params that belong to the chunk it is declared on.
    """
    if node.get_param("lock_to_freecam").value:
        # The freecam owns the transform while the lock is on, and re-authors it on every
        # CAMERA_CHANGED. Writing params here would be silently overwritten a frame later,
        # which is worse than not writing them.
        setattr(node, _FREECAM_OWNED, True)
        return

    # Edge-triggered, not level-triggered: this is the first cook since the lock came off, so
    # the params are stale by definition and adopting is not overwriting anything. Read and
    # cleared here so it fires exactly once per lock, however many cooks follow.
    handing_back = getattr(node, _FREECAM_OWNED, False)
    if handing_back:
        setattr(node, _FREECAM_OWNED, False)

    path = node.get_param("primpath_location").value
    if not path:
        return

    with chunk_utils.author_in_variant_context(stage, path) as target_path:
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            return
        # Take on whatever is already there before overwriting it -- see the docstring.
        _adopt_existing_transform(node, prim, force=handing_back)

        _author_matrix(node, stage, stage.OverridePrim(target_path), _camera_matrix(node))


def main():
    # gizmo="transform" gives the whole chunk one native manipulator, built from the three
    # groups below. The focus-distance handle is declared separately in config(), since it
    # needs an axis and sugar is type-only.
    chunk_api = chunk.Chunk("Camera", gizmo="transform")

    # prim location
    # A literal, not get_unique_path(stage, ...). A default computed from whatever the stage
    # holds at construction is a value wearing a default's clothes: the save path leaves
    # defaults out on the grounds that running main() again reproduces them, and running
    # main() again does not reproduce this one -- on reload no prim exists yet, so every
    # instance is handed the same path back. execute() does the uniquifying and records the
    # result as a value, which is what survives a save.
    prim_path = "/cameras/camera1"
    primpath = chunk.LineEdit(
        "primpath_location",
        "Prim Path",
        default_value=prim_path,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
    )
    chunk_api.add_param(primpath)
    # add camera type
    cameras = _get_camera_data()
    cam_presets = [cam.get("model") for cam in cameras]
    menu = chunk.Menu("camera_preset", "Camera Preset", menu_items=cam_presets)
    menu.cameras = cameras
    # Default to Fuji X-T5.
    menu.value = "X-T5"
    menu.add_callback(set_format_list, send_on_create=False)
    chunk_api.add_param(menu)
    formats = chunk.Menu("formats", "Formats")
    formats.add_callback(set_preset_values, send_on_create=False)
    chunk_api.add_param(formats)

    
    chunk_api.add_param(chunk.Slider("focalLength", "Focal Length", min_value=1.0, max_value=800.0))
    chunk_api.add_param(chunk.Slider("horizontalAperture", "Horizontal Aperture", min_value=1.0, max_value=100.0))
    chunk_api.add_param(chunk.Slider("horizontalApertureOffset", "Horizontal Aperture Offset", min_value=-50.0, max_value=50.0))
    chunk_api.add_param(chunk.Slider("verticalAperture", "Vertical Aperture", min_value=1.0, max_value=100.0))
    chunk_api.add_param(chunk.Slider("verticalApertureOffset", "Vertical Aperture Offset", min_value=-50.0, max_value=50.0))
    chunk_api.add_param(chunk.Slider("fStop", "f-Stop", min_value=0.7, max_value=64.0))
    chunk_api.add_param(chunk.Slider("focusDistance", "Focus Distance", min_value=0.0, max_value=1000.0))
    focus_btn = chunk.Button(
        "focus_on_selection", "Focus on Selection",
        tooltip="Remember the selected prim as the Focus Point (the average of several) and "
        "turn Autofocus on, so Focus Distance follows it as the camera moves.",
    )
    focus_btn.add_callback(_focus_on_selection, send_on_create=False)
    chunk_api.add_param(focus_btn)
    chunk_api.add_param(chunk.Checkbox(
        "autofocus", "Autofocus", default_value=False,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Keep Focus Distance on the Focus Point from wherever the camera is -- flown, "
        "dragged or typed. Off, the distance stays where it was last set.",
    ))
    chunk_api.add_param(chunk.Vector(
        "focus_point", "Focus Point",
        param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0),
        flags=chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.COLLAPSABLE,
        tooltip="World-space point Autofocus keeps in focus. Set by Focus on Selection, or type one.",
    ))

    default_cam = next((c for c in cameras if c.get("model") == menu.value), cameras[0])
    formats.menu_items = [f.get("format") for f in default_cam.get("sensorSize", [])]

    # The camera's own transform, so it needs no upstream Transform chunk to be placed.
    # Same param names and grouping as transform.py, so the one transform handle behaves
    # identically wherever it appears.
    for grp_name, grp_label, axes, min_v, max_v, default, gizmo in [
        ("translate_grp", "Translate", ("tx", "ty", "tz"), -100.0, 100.0, 0.0, "translate"),
        ("rotate_grp",    "Rotate",    ("rx", "ry", "rz"), -180.0, 180.0, 0.0, "rotate"),
        ("scale_grp",     "Scale",     ("sx", "sy", "sz"),    0.0, 100.0, 1.0, "scale"),
    ]:
        grp = chunk.Group(
            grp_name, grp_label, gizmo=gizmo,
            flags=chunk_core.ChunkFlags.GroupFlags.COLLAPSABLE | chunk_core.ChunkFlags.GroupFlags.LABELED,
        )
        grp.set_state(chunk_core.ChunkFlags.State.COLLAPSED)
        for name, label in zip(axes, ("X", "Y", "Z")):
            grp.add_param(chunk.Slider(
                name, label, min_value=min_v, max_value=max_v, default_value=default,
                flags=chunk.ChunkFlags.ParamFlags.LABELED,
            ))
        chunk_api.add_param(grp)

    lock_cam = chunk.Checkbox("lock_to_freecam", "Lock to Free Camera")
    lock_cam.add_callback(_apply_freecam_transform)
    chunk_api.add_param(lock_cam)
    chunk_api.add_callback(
        _apply_freecam_transform, notice_alias=chunk.ChunkNoticeAlias.CAMERA_CHANGED, listen_globally=True
    )

    set_cam_btn = chunk.Button("set_viewport_camera", "Set as Viewport Camera")
    set_cam_btn.add_callback(_set_as_viewport_camera)
    chunk_api.add_param(set_cam_btn)

    return chunk_api


def execute(node):
    stage = node.get_stage()
    if not stage:
        return

    raw_path = node.get_param("primpath_location").value

    # The path-unchanged guard used to skip the whole function. It still skips re-creating
    # the prim -- get_unique_path would otherwise rename the camera on every cook -- but the
    # transform below has to be re-authored whenever its params change, and that is exactly
    # the case where the path has *not* moved. Leaving the early return in place made the
    # transform handle appear to do nothing at all.
    if raw_path == getattr(node, '_last_camera_path', None):
        _author_camera_transform(node, stage)
        _refocus(node)
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    _, _, var_selections = chunk_utils.split_variant_path(raw_path)
    if not var_selections and not chunk_notice._loading_stage:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)
        # Recorded as a value, not left as a local. This is the only thing telling this
        # chunk apart from another of the same kind, and a saved scene carries chunk-prim
        # values -- without it two cameras reopened both claiming /cameras/camera1 and
        # authored over one prim. Guarded so a path that did not move is not written back
        # on every cook.
        if raw_path != node.get_param("primpath_location").value:
            node.get_param("primpath_location").value = raw_path

    node._last_camera_path = raw_path

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.DefinePrim(target_path, "Camera")

        node.get_param("focalLength").connect_usd_attr(prim.GetAttribute("focalLength"))
        node.get_param("horizontalAperture").connect_usd_attr(prim.GetAttribute("horizontalAperture"))
        node.get_param("horizontalApertureOffset").connect_usd_attr(prim.GetAttribute("horizontalApertureOffset"))
        node.get_param("verticalAperture").connect_usd_attr(prim.GetAttribute("verticalAperture"))
        node.get_param("verticalApertureOffset").connect_usd_attr(prim.GetAttribute("verticalApertureOffset"))
        node.get_param("fStop").connect_usd_attr(prim.GetAttribute("fStop"))
        node.get_param("focusDistance").connect_usd_attr(prim.GetAttribute("focusDistance"))

        attr = UsdGeom.VisibilityAPI.Apply(prim).CreateGuideVisibilityAttr()
        attr.Set(UsdGeom.Tokens.inherited)

        if not var_selections and prim.GetPath().pathString != target_path:
            node.get_param("primpath_location").value = prim.GetPath().pathString
            node._last_camera_path = prim.GetPath().pathString

    # After the prim exists, and after the Clear above -- authoring it earlier would be wiped.
    _author_camera_transform(node, stage)
    _refocus(node)

    if node.get_param("lock_to_freecam").value:
        scene = node.get_scene()
        prim = stage.GetPrimAtPath(node.get_param("primpath_location").value)
        if prim.IsValid():
            scene.set_viewport_camera(prim)


def config():
    return chunk.DefaulConfig(
        # Focus distance is a scalar handle: one shape, one driven param. axis="Z" because
        # the direction *is* the meaning here -- focus distance runs along the camera's -Z,
        # and a handle that slid in any direction would lose that. It also gives a native
        # host enough to build a real linear handle instead of a generic drag.
        #
        # The anchor is left to inference: primpath_location by name, since this chunk never
        # sets PRIM_SELECTION on it.
        #
        # Note usd-guides already draws a focus reticle at fz = -focus_dist. The viewport
        # work is promoting that existing curve to interactive, not drawing a second one on
        # top of it.
        gizmos=[
            chunk.Gizmo("zdist", type="cube", label="Focus Distance",
                        axis="-Z", driver="focusDistance"),
        ],
        category=chunk.StandardCategory.MODELLING,
        description="""
        Camera chunk — creates a UsdGeom.Camera prim and exposes its lens
        parameters as live sliders, with optional real-world camera presets
        and freecam lock for DCC viewport alignment.

        Parameters:
          - Prim Path: stage path for the camera prim. Auto-initialised to a
            unique /cameras/camera1 path. Updated in-place when taken (outside
            variant contexts).
          - Camera Preset: real-world camera body fetched from physicallybased.info
            (cached locally as cameras.json). Defaults to Fuji X-T5. Changing
            the preset repopulates the Formats menu and applies the first sensor
            size as the default aperture values.
          - Formats: sensor format for the selected camera body. Changing the
            format updates Focal Length, Horizontal Aperture, and Vertical
            Aperture to match the physical sensor dimensions.
          - Focal Length / Apertures / Offsets / f-Stop / Focus Distance: USD
            camera attributes wired as live sliders via connect_usd_attr, so
            slider changes write directly to the prim attribute.
          - Lock to Free Camera: when checked, the chunk subscribes to the
            scene's active-camera-changed signal and continuously syncs the
            prim's transform and focus distance from the DCC freecam. The
            prim is also scheduled as the active viewport camera.
          - Set as Viewport Camera: one-shot button that makes the camera prim
            the active viewport camera without enabling the lock.

        GuideVisibility is set to inherited so the camera frustum is visible
        in DCC guide modes. Variant-path authoring is supported — uniqueness
        checks are skipped when the path contains an embedded variant selection.
        """,
    )
