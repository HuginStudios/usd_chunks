"""The free camera keeps its own lens across a chunk camera -- restored once, not per move.

Reported live, twice, from opposite directions:

First, the settings on a new Camera chunk transferred to the free camera. usdview cannot
tumble a camera prim, so any navigation while one is the viewport camera calls
`switchToFreeCamera()`, which builds a **new** FreeCamera with `FreeCamera.FromGfCamera()`
-- copying that prim's focal length and aperture. A 50mm on a 13.3mm aperture turned the
free camera into a ~15-degree telephoto to fly around in.

Then the fix for that made the lock pop. It put the user's lens back on every rebuild, and
with `lock_to_freecam` re-asserting the prim 150ms after every move, each navigation started
by zooming out to the user's lens and each pause ended with the snap zooming back in to the
prim's. While the lock holds, the free camera IS the chunk camera being flown; the copy
usdview makes is the two agreeing, and it is left alone. The user's lens comes back once,
on the first rebuild after the lock is released.

usdview is not importable here, so the proxy is built without its constructor and driven with
stand-ins for the two objects it touches. What is asserted is the decision, not usdview's own
behaviour -- which is exactly the part that was ours to get right.
"""
from __future__ import annotations

import pytest

# chunk_usdview imports pxr.Usdviewq, which ships with a full USD build but not with the
# usd-core wheel CI installs. Without this the module errors at collection instead of
# skipping, taking the whole run down with it.
pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from pxr import Gf, Usd, UsdGeom  # noqa: E402

from usd_chunks.hosts import usdview as usdview_host  # noqa: E402
from usd_chunks.hosts.usdview import UsdviewScene  # noqa: E402


class _Signal:
    """A Qt signal, as far as the lens code uses one."""

    def __init__(self):
        self.emits = 0
        self.slots = []

    def emit(self):
        self.emits += 1

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)


class _FreeCamera:
    """usdview's FreeCamera, as far as the lens code goes: the lens lives on a Gf.Camera, and
    putting one back is announced on the frustum signals -- which is why the real one is
    corrected before the frustum signal is connected."""

    def __init__(self, fov, aspect=1.0, offsets=(0.0, 0.0)):
        self._camera = Gf.Camera()
        self._camera.SetPerspectiveFromAspectRatioAndFieldOfView(aspect, fov, Gf.Camera.FOVVertical)
        self._camera.horizontalApertureOffset, self._camera.verticalApertureOffset = offsets
        self.signalFrustumChanged = _Signal()
        self.signalFrustumSettingsChanged = _Signal()

    @property
    def fov(self):
        return self._camera.GetFieldOfView(Gf.Camera.FOVVertical)

    @fov.setter
    def fov(self, value):
        """As usdview's: the field of view changes, the frame keeps its shape and offsets."""
        self._camera.SetPerspectiveFromAspectRatioAndFieldOfView(self._camera.aspectRatio, value, Gf.Camera.FOVVertical)
        self.signalFrustumChanged.emit()
        self.signalFrustumSettingsChanged.emit()

    @property
    def writes(self):
        """How often a lens was put on it."""
        return self.signalFrustumChanged.emits

    def lens(self):
        cam = self._camera
        return (
            cam.focalLength,
            cam.horizontalAperture,
            cam.verticalAperture,
            cam.horizontalApertureOffset,
            cam.verticalApertureOffset,
            cam.fStop,
        )


def _lens_of(fov):
    """The lens of a free camera with this field of view and nothing else changed."""
    return _FreeCamera(fov).lens()


class _Timer:
    """The snap timer, as far as release_viewport_camera touches it."""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def scene():
    """The proxy without usdview's constructor, which needs a running app."""
    s = UsdviewScene.__new__(UsdviewScene)
    s._snap_prim = None
    s._free_camera_lens = None
    s._free_camera_clip = None
    s._restore_lens = False
    return s


def test_the_users_own_lens_is_remembered_while_nothing_of_ours_is_viewed(scene) -> None:
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))

    assert scene._free_camera_lens == _lens_of(60.0)


def test_a_rebuild_while_our_camera_is_viewed_is_left_wearing_its_lens(scene) -> None:
    """The lock is the user flying the chunk camera. usdview hands back a free camera wearing
    that camera's lens, and that is the lens they should be looking through -- putting the
    user's own back here is the zoom-out that made every navigation pop."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))     # the user's, remembered
    scene._snap_prim = "/cameras/camera1"                    # now we are viewing a prim

    rebuilt = _FreeCamera(15.15)                             # 50mm on a 13.3mm aperture
    scene._reconcile_free_camera_lens(rebuilt)

    assert rebuilt.fov == pytest.approx(15.15)
    assert rebuilt.writes == 0


def test_the_remembered_lens_survives_the_rebuilds_while_locked(scene) -> None:
    """The snapshot has to outlive the lock, or releasing it restores the telephoto and the
    leak is permanent."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))
    scene._snap_prim = "/cameras/camera1"

    for _ in range(3):
        scene._reconcile_free_camera_lens(_FreeCamera(15.15))

    assert scene._free_camera_lens == _lens_of(60.0)


def test_releasing_the_lock_puts_the_users_lens_back_once(scene) -> None:
    """The original report, fixed at the right moment: the first free camera usdview builds
    after our prim lets go still wears the prim's lens, and gets the user's back. The one
    after that is the user's own to change."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))
    scene._snap_prim = "/cameras/camera1"
    scene._reconcile_free_camera_lens(_FreeCamera(15.15))
    scene._camera_snap_timer = _Timer()

    scene.release_viewport_camera()
    assert scene._snap_prim is None
    assert scene._camera_snap_timer.stopped

    first = _FreeCamera(15.15)
    scene._reconcile_free_camera_lens(first)
    assert first.fov == pytest.approx(60.0)
    assert first.writes == 1

    later = _FreeCamera(35.0)
    scene._reconcile_free_camera_lens(later)
    assert later.writes == 0
    assert scene._free_camera_lens == _lens_of(35.0), "the user's new choice is remembered"


def test_release_is_idempotent(scene) -> None:
    """The camera chunk calls it from a callback that also fires on every camera move while
    the lock is off. A second release must not re-arm a restore that already happened."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))
    scene._snap_prim = "/cameras/camera1"
    scene._camera_snap_timer = _Timer()
    scene.release_viewport_camera()
    scene._reconcile_free_camera_lens(_FreeCamera(15.15))     # restored here

    scene.release_viewport_camera()
    scene._reconcile_free_camera_lens(_FreeCamera(35.0))

    assert scene._free_camera_lens == _lens_of(35.0)


def test_a_lens_that_already_agrees_is_not_written_on_release(scene) -> None:
    """Putting a lens back emits, and an unnecessary emit is an unnecessary cook."""
    scene._free_camera_lens = _lens_of(60.0)
    scene._restore_lens = True

    unchanged = _FreeCamera(60.0)
    scene._reconcile_free_camera_lens(unchanged)

    assert unchanged.writes == 0


def test_nothing_is_restored_before_anything_was_remembered(scene) -> None:
    """A camera prim viewed before we ever saw the user's free camera -- restoring a lens we
    never learned would be inventing one."""
    scene._snap_prim = "/cameras/camera1"
    scene._camera_snap_timer = _Timer()
    scene.release_viewport_camera()

    arrived = _FreeCamera(15.15)
    scene._reconcile_free_camera_lens(arrived)

    assert arrived.fov == pytest.approx(15.15)
    assert arrived.writes == 0


def test_a_free_camera_without_a_lens_does_not_raise(scene) -> None:
    """This sits on the view-settings path, which runs on every setting change. Raising here
    would break far more than a lens."""
    class _Bare:
        pass

    scene._reconcile_free_camera_lens(_Bare())
    assert scene._free_camera_lens is None

    scene._snap_prim = "/cameras/camera1"
    scene._free_camera_lens = _lens_of(60.0)
    scene._reconcile_free_camera_lens(_Bare())      # must not raise


# ---------------------------------------------------------------------------
# refreshing the snapshot
# ---------------------------------------------------------------------------

class _ViewSettings:
    def __init__(self, camera_prim=None):
        self.cameraPrim = camera_prim


class _DataModel:
    def __init__(self, camera_prim=None):
        self.viewSettings = _ViewSettings(camera_prim)


class _Api:
    def __init__(self, camera_prim=None):
        self.dataModel = _DataModel(camera_prim)


def _snapping_scene(camera_prim, free_cam):
    s = UsdviewScene.__new__(UsdviewScene)
    s._snap_prim = None
    s._free_camera_lens = None
    s._free_camera_clip = None
    s._restore_lens = False
    s.usdviewAPI = _Api(camera_prim)
    s._get_free_camera = lambda: free_cam
    return s


def test_the_snapshot_refreshes_while_the_user_is_on_their_own_camera() -> None:
    """Otherwise the remembered lens is frozen at whatever it was before the first lock, and
    a user who later picks a different navigation lens never gets it back."""
    s = _snapping_scene(camera_prim=None, free_cam=_FreeCamera(35.0))

    s._remember_free_camera_lens()

    assert s._free_camera_lens == _lens_of(35.0)


def test_the_snapshot_is_not_taken_while_a_camera_prim_is_in_view() -> None:
    """The lock re-asserts the prim after every move, so this runs repeatedly while a chunk
    camera is up -- and by then the free camera is a copy of that camera. Remembering it would
    bake the leak in permanently instead of undoing it."""
    s = _snapping_scene(camera_prim="/cameras/camera1", free_cam=_FreeCamera(15.15))
    s._free_camera_lens = _lens_of(60.0)

    s._remember_free_camera_lens()

    assert s._free_camera_lens == _lens_of(60.0), "the user's lens, not the chunk camera's"


def test_the_snapshot_is_not_taken_mid_navigation_while_our_prim_holds_the_lock() -> None:
    """During a drag usdview has switched to a free camera, so cameraPrim reads None -- but
    that camera was built from our prim and wears its lens. With nothing putting the user's
    lens back per move any more, remembering here would remember the prim's."""
    s = _snapping_scene(camera_prim=None, free_cam=_FreeCamera(15.15))
    s._free_camera_lens = _lens_of(60.0)
    s._snap_prim = "/cameras/camera1"

    s._remember_free_camera_lens()

    assert s._free_camera_lens == _lens_of(60.0)


def test_a_host_that_cannot_answer_does_not_raise() -> None:
    class _Broken:
        @property
        def dataModel(self):
            raise RuntimeError("no app")

    s = UsdviewScene.__new__(UsdviewScene)
    s._snap_prim = None
    s._free_camera_lens = None
    s._free_camera_clip = None
    s._restore_lens = False
    s.usdviewAPI = _Broken()

    s._remember_free_camera_lens()      # must not raise
    assert s._free_camera_lens is None


# ---------------------------------------------------------------------------
# clipping: the one thing usdview's copy of the prim does not carry
# ---------------------------------------------------------------------------
#
# FromGfCamera takes the prim's clippingRange along, but the next computeGfCamera resets
# near and far to usdview's defaults unless the free camera holds an override. So while
# our prim is viewed, the prim's range goes on as the override; on release the user's own
# overrides (or none) come back with their lens.

class _ClippingFreeCamera(_FreeCamera):
    """usdview's FreeCamera has override setters that emit nothing; None clears one."""

    def __init__(self, fov, near=None, far=None):
        super().__init__(fov)
        self.overrideNear = near
        self.overrideFar = far


#: The in-memory stages behind _camera_prim, kept alive: a prim dies with its stage.
_STAGES = []


def _camera_prim(near, far):
    stage = Usd.Stage.CreateInMemory()
    _STAGES.append(stage)
    cam = UsdGeom.Camera.Define(stage, "/cameras/camera1")
    cam.GetClippingRangeAttr().Set(Gf.Vec2f(near, far))
    return cam.GetPrim()


def test_a_rebuild_while_our_camera_is_viewed_wears_its_clipping(scene) -> None:
    scene._reconcile_free_camera_lens(_ClippingFreeCamera(60.0))
    scene._snap_prim = _camera_prim(0.1, 50.0)

    rebuilt = _ClippingFreeCamera(15.15)
    scene._reconcile_free_camera_lens(rebuilt)

    assert (rebuilt.overrideNear, rebuilt.overrideFar) == pytest.approx((0.1, 50.0))
    assert rebuilt.writes == 0, "the lens is still left as the copy made it"


def test_the_prims_current_clipping_is_read_on_every_rebuild(scene) -> None:
    """The sliders drive the prim live; each navigation's free camera takes what it says now."""
    prim = _camera_prim(0.1, 50.0)
    scene._snap_prim = prim
    scene._reconcile_free_camera_lens(_ClippingFreeCamera(15.15))

    UsdGeom.Camera(prim).GetClippingRangeAttr().Set(Gf.Vec2f(2.0, 900.0))
    rebuilt = _ClippingFreeCamera(15.15)
    scene._reconcile_free_camera_lens(rebuilt)

    assert (rebuilt.overrideNear, rebuilt.overrideFar) == pytest.approx((2.0, 900.0))


def test_releasing_the_lock_puts_the_users_clipping_back(scene) -> None:
    scene._reconcile_free_camera_lens(_ClippingFreeCamera(60.0, near=0.5, far=3000.0))
    scene._snap_prim = _camera_prim(0.1, 50.0)
    scene._reconcile_free_camera_lens(_ClippingFreeCamera(15.15))
    scene._camera_snap_timer = _Timer()

    scene.release_viewport_camera()
    first = _ClippingFreeCamera(15.15)
    scene._reconcile_free_camera_lens(first)

    assert (first.overrideNear, first.overrideFar) == pytest.approx((0.5, 3000.0))


def test_releasing_clears_the_prims_clipping_when_the_user_had_none(scene) -> None:
    """Even a user whose lens was never learned gets the override taken off again: it was ours,
    and left on it would clip their free camera at the prim's range for good."""
    scene._snap_prim = _camera_prim(0.1, 50.0)
    scene._camera_snap_timer = _Timer()
    scene.release_viewport_camera()

    arrived = _ClippingFreeCamera(15.15, near=0.1, far=50.0)
    scene._reconcile_free_camera_lens(arrived)

    assert (arrived.overrideNear, arrived.overrideFar) == (None, None)
    assert arrived.writes == 0, "a lens never learned is still not invented"


def test_the_users_clipping_is_remembered_with_their_lens() -> None:
    s = _snapping_scene(camera_prim=None, free_cam=_ClippingFreeCamera(35.0, near=0.25, far=800.0))

    s._remember_free_camera_lens()

    assert s._free_camera_clip == pytest.approx((0.25, 800.0))


def test_a_snap_prim_that_is_not_a_camera_puts_nothing_on(scene) -> None:
    scene._snap_prim = "/cameras/camera1"

    rebuilt = _ClippingFreeCamera(15.15)
    scene._reconcile_free_camera_lens(rebuilt)

    assert (rebuilt.overrideNear, rebuilt.overrideFar) == (None, None)


def test_a_snap_finds_a_camera_re_authored_before_it_lands(scene) -> None:
    """The snap lands 150ms after it is asked for. The Camera chunk's "Set as Viewport Camera"
    button cooks that chunk, which re-authors the camera prim and expires the handle it just
    passed -- the snap used to be dropped silently. It resolves the path instead."""
    stage = Usd.Stage.CreateInMemory()
    prim = UsdGeom.Camera.Define(stage, "/cameras/camera1").GetPrim()
    viewed = []
    scene.get_stage = lambda: stage
    scene._set_viewport_camera = lambda p: viewed.append(str(p.GetPath()))
    scene._snap_prim = prim
    scene._snap_path = prim.GetPath()

    stage.RemovePrim("/cameras/camera1")
    UsdGeom.Camera.Define(stage, "/cameras/camera1")
    assert not prim.IsValid()

    scene._on_snap_timer()

    assert viewed == ["/cameras/camera1"]


# ---------------------------------------------------------------------------
# unlocking: the free camera in view gets the user's whole lens back
# ---------------------------------------------------------------------------


@pytest.fixture
def next_tick(monkeypatch):
    """QTimer.singleShot without an event loop: calling the fixture runs what was queued."""
    queued = []
    monkeypatch.setattr(usdview_host.QtCore.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))

    def run():
        while queued:
            queued.pop(0)()

    return run


def _locked_camera():
    """The free camera usdview builds while the lock holds a chunk camera whose aperture was
    changed: a narrower view, a wider frame, and the frame shifted."""
    return _FreeCamera(22.62, aspect=1.6, offsets=(12.0, 6.0))


def _locked_scene(scene, users_camera):
    """`scene` with the user's lens remembered and a chunk camera locked into the view."""
    cameras = {"free": users_camera}
    scene.usdviewAPI = _Api(camera_prim=None)
    scene._get_free_camera = lambda: cameras["free"]
    scene._current_cam = None
    scene._camera_snap_timer = _Timer()
    scene._on_view_settings_changed()

    scene._snap_prim = "/cameras/camera1"
    scene.usdviewAPI.dataModel.viewSettings.cameraPrim = "/cameras/camera1"
    cameras["free"] = _locked_camera()  # navigated while locked
    scene._on_view_settings_changed()
    return cameras


def test_unlocking_puts_the_lens_back_on_the_free_camera_usdview_falls_back_to(scene, next_tick) -> None:
    """Viktor's ticket: lock a camera chunk, change its aperture, unlock, and the free camera
    kept the chunk camera's aperture. Unlocking cooks the chunk, the cook expires the prim, and
    usdview drops it and shows the free camera it built while the lock held. No new one is
    built, so the restore, waiting for one, never ran."""
    cameras = _locked_scene(scene, _FreeCamera(60.0))

    scene.release_viewport_camera()
    scene.usdviewAPI.dataModel.viewSettings.cameraPrim = None  # usdview drops the dead prim
    scene._on_view_settings_changed()
    next_tick()

    assert cameras["free"].lens() == _lens_of(60.0)


def test_unlocking_puts_back_the_whole_lens_not_only_the_field_of_view(scene) -> None:
    """The offsets and the frame's width are part of the lens too. Putting back only the field
    of view left the view shifted by the chunk camera's offsets."""
    _locked_scene(scene, _FreeCamera(60.0))

    scene.release_viewport_camera()
    rebuilt = _locked_camera()  # the first navigation after unlocking
    scene._reconcile_free_camera_lens(rebuilt)

    assert rebuilt.lens() == _lens_of(60.0)
    assert rebuilt.writes == 1


def test_a_navigation_after_unlocking_puts_the_lens_on_the_new_camera(scene, next_tick) -> None:
    """switchToFreeCamera clears the camera prim, which emits, before it hands its new free
    camera over. Spending the restore on the old one there would leave the new one wearing the
    chunk camera's lens, remembered as the user's."""
    cameras = _locked_scene(scene, _FreeCamera(60.0))
    scene.release_viewport_camera()

    scene.usdviewAPI.dataModel.viewSettings.cameraPrim = None
    scene._on_view_settings_changed()  # the prim cleared, the old camera still in
    cameras["free"] = _locked_camera()
    scene._on_view_settings_changed()  # the new camera handed over
    next_tick()

    assert cameras["free"].lens() == _lens_of(60.0)
    assert scene._free_camera_lens == _lens_of(60.0)


def test_nothing_is_put_back_while_the_view_is_still_through_the_prim(scene, next_tick) -> None:
    """Released, but not yet navigated: the free camera is not the one in view, and the one
    built on the first navigation is the one that needs the lens."""
    cameras = _locked_scene(scene, _FreeCamera(60.0))
    scene.release_viewport_camera()

    scene._on_view_settings_changed()
    next_tick()

    assert cameras["free"].writes == 0
    assert scene._restore_lens
