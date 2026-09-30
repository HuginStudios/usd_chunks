"""Making a Layer or an Edit Target logs no error.

An on-create callback is triggered with the notice class, not a notice. The check that
skips notices from other stages called GetStage on that class, which raised, so every
Layer and Edit Target made logged a traceback.
"""

from __future__ import annotations

import logging

from usd_chunks import Scene


def test_layer_and_edit_target_create_without_errors(caplog) -> None:
    with caplog.at_level(logging.ERROR), Scene() as scene:
        shot = scene.create.layer(layer_name="shot")
        scene.create.edit_target(shot)

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
