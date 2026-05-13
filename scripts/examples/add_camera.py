import re
from usd_chunks import chunk, chunk_notice, chunk_core, chunk_utils
from pxr import UsdGeom
import http.client
import pprint
import json
import os
from functools import partial


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
    sensor_size = cam_data.get("sensorSize")
    formats = _chunk.get_param("formats")
    format_list = [format.get("format") for format in sensor_size]
    formats.menu_items = format_list
    set_preset_values(owner, notice, sender)


def set_preset_values(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    camera_presets = _chunk.get_param("camera_preset")
    selected_model = camera_presets.value

    cam_data = next((c for c in camera_presets.cameras if c.get("model") == selected_model), None)
    # pprint.pprint(cam_data)

    focal_param = _chunk.get_param("focalLength")
    sensor_size = cam_data.get("sensorSize")
    formats = _chunk.get_param("formats")
    horizontal_param = _chunk.get_param("horizontalAperture")
    vertical_param = _chunk.get_param("verticalAperture")

    focal_param.value = sensor_size[formats.get_selected_index()].get("focalLength", [50.0])[0]
    horizontal_param.value = sensor_size[formats.get_selected_index()].get("size")[0]
    vertical_param.value = sensor_size[formats.get_selected_index()].get("size")[1]


def _apply_freecam_transform(_chunk):
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
    scene.schedule_viewport_camera(prim)


def _on_lock_changed(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    scene = _chunk.get_scene()
    if param.value:

        _cb = partial(_apply_freecam_transform, _chunk)
        _chunk._freecam_callback = _cb
        scene.connect_active_camera_changed(_cb)
        _apply_freecam_transform(_chunk)
    else:
        freecam_cb_attr = getattr(_chunk, "_freecam_callback", None)
        if freecam_cb_attr:
            scene.disconnect_active_camera_changed(freecam_cb_attr)
            _chunk._freecam_callback = None


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
    primpath.add_callback(execute)

    # add camera type
    cameras = _get_camera_data()
    cam_presets = [cam.get("model") for cam in cameras]
    menu = chunk.Menu("camera_preset", "Camera Preset", menu_items=cam_presets)
    menu.cameras = cameras
    # Default to Fuji X-T5.
    menu.value = "X-T5"
    menu.add_callback(set_format_list)
    chunk_api.add_param(menu)
    formats = chunk.Menu("formats", "Formats")
    formats.add_callback(set_preset_values)
    chunk_api.add_param(formats)

    default_cam = next((c for c in cameras if c.get("model") == menu.value), cameras[0])
    formats.menu_items = [f.get("format") for f in default_cam.get("sensorSize", [])]

    chunk_api.add_param(chunk.Slider("focalLength", "Focal Length", min_value=1.0, max_value=800.0))
    chunk_api.add_param(chunk.Slider("horizontalAperture", "Horizontal Aperture", min_value=1.0, max_value=100.0))
    chunk_api.add_param(chunk.Slider("horizontalApertureOffset", "Horizontal Aperture Offset", min_value=-50.0, max_value=50.0))
    chunk_api.add_param(chunk.Slider("verticalAperture", "Vertical Aperture", min_value=1.0, max_value=100.0))
    chunk_api.add_param(chunk.Slider("verticalApertureOffset", "Vertical Aperture Offset", min_value=-50.0, max_value=50.0))
    chunk_api.add_param(chunk.Slider("fStop", "f-Stop", min_value=0.7, max_value=64.0))
    chunk_api.add_param(chunk.Slider("focusDistance", "Focus Distance", min_value=0.0, max_value=1000.0))

    chunk_api.add_callback(execute, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)
    lock_cam = chunk.Checkbox("lock_to_freecam", "Lock to Free Camera")
    lock_cam.add_callback(_on_lock_changed)
    chunk_api.add_param(lock_cam)

    return chunk_api


def execute(owner, notice, sender):
    param = notice.data
    _chunk = param.get_chunk()
    stage = owner.get_stage()
    # Clear any edits in this layer before importing a reference again.
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()
    prim_path = _chunk.get_param("primpath_location").value

    prim_path = chunk_utils.get_unique_path(stage, prim_path)
    prim = stage.DefinePrim(prim_path, "Camera")

    focal_attr = prim.GetAttribute("focalLength")
    focal_param = _chunk.get_param("focalLength")
    focal_param.connect_usd_attr(focal_attr)

    _chunk.get_param("horizontalAperture").connect_usd_attr(prim.GetAttribute("horizontalAperture"))
    _chunk.get_param("horizontalApertureOffset").connect_usd_attr(prim.GetAttribute("horizontalApertureOffset"))
    _chunk.get_param("verticalAperture").connect_usd_attr(prim.GetAttribute("verticalAperture"))
    _chunk.get_param("verticalApertureOffset").connect_usd_attr(prim.GetAttribute("verticalApertureOffset"))
    _chunk.get_param("fStop").connect_usd_attr(prim.GetAttribute("fStop"))
    _chunk.get_param("focusDistance").connect_usd_attr(prim.GetAttribute("focusDistance"))

    attr = UsdGeom.VisibilityAPI.Apply(prim).CreateGuideVisibilityAttr()
    attr.Set(UsdGeom.Tokens.inherited)

    if prim.GetPath().pathString != prim_path:
        _chunk.get_param("primpath_location").value = prim.GetPath().pathString

    if _chunk.get_param("lock_to_freecam").value:
        _apply_freecam_transform(_chunk)


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.EXAMPLES)
