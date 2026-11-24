"""A new Camera chunk's lens and sensor are on the camera prim, not only in its params.

The params are bound to the prim with connect_usd_attr, which pushes a value only once a param
holds one of its own. A param still at its default holds none, so the default preset's sensor
(an X-T5: 23.5 x 15.7 mm) never reached the prim: it kept the schema's 20.955 x 15.2908 and
framed wider than the chunk showed. The focal length looked right only because the default
(50) is also the schema's.
"""

from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene

LENS = ("focalLength", "horizontalAperture", "verticalAperture", "fStop", "focusDistance")


def _prim(scene, camera):
    return UsdGeom.Camera(scene.get_stage().GetPrimAtPath(camera.get_param("primpath_location").value))


def _on_prim(scene, camera):
    prim = _prim(scene, camera)
    return {name: prim.GetPrim().GetAttribute(name).Get() for name in LENS}


def _in_params(camera):
    return {name: camera.get_param(name).value for name in LENS}


def test_a_new_cameras_sensor_is_on_the_prim() -> None:
    with Scene.new() as scene:
        camera = scene.create.camera()

        assert _on_prim(scene, camera) == pytest.approx(_in_params(camera))


def test_the_sensor_is_on_the_prim_after_a_reopen(tmp_path) -> None:
    path = str(tmp_path / "camera.usda")
    with Scene.new() as scene:
        scene.create.camera()
        scene.save(path)

    with Scene.open(path) as scene:
        camera = next(c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "camera")
        assert _on_prim(scene, camera) == pytest.approx(_in_params(camera))


def test_an_edited_lens_still_drives_the_prim() -> None:
    with Scene.new() as scene:
        camera = scene.create.camera()

        scene.set(camera, horizontalAperture=36.0, focalLength=85.0)

        prim = _prim(scene, camera)
        assert prim.GetHorizontalApertureAttr().Get() == pytest.approx(36.0)
        assert prim.GetFocalLengthAttr().Get() == pytest.approx(85.0)
