"""The camera chunk's Near/Far sliders drive the prim's float2 `clippingRange`."""

from __future__ import annotations

import pytest
from pxr import Gf, UsdGeom

from test_camera_focus_on_selection import _camera_scene


def _clipping(stage, camera):
    prim = stage.GetPrimAtPath(camera.get_param("primpath_location").value)
    return UsdGeom.Camera(prim).GetClippingRangeAttr().Get()


def test_defaults_match_the_schema(qapp) -> None:
    stage, _, camera = _camera_scene(qapp)

    assert camera.get_param("clippingRange").value == (1.0, 1000000.0)
    assert _clipping(stage, camera) == Gf.Vec2f(1.0, 1000000.0)


def test_each_slider_writes_its_half(qapp) -> None:
    stage, _, camera = _camera_scene(qapp)

    camera.get_param("Near").value = 0.5
    qapp.processEvents()
    assert _clipping(stage, camera) == pytest.approx(Gf.Vec2f(0.5, 1000000.0))

    camera.get_param("Far").value = 250.0
    qapp.processEvents()
    assert _clipping(stage, camera) == pytest.approx(Gf.Vec2f(0.5, 250.0))


def test_the_vector_writes_whole(qapp) -> None:
    stage, _, camera = _camera_scene(qapp)

    camera.get_param("clippingRange").value = (2.0, 5000.0)
    qapp.processEvents()
    assert _clipping(stage, camera) == pytest.approx(Gf.Vec2f(2.0, 5000.0))
