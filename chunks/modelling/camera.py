from __future__ import annotations
import os
import json
import http.client
from pxr import Gf, Sdf, Usd, UsdGeom
from usd_chunks import chunk, chunk_utils
from usd_chunks.chunk_cache import user_cache_dir
from usd_chunks.gizmo import runtime as chunk_gizmo

#: The legacy shared transform op, from before each chunk owned a named one. Still read, so
#: a camera saved in that form keeps its placement.
_LEGACY_OP = "xformOp:transform"

#: Closer than this, a flown param is left as it is. Writing it would cook the camera, point
#: the viewport at it again, and the freecam would report the move back with float noise.
_FLOWN_TOLERANCE = 1e-6

#: Set while `lock_to_freecam` has this camera claiming the viewport. Only the chunk holding
#: the claim may release it: an unlocked camera chunk hears every camera move too, and
#: releasing on each one cancelled a snap still pending from someone else -- Set Camera
#: switching cameras, or another chunk's "Set as Viewport Camera".
_HOLDS_VIEW = "_holds_view"


def _download_camera_types_cache(camera_cache):
    conn = http.client.HTTPSConnection("api.physicallybased.info")
    conn.request("GET", "/v2/cameras")

    res = conn.getresponse()
    data = json.loads(res.read().decode("utf-8"))
    os.makedirs(os.path.dirname(camera_cache), exist_ok=True)
    with open(camera_cache, "w") as f:
        json.dump(data, f, indent=2)
    return data


def _get_camera_data():
    # Per-user, not beside this file: installed, that is site-packages.
    camera_cache = user_cache_dir("cameras.json")
    if not os.path.exists(camera_cache):
        data = _download_camera_types_cache(camera_cache)
    else:
        with open(camera_cache) as f:
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
    lock = _chunk.get_param("lock_to_freecam")
    if lock is None:
        # The chunk is mid-delete and its params are gone; this global listener outlives them.
        return
    if not lock.value:
        # Also reached on every camera move while the lock is off -- by every camera chunk,
        # so only the one whose lock claimed the view lets go of it. Here rather than only on
        # the checkbox because the checkbox callback is this same function.
        if getattr(_chunk, _HOLDS_VIEW, False):
            setattr(_chunk, _HOLDS_VIEW, False)
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
    # Into the params, not onto the prim: the cook authors the camera from them as it does
    # unlocked, so what the freecam did reaches the chunks wired below like any other edit,
    # and there is never a placement on the prim the params disagree with. The cook also
    # refocuses (_refocus), since the camera moved.
    with chunk.change_block():
        _follow_freecam(_chunk, gf_cam.transform)
        if not prim.GetAttribute("focusDistance").Get():
            _chunk.get_param("focusDistance").value = gf_cam.focusDistance
    setattr(_chunk, _HOLDS_VIEW, True)
    scene.set_viewport_camera(prim)


def _follow_freecam(node, matrix):
    """Set the translate and rotate params to where the freecam is.

    Translate and rotate only -- the chunk keeps its own scale. The freecam is a viewpoint
    and has no scale of its own to give, so taking its matrix whole silently replaced the
    camera's scale with 1: the object visibly snapped back to unscaled while the sliders
    went on reading what the user set, which is the worst way for the two to disagree.

    Only the params that moved are written (_FLOWN_TOLERANCE).
    """
    parts = chunk_gizmo.decompose(matrix)
    for names, values in ((("tx", "ty", "tz"), parts["translate"]), (("rx", "ry", "rz"), parts["rotate"])):
        for name, value in zip(names, values):
            param = node.get_param(name)
            if abs(float(param.value or 0.0) - float(value)) > _FLOWN_TOLERANCE:
                param.value = float(value)


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
    different layers and go away independently. On a camera prim, USD 25.08 skips the
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
    """The focus distance for `prims`, or None when nothing is measurable."""
    camera = stage.GetPrimAtPath(camera_path) if camera_path else None
    if camera is None or not camera.IsValid():
        return None
    point = chunk_utils.focus_point_of(stage, prims, time, exclude=camera.GetPath())
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
    point = chunk_utils.focus_point_of(stage, scene.get_selected_prims(), _time(scene), exclude=camera.GetPath())
    if point is None:
        return
    _chunk.get_param("focus_point").value = tuple(float(c) for c in point)
    _chunk.get_param("autofocus").value = True
    _refocus(_chunk)


def _refocus(node) -> None:
    """With Autofocus on, put Focus Distance back on the Focus Point from where the camera
    is now. Called after every authoring of the camera's transform. Writes only on a
    change: the write re-cooks the chunk, and the re-cook lands here again.
    """
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


def _aim(node, stage) -> None:
    """With Aim At set, turn the camera to face that prim's centre from where it stands.

    Into the rotate params, as _refocus writes Focus Distance, so the sliders, the transform
    handle and a saved file all read the rotation the camera really has -- and clearing Aim
    At leaves the camera facing where it last aimed. The view is levelled to the stage's up
    axis. Writes only on a change: each write re-cooks the chunk, and the re-cook lands here.
    """
    aim_param = node.get_param("aim_at")
    aim_path = aim_param.value
    if not aim_path:
        return
    target = stage.GetPrimAtPath(chunk_utils.split_variant_path(aim_path)[1])
    if not target:
        chunk_utils.warn_if_missing(node, aim_path, "the camera is not aimed", aim_param)
        return
    scene = node.get_scene()
    time = _time(scene) if scene is not None else Usd.TimeCode.Default()
    centre = chunk_utils.focus_point_of(stage, [target], time)
    if centre is None:
        return
    up = Gf.Vec3d(0, 0, 1) if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z else Gf.Vec3d(0, 1, 0)
    # The params are in the parent's space, so the aim is worked out there.
    camera = stage.GetPrimAtPath(chunk_utils.split_variant_path(node.get_param("primpath_location").value)[1])
    parent = camera.GetParent() if camera else None
    if parent and parent.IsA(UsdGeom.Xformable):
        to_parent = UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(time).GetInverse()
        centre, up = to_parent.Transform(centre), to_parent.TransformDir(up)
    eye = Gf.Vec3d(*(float(node.get_param(n).value) for n in ("tx", "ty", "tz")))
    if (centre - eye).GetLength() < 1e-9 or Gf.Cross(centre - eye, up).GetLength() < 1e-9:
        return  # on the target, or straight above or below it: no one way to face it
    look = Gf.Matrix4d().SetLookAt(eye, centre, up).GetInverse()
    # decompose is the inverse of _camera_matrix, so these recompose to exactly this facing.
    rotate = chunk_gizmo.decompose(look)["rotate"]
    changed = {
        name: float(value) for name, value in zip(("rx", "ry", "rz"), rotate)
        if abs(float(node.get_param(name).value) - float(value)) > _FLOWN_TOLERANCE
    }
    if changed:
        with chunk.change_block():
            for name, value in changed.items():
                node.get_param(name).value = value


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

    `_apply_freecam_transform` sets the transform params from the DCC freecam on every
    CAMERA_CHANGED, so a drag would be overwritten a frame later. The handle would
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


def _adopt_existing_transform(node, prim):
    """Seed the transform params from the prim, so a cook does not author over its placement.

    A camera prim that already carries a placement -- referenced in, or saved before the
    camera owned its transform -- would otherwise be authored over by default params on the
    first cook.

    Only when *every* param is still at its default, so it never overwrites a real edit -- a
    user who typed a position keeps it even if the prim carries something else. The freecam
    lock needs none of this: it writes the params themselves (_follow_freecam).
    """
    if any(
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
    _seed_transform_params(node, matrix)


def _seed_transform_params(node, matrix):
    """Set the transform params to `matrix`."""
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

    The same with Lock to Free Camera on: the freecam moves the params (_follow_freecam), and
    the cook authors them.
    """
    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            return
        # Take on whatever is already there before overwriting it -- see the docstring.
        _adopt_existing_transform(node, prim)

        _author_matrix(node, stage, stage.OverridePrim(target_path), _camera_matrix(node))


def _section(name: str, label: str, gizmo: str | None = None, icon: str = "") -> chunk.Group:
    """A collapsed, labelled group."""
    grp = chunk.Group(
        name, label, gizmo=gizmo, icon=icon,
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )
    grp.set_state(chunk.ChunkFlags.State.COLLAPSED)
    return grp


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


    lens = _section("lens_grp", "Lens", icon="camera")
    lens.add_param(chunk.Slider("focalLength", "Focal Length", min_value=1.0, max_value=800.0))
    lens.add_param(chunk.Slider("fStop", "f-Stop", min_value=0.7, max_value=64.0))
    chunk_api.add_param(lens)

    sensor = _section("sensor_grp", "Sensor", icon="box_select")
    sensor.add_param(chunk.Slider("horizontalAperture", "Horizontal Aperture", min_value=1.0, max_value=100.0))
    sensor.add_param(chunk.Slider("verticalAperture", "Vertical Aperture", min_value=1.0, max_value=100.0))
    sensor.add_param(chunk.Slider("horizontalApertureOffset", "Horizontal Aperture Offset", min_value=-50.0, max_value=50.0))
    sensor.add_param(chunk.Slider("verticalApertureOffset", "Vertical Aperture Offset", min_value=-50.0, max_value=50.0))
    chunk_api.add_param(sensor)

    focus = _section("focus_grp", "Focus", icon="pivot")
    focus.add_param(chunk.Slider("focusDistance", "Focus Distance", min_value=0.0, max_value=1000.0))
    focus_btn = chunk.Button(
        "focus_on_selection", "Focus on Selection",
        tooltip="Remember the selected prim as the Focus Point (the average of several) and "
        "turn Autofocus on, so Focus Distance follows it as the camera moves.",
    )
    focus_btn.add_callback(_focus_on_selection, send_on_create=False)
    focus.add_param(focus_btn)
    focus.add_param(chunk.Checkbox(
        "autofocus", "Autofocus", default_value=False,
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Keep Focus Distance on the Focus Point from wherever the camera is -- flown, "
        "dragged or typed. Off, the distance stays where it was last set.",
    ))
    focus.add_param(chunk.Vector(
        "focus_point", "Focus Point",
        param=chunk.Slider(), length=3, suffixes="XYZ", default_value=(0.0, 0.0, 0.0),
        flags=chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.COLLAPSABLE,
        tooltip="World-space point Autofocus keeps in focus. Set by Focus on Selection, or type one.",
    ))
    chunk_api.add_param(focus)

    clipping = _section("clipping_grp", "Clipping", icon="ruler")
    # One Vector, not two Sliders: UsdGeom.Camera keeps near and far together as the float2
    # `clippingRange`, and a Vector is the one param shape connect_usd_attr binds to a float2
    # whole. Defaults are the schema's own (1, 1000000) so an untouched camera clips as USD would.
    clipping.add_param(chunk.Vector(
        "clippingRange", "Clipping Range",
        param=chunk.Slider(min_value=0.001, max_value=100000.0), length=2, suffixes=("Near", "Far"),
        default_value=(1.0, 1000000.0),
        tooltip="Near and far clipping planes, in scene units. Geometry closer than Near or "
        "further than Far is not drawn.",
    ))
    chunk_api.add_param(clipping)

    default_cam = next((c for c in cameras if c.get("model") == menu.value), cameras[0])
    formats.menu_items = [f.get("format") for f in default_cam.get("sensorSize", [])]

    # The camera's own transform, so it needs no upstream Transform chunk to be placed.
    # Same param names and grouping as transform.py, so the one transform handle behaves
    # identically wherever it appears.
    for grp_name, grp_label, axes, min_v, max_v, default, gizmo, icon in [
        ("translate_grp", "Translate", ("tx", "ty", "tz"), -100.0, 100.0, 0.0, "translate", "move"),
        ("rotate_grp",    "Rotate",    ("rx", "ry", "rz"), -180.0, 180.0, 0.0, "rotate", "rotate"),
        ("scale_grp",     "Scale",     ("sx", "sy", "sz"),    0.0, 100.0, 1.0, "scale", "scale"),
    ]:
        grp = _section(grp_name, grp_label, gizmo=gizmo, icon=icon)
        for name, label in zip(axes, ("X", "Y", "Z")):
            grp.add_param(chunk.Slider(
                name, label, min_value=min_v, max_value=max_v, default_value=default,
                flags=chunk.ChunkFlags.ParamFlags.LABELED,
            ))
        chunk_api.add_param(grp)

    chunk_api.add_param(chunk.LineEdit(
        "aim_at", "Aim At",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="A prim to keep the camera pointed at, from wherever it is moved to. Sets "
        "Rotate on every cook; clear it to rotate by hand again.",
    ))

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

    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param, unique=True):
        prim = stage.DefinePrim(target_path, "Camera")

        node.get_param("focalLength").connect_usd_attr(prim.GetAttribute("focalLength"))
        node.get_param("horizontalAperture").connect_usd_attr(prim.GetAttribute("horizontalAperture"))
        node.get_param("horizontalApertureOffset").connect_usd_attr(prim.GetAttribute("horizontalApertureOffset"))
        node.get_param("verticalAperture").connect_usd_attr(prim.GetAttribute("verticalAperture"))
        node.get_param("verticalApertureOffset").connect_usd_attr(prim.GetAttribute("verticalApertureOffset"))
        node.get_param("fStop").connect_usd_attr(prim.GetAttribute("fStop"))
        node.get_param("focusDistance").connect_usd_attr(prim.GetAttribute("focusDistance"))
        node.get_param("clippingRange").connect_usd_attr(prim.GetAttribute("clippingRange"))

        attr = UsdGeom.VisibilityAPI.Apply(prim).CreateGuideVisibilityAttr()
        attr.Set(UsdGeom.Tokens.inherited)

    # After the prim exists: the transform is authored onto it, facing Aim At if set.
    _aim(node, stage)
    _author_camera_transform(node, stage)
    _refocus(node)

    if node.get_param("lock_to_freecam").value:
        scene = node.get_scene()
        prim = stage.GetPrimAtPath(node.get_param("primpath_location").value)
        if prim.IsValid():
            setattr(node, _HOLDS_VIEW, True)
            scene.set_viewport_camera(prim)


def config():
    return chunk.DefaultConfig(
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
        icon="camera",
        description="""
        Camera chunk — creates a UsdGeom.Camera prim and exposes its lens
        parameters as live sliders, with optional real-world camera presets
        and freecam lock for DCC viewport alignment.

        Parameters are grouped into collapsed Lens, Sensor, Focus, Clipping and
        Transform sections. Shift+F in the viewport turns the view to face the selection.

        Parameters:
          - Prim Path: stage path for the camera prim. Auto-initialised to a
            unique /cameras/camera1 path. Updated in-place when taken (outside
            variant contexts).
          - Camera Preset: real-world camera body fetched from physicallybased.info
            (cached per user as usd_chunks/cameras.json). Defaults to Fuji X-T5. Changing
            the preset repopulates the Formats menu and applies the first sensor
            size as the default aperture values.
          - Formats: sensor format for the selected camera body. Changing the
            format updates Focal Length, Horizontal Aperture, and Vertical
            Aperture to match the physical sensor dimensions.
          - Focal Length / Apertures / Offsets / f-Stop / Focus Distance: USD
            camera attributes wired as live sliders via connect_usd_attr, so
            slider changes write directly to the prim attribute.
          - Clipping Range: Near and Far sliders driving the camera's float2
            `clippingRange` attribute, wired the same way.
          - Aim At: a prim to keep the camera pointed at. Each cook turns the
            Rotate params to face its centre from the camera's position, level to
            the stage's up axis, so moving the camera or the prim keeps it framed.
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
