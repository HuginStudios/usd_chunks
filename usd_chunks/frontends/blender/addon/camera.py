"""The viewport camera, both ways: Blender's view out to chunks, a USD camera in to Blender.

**Out.** `viewport_camera()` reads the 3D view the user is navigating as a `Gf.Camera`, and
`_tick` sends CAMERA_CHANGED once that view has stopped moving. That is what a Camera
chunk's "Lock to Free Camera" listens for; usdview gets the same from its free camera.

**In.** `set_viewport_camera(prim)` mirrors a USD camera prim onto one Blender camera object
and looks through it. A mirror rather than driving the view's matrices directly: Blender's
camera view brings the frame border and passepartout, and F12 renders from the same camera.
The Hydra view (viewport.py) draws from Blender's view matrices, so it follows for free.

**Why a timer and not a draw handler.** Blender refuses ID writes inside a draw callback,
and the mirror is an ID. A notice sent from inside a draw would also cook mid-draw. The
timer reads the same matrices a draw would, and is the one place both directions run.

USD and Blender cameras both look down -Z with +Y up, so a camera's own frame needs no
conversion; only the world does. The Hydra view draws the stage through
`viewport.stage_to_blender` (a Y-up stage turned to stand up in Z-up Blender), so a prim's
world matrix crosses over through it, and the view goes back through its inverse.
"""
from __future__ import annotations

import logging

import bpy
from mathutils import Matrix
from pxr import Gf, Usd, UsdGeom

from usd_chunks.frontends.blender.addon import viewport

logger = logging.getLogger(__name__)

#: The one Blender object (and camera datablock) mirroring whichever prim is in view.
MIRROR_NAME = "USD Camera"

#: Seconds between ticks. Also the debounce: a view is "stopped" when two ticks agree.
_INTERVAL = 0.1

#: Mutable session state. Objects are looked up by name every tick rather than held: a
#: memfile undo frees and recreates ID datablocks, and a held reference would dangle.
_state = {
    "prim_path": None,      # the prim the mirror follows, or None when nothing is claimed
    "stage": None,          # the stage that prim lives on
    "prim_sig": None,       # what the mirror was last written from
    "pending_view": None,   # the view signature seen last tick
    "sent_view": None,      # the view signature CAMERA_CHANGED last went out for
    "previous_camera": None,  # scene.camera's name before the mirror took it
    "previous_perspective": None,  # the 3D view's view_perspective before CAMERA
    "scene_sender": None,   # the SceneSender to send CAMERA_CHANGED from
}


# ---------------------------------------------------------------------------
# Conversions -- pure, so they test without a window
# ---------------------------------------------------------------------------

def to_gf(matrix) -> Gf.Matrix4d:
    """mathutils (row-major, column vectors) to Gf (row vectors): a transpose."""
    return Gf.Matrix4d(*[v for row in matrix.transposed() for v in row])


def to_mathutils(matrix) -> Matrix:
    return Matrix([list(matrix.GetRow(i)) for i in range(4)]).transposed()


def gf_camera_from_view(view_matrix, window_matrix, lens, clip_start, clip_end,
                        is_perspective=True) -> Gf.Camera:
    """The 3D view as a Gf.Camera.

    Apertures come from the projection rather than from Blender's sensor constants, so they
    are right in camera view too, where the window matrix carries the camera's own lens:
    `window_matrix[0][0]` is `2 * focal / aperture` for a perspective projection and
    `2 / width` for an orthographic one. Gf apertures are in tenths of a scene unit.
    """
    camera = Gf.Camera()
    camera.transform = to_gf(view_matrix.inverted())
    camera.clippingRange = Gf.Range1f(clip_start, clip_end)
    p00, p11 = window_matrix[0][0], window_matrix[1][1]
    if is_perspective:
        camera.projection = Gf.Camera.Perspective
        camera.focalLength = lens
        camera.horizontalAperture = 2.0 * lens / p00
        camera.verticalAperture = 2.0 * lens / p11
    else:
        camera.projection = Gf.Camera.Orthographic
        camera.horizontalAperture = 2.0 / p00 / Gf.Camera.APERTURE_UNIT
        camera.verticalAperture = 2.0 / p11 / Gf.Camera.APERTURE_UNIT
    return camera


def prim_camera(prim, time=Usd.TimeCode.Default()):
    """(world matrix in Blender's space, Gf.Camera) for a camera prim, or None if it is not
    one."""
    if prim is None or not prim.IsValid() or not prim.IsA(UsdGeom.Camera):
        return None
    camera = UsdGeom.Camera(prim).GetCamera(time)
    world = viewport.stage_to_blender(prim.GetStage()) @ to_mathutils(camera.transform)
    return world, camera


def _to_stage_camera(camera: Gf.Camera) -> Gf.Camera:
    """A camera placed in Blender's world, placed in the open stage's instead."""
    back = viewport.stage_to_blender(viewport.active_stage()).inverted_safe()
    camera.transform = to_gf(back @ to_mathutils(camera.transform))
    return camera


def apply_to_object(obj, world, camera: Gf.Camera) -> None:
    """Write a USD camera onto a Blender camera object.

    Horizontal sensor fit, so the horizontal field of view matches whatever the render
    resolution's aspect -- the resolution is the user's and is left alone. Lens shift is a
    fraction of the fitted (horizontal) dimension, hence both offsets over the width.
    """
    data = obj.data
    obj.matrix_world = world
    h_ap = camera.horizontalAperture or 1.0
    data.sensor_fit = "HORIZONTAL"
    if camera.projection == Gf.Camera.Orthographic:
        data.type = "ORTHO"
        data.ortho_scale = h_ap * Gf.Camera.APERTURE_UNIT
    else:
        data.type = "PERSP"
        data.lens = camera.focalLength
        data.sensor_width = h_ap
        data.sensor_height = camera.verticalAperture or h_ap
    data.shift_x = camera.horizontalApertureOffset / h_ap
    data.shift_y = camera.verticalApertureOffset / h_ap
    clip = camera.clippingRange
    data.clip_start = max(clip.min, 1e-6)
    data.clip_end = max(clip.max, data.clip_start * 1.001)
    use_dof = camera.fStop > 0 and camera.focusDistance > 0
    data.dof.use_dof = use_dof
    if use_dof:
        data.dof.aperture_fstop = camera.fStop
        data.dof.focus_distance = camera.focusDistance


def _signature(world, camera: Gf.Camera) -> tuple:
    """Enough to tell whether a prim changed since the mirror was written."""
    clip = camera.clippingRange
    return (tuple(round(v, 6) for row in world for v in row), camera.projection,
            camera.focalLength, camera.horizontalAperture, camera.verticalAperture,
            camera.horizontalApertureOffset, camera.verticalApertureOffset,
            clip.min, clip.max, camera.fStop, camera.focusDistance)


def _view_signature(rv3d, space) -> tuple:
    """Where the view is. Only the placement and lens: entering camera view changes the
    window matrix (zoom, border) but not where the user is looking, and must not read as
    a move -- otherwise looking through a locked camera would announce itself forever.
    """
    return (tuple(round(v, 5) for row in rv3d.view_matrix for v in row),
            rv3d.is_perspective, round(space.lens, 4))


# ---------------------------------------------------------------------------
# Blender state
# ---------------------------------------------------------------------------

def _view3d():
    """(space, region_3d) of the largest 3D view, or (None, None) headless.

    The largest because there is no "active" 3D view outside an operator's context, and
    the big one is the one being looked at.
    """
    best, best_area = None, 0
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D" and area.width * area.height > best_area:
                best, best_area = area, area.width * area.height
    if best is None:
        return None, None
    space = best.spaces.active
    return space, space.region_3d


def viewport_camera() -> Gf.Camera | None:
    """The 3D view as a Gf.Camera in the stage's space (what a chunk authors with it); the
    scene camera headless; None with neither."""
    space, rv3d = _view3d()
    if rv3d is not None:
        return _to_stage_camera(gf_camera_from_view(
            rv3d.view_matrix, rv3d.window_matrix, space.lens,
            space.clip_start, space.clip_end, rv3d.is_perspective))
    scene = bpy.context.scene
    obj = scene.camera if scene else None
    if obj is None:
        return None
    camera = Gf.Camera()
    camera.transform = to_gf(obj.matrix_world)
    if obj.data.type == "PERSP":
        camera.focalLength = obj.data.lens
        camera.horizontalAperture = obj.data.sensor_width
        camera.verticalAperture = obj.data.sensor_height
    camera.clippingRange = Gf.Range1f(obj.data.clip_start, obj.data.clip_end)
    return _to_stage_camera(camera)


def mirror_object():
    """The mirror camera object, made (and linked into the scene) if it is missing."""
    obj = bpy.data.objects.get(MIRROR_NAME)
    if obj is None or obj.type != "CAMERA":
        data = bpy.data.cameras.get(MIRROR_NAME) or bpy.data.cameras.new(MIRROR_NAME)
        obj = bpy.data.objects.new(MIRROR_NAME, data)
        # Out of render-mode evaluation, so a Blender Import of the whole scene does not
        # export the mirror back onto the stage it mirrors. Rendering through it still works.
        obj.hide_render = True
    scene = bpy.context.scene
    if scene is not None and obj.name not in scene.collection.all_objects:
        scene.collection.objects.link(obj)
    return obj


def _sync_mirror() -> bool:
    """Rewrite the mirror if its prim changed -- or the frame did, for an animated
    camera. False when the prim is gone."""
    stage, path = _state["stage"], _state["prim_path"]
    found = (prim_camera(stage.GetPrimAtPath(path), viewport.current_time())
             if stage is not None and path else None)
    if found is None:
        return False
    sig = _signature(*found)
    obj = bpy.data.objects.get(MIRROR_NAME)
    if obj is None or sig != _state["prim_sig"]:
        apply_to_object(mirror_object(), *found)
        _state["prim_sig"] = sig
    return True


def set_viewport_camera(prim, scene_sender=None) -> None:
    """Mirror `prim` (a Usd.Prim or UsdGeom.Camera) and look through it."""
    prim = prim.GetPrim() if prim is not None else None
    found = prim_camera(prim, viewport.current_time())
    if found is None:
        return
    scene = bpy.context.scene
    obj = mirror_object()
    apply_to_object(obj, *found)
    if _state["prim_path"] is None and scene is not None:
        # Only on the first claim: a locked camera re-claims on every move, and by then
        # scene.camera is already the mirror.
        previous = scene.camera
        _state["previous_camera"] = previous.name if previous and previous != obj else None
    _state.update(prim_path=prim.GetPath(), stage=prim.GetStage(),
                  prim_sig=_signature(*found))
    if scene_sender is not None:
        _state["scene_sender"] = scene_sender
    if scene is not None:
        scene.camera = obj
    space, rv3d = _view3d()
    if rv3d is not None:
        if rv3d.view_perspective != "CAMERA":
            if _state["previous_perspective"] is None:
                _state["previous_perspective"] = rv3d.view_perspective
            rv3d.view_perspective = "CAMERA"
        # The camera view the snap lands on is where the user already was; not a move.
        _state["sent_view"] = _state["pending_view"] = _view_signature(rv3d, space)


def release_viewport_camera() -> None:
    """Stop following the prim, and hand back the camera and view the mirror replaced."""
    if _state["prim_path"] is None:
        return
    scene = bpy.context.scene
    previous = bpy.data.objects.get(_state["previous_camera"] or "")
    if scene is not None and scene.camera is not None and scene.camera.name == MIRROR_NAME:
        scene.camera = previous
    _space, rv3d = _view3d()
    if rv3d is not None and _state["previous_perspective"] and rv3d.view_perspective == "CAMERA":
        rv3d.view_perspective = _state["previous_perspective"]
    _state.update(prim_path=None, stage=None, prim_sig=None, previous_camera=None,
                  previous_perspective=None)


def look_at(point) -> None:
    """Turn the 3D view in place to face `point`, a point in the stage's world space.

    In place: the eye stays where it is. Blender orbits about `view_location` at
    `view_distance`, so the pivot moves onto the point and the distance takes up the
    difference. Out of camera view first, as usdview drops to its free camera.
    """
    _space, rv3d = _view3d()
    if rv3d is None:
        return
    eye = rv3d.view_matrix.inverted().translation
    target = viewport.to_blender(viewport.active_stage(), point)
    direction = target - eye
    if direction.length < 1e-9:
        return
    if rv3d.view_perspective == "CAMERA":
        rv3d.view_perspective = "PERSP"
    # The view's forward (-Z) onto the direction, its up (Y) towards Blender's world up.
    rv3d.view_rotation = direction.to_track_quat("-Z", "Y")
    rv3d.view_location = target
    rv3d.view_distance = direction.length


def _tick() -> float:
    try:
        if _state["prim_path"] is not None and not _sync_mirror():
            release_viewport_camera()
        space, rv3d = _view3d()
        if rv3d is None:
            return _INTERVAL
        sig = _view_signature(rv3d, space)
        settled = sig == _state["pending_view"]
        _state["pending_view"] = sig
        sender = _state["scene_sender"]
        if settled and sig != _state["sent_view"] and sender is not None:
            _state["sent_view"] = sig
            sender.activeCameraChangedCallback()
    except Exception:  # pragma: no cover - a timer that raises is unregistered for good
        logger.exception("usd_chunks: camera sync failed")
    return _INTERVAL


def attach(scene_sender) -> None:
    """The SceneSender CAMERA_CHANGED goes out from. One per session (see _open_stage)."""
    _state["scene_sender"] = scene_sender
    _state["sent_view"] = _state["pending_view"]  # the view at open is not a move


def register() -> None:
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=_INTERVAL, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _state.update(prim_path=None, stage=None, prim_sig=None, scene_sender=None)
