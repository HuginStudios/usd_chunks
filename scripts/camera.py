import os
import json
import http.client
from pxr import UsdGeom
from usd_chunks import chunk, chunk_notice, chunk_utils


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
    print(camera_cache)
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
    do_lock = _chunk.get_param("lock_to_freecam").value
    if not do_lock:
        return

    scene = _chunk.get_scene()
    gf_cam = scene.get_active_camera()
    if gf_cam is None:
        return
    stage = scene.get_stage()
    prim_path = _chunk.get_param("primpath_location").value
    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        return
    UsdGeom.Xformable(prim).MakeMatrixXform().Set(gf_cam.transform)
    if not prim.GetAttribute("focusDistance").Get():
        _chunk.get_param("focusDistance").value = gf_cam.focusDistance
    scene.set_viewport_camera(prim)


def _set_as_viewport_camera(owner, notice, sender):
    _chunk = owner.get_chunk()
    scene = _chunk.get_scene()
    stage = _chunk.get_stage()
    prim_path = _chunk.get_param("primpath_location").value
    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        return
    scene.set_viewport_camera(prim)


def main():
    chunk_api = chunk.Chunk("Camera")

    # prim location
    stage = chunk_api.get_stage()
    prim_path = chunk_utils.get_unique_path(stage, "/cameras/camera1")
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
    
    default_cam = next((c for c in cameras if c.get("model") == menu.value), cameras[0])
    formats.menu_items = [f.get("format") for f in default_cam.get("sensorSize", [])]

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

    if raw_path == getattr(node, '_last_camera_path', None):
        return

    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    _, _, var_selections = chunk_utils.split_variant_path(raw_path)
    if not var_selections and not chunk_notice._loading_stage:
        raw_path = chunk_utils.get_unique_path(stage, raw_path)

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
                        axis="Z", driver="focusDistance"),
        ],
        category=chunk.StandardCategory.EXAMPLES,
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
