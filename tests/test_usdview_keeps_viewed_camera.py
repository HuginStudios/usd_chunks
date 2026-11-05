"""Looking through a camera survives a cook that writes the camera again.

Changing fStop on a Camera chunk threw usdview's viewport back to the free camera. The cook
clears the chunk's layer and writes the camera again; for that moment there is no prim at
the camera's path, and usdview, re-checking its camera on the stage notice, lets it go.

Checked for real in testusdview (fStop and focal length changes keep /cams/shot in view).
Here the decision is checked with a stand-in for usdview's view settings.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")

from pxr import Usd, UsdGeom  # noqa: E402

from usd_chunks.hosts import usdview as usdview_host  # noqa: E402
from usd_chunks.hosts.usdview import UsdviewScene  # noqa: E402


class _ViewSettings:
    """usdview's view settings: the camera is kept by path, looked up fresh on each read."""

    def __init__(self, stage):
        self._stage = stage
        self.camera_path = None

    @property
    def cameraPrim(self):
        return self._stage.GetPrimAtPath(self.camera_path) if self.camera_path else None

    @cameraPrim.setter
    def cameraPrim(self, prim):
        self.camera_path = prim.GetPath() if prim is not None else None


@pytest.fixture
def host(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Camera.Define(stage, "/cams/shot")
    settings = _ViewSettings(stage)
    scene = UsdviewScene.__new__(UsdviewScene)
    scene.usdviewAPI = types.SimpleNamespace(dataModel=types.SimpleNamespace(viewSettings=settings))
    scene._snap_prim = None
    scene._viewed_camera = None
    scene.get_stage = lambda: stage
    scene._set_viewport_camera = lambda prim: setattr(settings, "cameraPrim", prim)
    later = []
    monkeypatch.setattr(usdview_host.QtCore.QTimer, "singleShot", lambda _ms, fn: later.append(fn))
    return scene, stage, settings, later


def _look_through(scene, settings, prim):
    settings.cameraPrim = prim
    scene._track_viewed_camera()


def test_a_camera_written_again_by_a_cook_stays_in_view(host) -> None:
    scene, stage, settings, later = host
    _look_through(scene, settings, stage.GetPrimAtPath("/cams/shot"))

    # The cook: the camera is gone for a moment, and usdview lets it go.
    stage.RemovePrim("/cams/shot")
    settings.cameraPrim = None
    scene._track_viewed_camera()
    UsdGeom.Camera.Define(stage, "/cams/shot")
    for fn in later:
        fn()

    assert settings.camera_path == "/cams/shot"


def test_picking_the_free_camera_is_left_alone(host) -> None:
    scene, stage, settings, later = host
    _look_through(scene, settings, stage.GetPrimAtPath("/cams/shot"))

    settings.cameraPrim = None
    scene._track_viewed_camera()
    for fn in later:
        fn()

    assert settings.camera_path is None


def test_a_deleted_camera_is_not_brought_back(host) -> None:
    scene, stage, settings, later = host
    _look_through(scene, settings, stage.GetPrimAtPath("/cams/shot"))

    stage.RemovePrim("/cams/shot")
    settings.cameraPrim = None
    scene._track_viewed_camera()
    for fn in later:
        fn()

    assert settings.camera_path is None
