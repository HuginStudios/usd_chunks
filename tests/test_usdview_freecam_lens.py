"""The free camera keeps its own lens when a chunk camera is being viewed.

Reported live: the settings on a new Camera chunk transferred to the free camera. Not once --
on every navigation, because it is a loop:

1. `lock_to_freecam` snaps the viewport to look through the camera prim.
2. The user navigates. usdview cannot tumble a camera prim, so it calls
   `switchToFreeCamera()`, which builds a **new** FreeCamera with `FreeCamera.FromGfCamera()`
   -- copying that prim's focal length and aperture.
3. Our proxy sees the new free camera and rewires the frustum signal.
4. That fires the lock, which writes the prim and re-arms the snap. Back to 1.

So a 50mm on a 13.3mm aperture turned the free camera into a ~15-degree telephoto to fly
around in, and each navigation did it again.

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


@pytest.fixture
def scene():
    """The proxy without usdview's constructor, which needs a running app."""
    s = UsdviewScene.__new__(UsdviewScene)
    s._snap_prim = None
    s._free_camera_fov = None
    return s


def test_the_users_own_lens_is_remembered_while_nothing_of_ours_is_viewed(scene) -> None:
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))

    assert scene._free_camera_fov == pytest.approx(60.0)


def test_a_rebuild_while_our_camera_is_viewed_gets_the_lens_put_back(scene) -> None:
    """The reported case. usdview hands back a free camera wearing the chunk camera's lens."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))     # the user's, remembered
    scene._snap_prim = "/cameras/camera1"                    # now we are viewing a prim

    rebuilt = _FreeCamera(15.15)                             # 50mm on a 13.3mm aperture
    scene._reconcile_free_camera_lens(rebuilt)

    assert rebuilt.fov == pytest.approx(60.0)
    assert rebuilt.writes == 1


def test_the_remembered_lens_is_not_overwritten_by_the_rebuild(scene) -> None:
    """The snapshot has to survive, or the second navigation remembers the telephoto and the
    leak becomes permanent instead of per-move."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))
    scene._snap_prim = "/cameras/camera1"

    for _ in range(3):
        scene._reconcile_free_camera_lens(_FreeCamera(15.15))

    assert scene._free_camera_fov == pytest.approx(60.0)


def test_a_lens_that_already_agrees_is_not_written(scene) -> None:
    """usdview's fov setter emits regardless, and an unnecessary emit is an unnecessary cook."""
    scene._free_camera_fov = 60.0
    scene._snap_prim = "/cameras/camera1"

    unchanged = _FreeCamera(60.0)
    scene._reconcile_free_camera_lens(unchanged)

    assert unchanged.writes == 0


def test_nothing_is_restored_before_anything_was_remembered(scene) -> None:
    """A camera prim viewed before we ever saw the user's free camera -- restoring a lens we
    never learned would be inventing one."""
    scene._snap_prim = "/cameras/camera1"

    arrived = _FreeCamera(15.15)
    scene._reconcile_free_camera_lens(arrived)

    assert arrived.fov == pytest.approx(15.15)
    assert arrived.writes == 0


def test_releasing_the_snap_lets_the_user_change_their_lens_again(scene) -> None:
    """Once nothing of ours is being viewed, the free camera is the user's again and whatever
    they set is the new thing to remember -- otherwise we would fight them forever."""
    scene._reconcile_free_camera_lens(_FreeCamera(60.0))
    scene._snap_prim = "/cameras/camera1"
    scene._reconcile_free_camera_lens(_FreeCamera(15.15))

    scene._snap_prim = None
    scene._reconcile_free_camera_lens(_FreeCamera(35.0))

    assert scene._free_camera_fov == pytest.approx(35.0)


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


def test_a_host_that_cannot_answer_does_not_raise() -> None:
    class _Broken:
        @property
        def dataModel(self):
            raise RuntimeError("no app")

    s = UsdviewScene.__new__(UsdviewScene)
    s._snap_prim = None
    s._free_camera_fov = None
    s.usdviewAPI = _Broken()

    s._remember_free_camera_lens()      # must not raise
    assert s._free_camera_fov is None
