"""A new Camera starts on the X-T5 preset without a warning on the way.

The preset menu was built with its items and no default, so it started at "", which is not
an item: every new Camera, and every Camera in a file being opened, logged
"camera_preset: '' is not one of [...]; using 'Iphone 13 Pro'" before main() set the X-T5.
The camera itself came out right; the warning read like a wrong camera body.
"""

from __future__ import annotations

import logging

from usd_chunks import Scene


def test_a_new_camera_logs_no_preset_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING), Scene.new() as scene:
        camera = scene.create.camera()
        assert camera.get_param("camera_preset").value == "X-T5"

    assert not [r for r in caplog.records if "is not one of" in r.getMessage()]


def test_the_preset_comes_back_after_a_reopen(tmp_path, caplog) -> None:
    path = str(tmp_path / "camera.usda")
    with Scene.new() as scene:
        scene.create.camera()
        scene.save(path)

    with caplog.at_level(logging.WARNING), Scene.open(path) as scene:
        camera = next(c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "camera")
        assert camera.get_param("camera_preset").value == "X-T5"
    assert not [r for r in caplog.records if "is not one of" in r.getMessage()]


def test_another_preset_survives_a_reopen(tmp_path) -> None:
    path = str(tmp_path / "camera.usda")
    with Scene.new() as scene:
        camera = scene.create.camera()
        scene.set(camera, camera_preset="Alexa 35")
        scene.save(path)

    with Scene.open(path) as scene:
        camera = next(c for c in scene.get_chunks() if c.get_config() and c.get_config().module == "camera")
        assert camera.get_param("camera_preset").value == "Alexa 35"
