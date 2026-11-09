"""A Camera keeps the lens a script gives it; only a change of preset sets the lens.

The Camera Preset and Formats menus put a camera body's focal length and apertures on the
lens. Their notices also arrive when a camera is made inside a change_block, after the
script's own values were set, and they put the preset's 50 mm back over a scripted
focalLength=35.
"""
from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


def _focal(scene, path):
    return scene.get_stage().GetPrimAtPath(path).GetAttribute("focalLength").Get()


@pytest.mark.parametrize("in_block", [False, True])
def test_a_scripted_lens_is_kept(scene, in_block) -> None:
    def make():
        return scene.create.camera(
            primpath_location="/cam", focalLength=35.0, horizontalAperture=36.0, verticalAperture=24.0,
        )

    if in_block:
        with chunk.change_block():
            cam = make()
    else:
        cam = make()
    scene.set_output(cam)

    values = [cam.get_param(n).value for n in ("focalLength", "horizontalAperture", "verticalAperture")]
    assert values == pytest.approx([35.0, 36.0, 24.0])
    assert _focal(scene, "/cam") == pytest.approx(35.0)


def test_a_new_camera_has_the_default_body_lens(scene) -> None:
    cam = scene.create.camera(primpath_location="/cam")
    presets = cam.get_param("camera_preset")
    body = next(c for c in presets.cameras if c.get("model") == presets.value)
    sensor = body["sensorSize"][0]

    assert cam.get_param("focalLength").value == pytest.approx(sensor.get("focalLength", [50.0])[0])
    assert cam.get_param("horizontalAperture").value == pytest.approx(sensor["size"][0])
    assert cam.get_param("verticalAperture").value == pytest.approx(sensor["size"][1])


def test_choosing_another_body_still_sets_the_lens(scene) -> None:
    with chunk.change_block():
        cam = scene.create.camera(primpath_location="/cam", focalLength=35.0)
    scene.set_output(cam)
    presets = cam.get_param("camera_preset")
    other = next(c for c in presets.cameras if c.get("model") != presets.value and c.get("sensorSize"))
    sensor = other["sensorSize"][0]

    scene.set(cam, camera_preset=other["model"])

    assert cam.get_param("horizontalAperture").value == pytest.approx(sensor["size"][0])
    assert cam.get_param("verticalAperture").value == pytest.approx(sensor["size"][1])
    assert cam.get_param("focalLength").value == pytest.approx(sensor.get("focalLength", [50.0])[0])
