"""A value given to scene.create is kept, even when the chunk fills a menu or makes a param later.

The Edit Target's menu lists the stage's layers, and it only gets them once the chunk is
in the scene. A value set before that was lost, so `edit_target="shot"` fell back to
"Session Layer" and nothing went into the shot layer.
"""

from __future__ import annotations

import pytest
from pxr import Sdf

from usd_chunks import Scene


def _layers_holding(stage, prim_path):
    """Display names of the layers, top down, on the way to the one with prim_path."""

    def walk(layer, trail):
        trail = trail + [layer.GetDisplayName()]
        if layer.GetPrimAtPath(prim_path):
            return trail
        for sub in layer.subLayerPaths:
            found = Sdf.Layer.Find(sub)
            if found and (hit := walk(found, trail)):
                return hit
        return None

    return walk(stage.GetSessionLayer(), []) or walk(stage.GetRootLayer(), []) or []


def test_edit_target_given_to_create_is_kept() -> None:
    with Scene() as scene:
        shot = scene.create.layer(layer_name="shot")
        target = scene.create.edit_target(shot, edit_target="shot")
        assert target.get_param("edit_target").value == "shot"


def test_chunks_after_the_edit_target_write_into_its_layer() -> None:
    with Scene() as scene:
        shot = scene.create.layer(layer_name="shot")
        with scene.create.edit_target(shot, edit_target="shot"):
            scene.create.ground(prim_path="/set/ground")

        assert "shot" in _layers_holding(scene.get_stage(), "/set/ground")


def test_a_param_built_when_the_chunk_joins_the_scene_is_set() -> None:
    """Light makes its params only once it is in the scene."""
    with Scene() as scene:
        scene.create.rect_light(primpath_location="/set/key", intensity=5.0)

        light = scene.get_stage().GetPrimAtPath("/set/key")
        assert light.GetAttribute("inputs:intensity").Get() == 5.0


def test_a_name_no_param_has_still_raises() -> None:
    with Scene() as scene:
        with pytest.raises(KeyError, match="no_such_param"):
            scene.create.transform(no_such_param=1.0)
