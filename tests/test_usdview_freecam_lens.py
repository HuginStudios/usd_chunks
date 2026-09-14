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

from usd_chunks.plugins.chunk_usdview import UsdviewScene  # noqa: E402


class _FreeCamera:
    """Only what the reconcile reads and writes. usdview's `fov` setter emits on every write,
    which is why the real one is corrected before the frustum signal is connected."""

    def __init__(self, fov):
        self.fov = fov
        self.writes = 0

    def __setattr__(self, name, value):
        if name == "fov" and "fov" in self.__dict__:
            object.__setattr__(self, "writes", self.writes + 1)
        object.__setattr__(self, name, value)


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
    s._free_camera_fov = None
    s._restore_lens = False
    return s


def test_the_users_own_lens_is_remembered_while_nothing_of_ours_is_viewed(scene) -> None:
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))

    assert scene._free_camera_fov == pytest.approx(60.0)


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

    assert scene._free_camera_fov == pytest.approx(60.0)


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
    assert scene._free_camera_fov == pytest.approx(35.0), "the user's new choice is remembered"


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

    assert scene._free_camera_fov == pytest.approx(35.0)


def test_a_lens_that_already_agrees_is_not_written_on_release(scene) -> None:
    """usdview's fov setter emits regardless, and an unnecessary emit is an unnecessary cook."""
    scene._free_camera_fov = 60.0
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


def test_a_free_camera_without_a_fov_does_not_raise(scene) -> None:
    """This sits on the view-settings path, which runs on every setting change. Raising here
    would break far more than a lens."""
    class _Bare:
        pass

    scene._reconcile_free_camera_lens(_Bare())
    assert scene._free_camera_fov is None

    scene._snap_prim = "/cameras/camera1"
    scene._free_camera_fov = 60.0
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
    s._free_camera_fov = None
    s._restore_lens = False
    s.usdviewAPI = _Api(camera_prim)
    s._get_free_camera = lambda: free_cam
    return s


def test_the_snapshot_refreshes_while_the_user_is_on_their_own_camera() -> None:
    """Otherwise the remembered lens is frozen at whatever it was before the first lock, and
    a user who later picks a different navigation lens never gets it back."""
    s = _snapping_scene(camera_prim=None, free_cam=_FreeCamera(35.0))

    s._remember_free_camera_lens()

    assert s._free_camera_fov == pytest.approx(35.0)


def test_the_snapshot_is_not_taken_while_a_camera_prim_is_in_view() -> None:
    """The lock re-asserts the prim after every move, so this runs repeatedly while a chunk
    camera is up -- and by then the free camera is a copy of that camera. Remembering it would
    bake the leak in permanently instead of undoing it."""
    s = _snapping_scene(camera_prim="/cameras/camera1", free_cam=_FreeCamera(15.15))
    s._free_camera_fov = 60.0

    s._remember_free_camera_lens()

    assert s._free_camera_fov == pytest.approx(60.0), "the user's lens, not the chunk camera's"


def test_the_snapshot_is_not_taken_mid_navigation_while_our_prim_holds_the_lock() -> None:
    """During a drag usdview has switched to a free camera, so cameraPrim reads None -- but
    that camera was built from our prim and wears its lens. With nothing putting the user's
    lens back per move any more, remembering here would remember the prim's."""
    s = _snapping_scene(camera_prim=None, free_cam=_FreeCamera(15.15))
    s._free_camera_fov = 60.0
    s._snap_prim = "/cameras/camera1"

    s._remember_free_camera_lens()

    assert s._free_camera_fov == pytest.approx(60.0)


def test_a_host_that_cannot_answer_does_not_raise() -> None:
    class _Broken:
        @property
        def dataModel(self):
            raise RuntimeError("no app")

    s = UsdviewScene.__new__(UsdviewScene)
    s._snap_prim = None
    s._free_camera_fov = None
    s._restore_lens = False
    s.usdviewAPI = _Broken()

    s._remember_free_camera_lens()      # must not raise
    assert s._free_camera_fov is None
