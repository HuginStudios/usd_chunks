"""usdview's free camera follows the stage's up axis when it changes on the open stage.

usdview reads the up axis once, when a stage opens, and builds its free camera Y-up or Z-up
from it. A Stage Settings chunk that changed the up axis afterwards left the camera tumbling
around the old axis until the file was opened again.

usdview's window is not built here: the scene proxy is made without its constructor and
handed stand-ins for the stage view and the app controller. The FreeCamera is usdview's own.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

# pxr.Usdviewq ships with a full USD build, not with the usd-core wheel CI installs.
pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from pxr import Gf, Usd, UsdGeom  # noqa: E402
from pxr.Usdviewq.freeCamera import FreeCamera  # noqa: E402

from usd_chunks.hosts import usdview as usdview_host  # noqa: E402
from usd_chunks.hosts.usdview import UsdviewScene  # noqa: E402


def _y_up_camera() -> FreeCamera:
    """A Y-up free camera looking at (1, 2, 3) from 10 units away, turned and tilted."""
    cam = FreeCamera(False)
    cam.center = Gf.Vec3d(1, 2, 3)
    cam.dist = 10.0
    cam.rotTheta = 30.0
    cam.rotPhi = 20.0
    return cam


def _eye(cam: FreeCamera) -> Gf.Vec3d:
    return cam.computeGfCamera(Gf.BBox3d()).frustum.position


def _close(a, b) -> bool:
    return (Gf.Vec3d(a) - Gf.Vec3d(b)).GetLength() < 1e-6


def test_the_camera_stays_put_and_tumbles_around_the_new_axis() -> None:
    cam = _y_up_camera()
    eye = _eye(cam)

    usdview_host._set_free_camera_up(cam, True)

    assert _close(_eye(cam), eye), "where it looks from"
    assert _close(cam.center, (1, 2, 3)), "what it looks at"
    frustum = cam.computeGfCamera(Gf.BBox3d()).frustum
    right = Gf.Cross(frustum.ComputeViewDirection(), frustum.ComputeUpVector())
    assert abs(right[2]) < 1e-6, "upright on Z: no roll"

    height = _eye(cam)[2]
    cam.rotTheta += 45.0  # a tumble: round the up axis, through the point it looks at
    assert _eye(cam)[2] == pytest.approx(height), "a tumble keeps its height on Z"


class _View:
    """usdview's StageView, as far as the up axis goes."""

    def __init__(self, is_z_up: bool):
        self._stageIsZup = is_z_up
        self.updates = 0

    def update(self):
        self.updates += 1


def _scene(stage: Usd.Stage, view: _View, cam: FreeCamera) -> UsdviewScene:
    """The proxy without usdview's constructor, over a stand-in app controller."""
    scene = UsdviewScene.__new__(UsdviewScene)
    ctrl = SimpleNamespace(_stageView=view, _dataModel=SimpleNamespace(viewSettings=SimpleNamespace(freeCamera=cam)))
    scene.usdviewAPI = SimpleNamespace(stage=stage, _UsdviewApi__appController=ctrl)
    scene.call_later = lambda fn, delay=0.0: fn()  # no Qt event loop here
    scene._listen_to_stage(stage)
    return scene


def test_changing_the_up_axis_turns_the_free_camera() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    view, cam = _View(False), _y_up_camera()
    scene = _scene(stage, view, cam)  # noqa: F841 - held: the listener goes with it

    # What a Stage Settings chunk does with no Edit Target upstream.
    stage.GetSessionLayer().pseudoRoot.SetInfo(UsdGeom.Tokens.upAxis, UsdGeom.Tokens.z)

    assert view._stageIsZup is True, "the free cameras usdview builds from now on are Z-up"
    assert cam._isZUp is True, "the one in view is turned too"
    assert view.updates == 1

    stage.GetSessionLayer().pseudoRoot.SetInfo(UsdGeom.Tokens.upAxis, UsdGeom.Tokens.y)
    assert view._stageIsZup is False and cam._isZUp is False, "and back"


def test_other_edits_leave_the_camera_alone() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    view, cam = _View(False), _y_up_camera()
    scene = _scene(stage, view, cam)  # noqa: F841 - held: the listener goes with it

    UsdGeom.Xform.Define(stage, "/World")
    stage.GetSessionLayer().pseudoRoot.SetInfo(UsdGeom.Tokens.metersPerUnit, 0.01)

    assert view._stageIsZup is False and cam._isZUp is False
    assert view.updates == 0


def test_a_new_stage_is_watched_instead_of_the_old_one() -> None:
    old, new = Usd.Stage.CreateInMemory(), Usd.Stage.CreateInMemory()
    view, cam = _View(False), _y_up_camera()
    scene = _scene(old, view, cam)
    scene.usdviewAPI.stage = new
    scene._listen_to_stage(new)

    old.GetSessionLayer().pseudoRoot.SetInfo(UsdGeom.Tokens.upAxis, UsdGeom.Tokens.z)
    assert view.updates == 0, "the stage that was closed"

    new.GetSessionLayer().pseudoRoot.SetInfo(UsdGeom.Tokens.upAxis, UsdGeom.Tokens.z)
    assert view._stageIsZup is True
