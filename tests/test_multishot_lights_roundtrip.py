"""A day/night multishot network survives a save and keeps switching on the shot.

    ShotContext (SHOT) -- Hood (shared) --+-- DayOnly --+-- NightOnly
                          Sun ------------+             |
                          Lamp ---------------------------+

The Hood is in every shot, the Sun is gated on *_day and the Lamp on *_night. Each
assertion reads the composed light prims, not chunk state.

What used to go wrong, in order: the Stage Expression declared nothing on the reopening
cook, so both gates shut; a light gated off at load came back at its schema default when
its gate opened (USD will not author into a muted layer); a variable changed after
reopening was saved without its value; and a shared parent prim (/lights) was defined only
by whichever light cooked first, so gating that one off took every light with it.
"""

from __future__ import annotations

import pathlib

import pytest
from pxr import Usd, UsdGeom, UsdLux

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as chunk_stage_initialize, save_scene



class _Staged(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _scene_for(stage: Usd.Stage) -> SceneSender:
    scene = _Staged(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage_initialize(stage, scene)
    return scene


def _add(scene, module, name):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module)
    c = (mod.create() if mod.loaded else mod.load())[0]
    c.name = name
    scene.add_chunk(c)
    return c


def _light(scene, name, path, **inputs):
    c = _add(scene, "light", name)
    c.get_param("primpath_location").value = path
    c.get_param("light_type").value = "SphereLight"
    for key, value in inputs.items():
        c.get_param(f"attr_grp.inputs.{key}").value = value
    return c


def _intensity(stage, path):
    """None unless the light is really in the scene -- an over-only prim is valid but not
    defined, and a renderer skips it."""
    prim = stage.GetPrimAtPath(path)
    return UsdLux.LightAPI(prim).GetIntensityAttr().Get() if prim and prim.IsDefined() else None


def _set_shot(scene, shot):
    shot_ctx = next(c for c in scene.get_chunks() if c.name == "ShotContext")
    rows = shot_ctx.get_param("variables")
    rows.get_indexed_param("var_value", 0).value = shot
    shot_ctx.execute()


def _assert_shot(stage, night):
    assert _intensity(stage, "/lights/hood") == pytest.approx(25.0), "the shared light is in every shot"
    assert _intensity(stage, "/lights/sun") == (None if night else pytest.approx(3.0))
    assert _intensity(stage, "/lights/lamp") == (pytest.approx(30.0) if night else None)


def _build(tmp_path) -> pathlib.Path:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene = _scene_for(stage)

    shot_ctx = _add(scene, "stage_expression", "ShotContext")
    rows = shot_ctx.get_param("variables")
    rows.size = 1
    rows.get_indexed_param("var_name", 0).value = "SHOT"
    rows.get_indexed_param("var_type", 0).value = "string"
    rows.get_indexed_param("var_value", 0).value = "sh010_day"
    # The Sun first, so its layer is the one that defines /lights: gated off at night, it
    # must not take the shared parent -- and every light under it -- along.
    sun = _light(scene, "Sun", "/lights/sun", intensity=3.0)
    hood = _light(scene, "Hood", "/lights/hood", intensity=25.0)
    lamp = _light(scene, "Lamp", "/lights/lamp", intensity=30.0)
    day = _add(scene, "stage_expression_filter", "DayOnly")
    night = _add(scene, "stage_expression_filter", "NightOnly")
    for f, pattern in ((day, "*_day"), (night, "*_night")):
        f.get_param("variable").value = "SHOT"
        f.get_param("pattern").value = pattern
    hood.add_dependency(shot_ctx)
    day.add_dependency(hood)
    day.add_dependency(sun)
    night.add_dependency(day)
    night.add_dependency(lamp)
    for c in (shot_ctx, hood, sun, lamp, day, night):
        c.execute()

    _assert_shot(stage, night=False)
    _set_shot(scene, "sh030_night")
    _assert_shot(stage, night=True)
    _set_shot(scene, "sh010_day")
    _assert_shot(stage, night=False)

    out = pathlib.Path(tmp_path) / "day_night.usda"
    save_scene(scene, out)
    return out


def _reopen(path) -> "tuple[Usd.Stage, SceneSender]":
    chunk_registry.ChunkLayerManager().reset()
    stage = Usd.Stage.Open(str(path))
    return stage, _scene_for(stage)


def test_the_saved_network_opens_on_its_shot_and_switches_both_ways(tmp_path) -> None:
    stage, scene = _reopen(_build(tmp_path))

    _assert_shot(stage, night=False)
    _set_shot(scene, "sh030_night")
    _assert_shot(stage, night=True)
    _set_shot(scene, "sh020_day")
    _assert_shot(stage, night=False)


def test_a_shot_chosen_after_reopening_is_the_one_saved(tmp_path) -> None:
    stage, scene = _reopen(_build(tmp_path))
    _set_shot(scene, "sh030_night")
    again = pathlib.Path(tmp_path) / "again.usda"
    save_scene(scene, again)

    stage, scene = _reopen(again)

    _assert_shot(stage, night=True)
    _set_shot(scene, "sh010_day")
    _assert_shot(stage, night=False)
