"""Changing a shot must not recompose the stage once per chunk.

Every `stage.MuteLayer` / `UnmuteLayer` is a composition change, and USD reports a
composition change to everything watching the stage as a resync of `/` -- the whole stage,
re-read. The gate and the view-driven cook both moved layers one at a time, so a single
shot change in a shot-gated graph cost one full-stage resync per chunk moved.

Live-reported from the end that hurts: a light coming back did not appear in the V-Ray
viewport until the renderer was switched away and back -- which is just a repopulate done
once, cleanly, instead of once per chunk. The prim browser was unhappy too.

Measured on the shot-pipeline demo before the fix: 158 full-stage resyncs on the first shot
change and 5 in the steady state. After: 7 and 3.

These tests count the resyncs rather than trusting the number, because the cost is invisible
from the composed stage -- the end state was always right, which is exactly why this went
unnoticed until a renderer disagreed.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
from pxr import Tf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "chunks"
for _dir in (_SCRIPTS / "utils", _SCRIPTS / "modelling"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
switch_mod = importlib.import_module("stage_expression_switch")

A = "/World/lights/a"
B = "/World/lights/b"

#: The gated branch below holds five chunks. One recomposition for the lot is the point; a
#: couple more for the cook that follows is fine. One per chunk is not -- which is what the
#: budget has to be tight enough to catch.
BUDGET = 4


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _ResyncCounter:
    """Counts notices that tell a watcher the whole stage may have changed."""

    def __init__(self, stage):
        self.full = 0
        self._conn = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on, stage)

    def _on(self, notice, sender):
        if any(str(p) == "/" for p in notice.GetResyncedPaths()):
            self.full += 1

    def reset(self):
        self.full = 0


def _make(scene, module_name, name, **config):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    chunks = mod.create() if mod.loaded else mod.load()
    for c in chunks:
        scene.add_chunk(c)
    node = chunks[0]
    node.name = name
    if config:
        node._config = ChunkConfig(module=module_name, **config)
    return node


def _light(scene, name, path, intensity):
    node = _make(scene, "light", name)
    node.get_param("primpath_location").value = path
    node.get_param("light_type").value = "SphereLight"
    node.execute()
    node.get_param("attr_grp.inputs.intensity").value = intensity
    return node


@pytest.fixture
def show(qapp):
    """A shared light, and a gated one behind SHOT ~ "sh040"."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})

    ctx = _make(scene, "stage_expression", "ShotContext")
    rows = ctx.get_param("variables")
    rows.size = 1
    rows.get_indexed_param("var_name", 0).value = "SHOT"
    rows.get_indexed_param("var_type", 0).value = "string"
    rows.get_indexed_param("var_value", 0).value = "sh010"
    ctx.execute()

    key = _light(scene, "Key", A, 10.0)
    key.add_dependency(ctx)

    # A branch of several chunks, not one: unbatched, the cost is one recomposition PER
    # chunk moved, so a one-chunk branch cannot tell the two apart.
    gated = _light(scene, "Gated", B, 900.0)
    gated.add_dependency(key)
    tail = gated
    for i in range(4):
        extra = _light(scene, f"GatedExtra{i}", f"{B}_{i}", 5.0)
        extra.add_dependency(tail)
        tail = extra

    gate = _make(scene, "stage_expression_switch", "Note", exec_inputs=2)
    gate.get_param(switch_mod.VARIABLE_PARAM).value = "SHOT"
    gate.get_param(switch_mod.PATTERN_PARAM).value = "sh040"
    gate.add_dependency(key)
    gate.add_dependency(tail)
    gate.execute()

    counter = _ResyncCounter(stage)

    def go(shot):
        counter.reset()
        rows.get_indexed_param("var_value", 0).value = shot
        ctx.execute()
        return counter.full

    return go, stage


def _defined(stage, path):
    prim = stage.GetPrimAtPath(path)
    return bool(prim) and prim.IsDefined()


def test_switching_a_shot_recomposes_a_handful_of_times(show):
    go, stage = show
    go("sh040")           # settle; the first switch does the most work

    assert go("sh020") <= BUDGET
    assert go("sh040") <= BUDGET


def test_the_light_coming_back_is_no_worse_than_it_going_away(show):
    """The direction that was reported: a light returning is an un-mute, and the un-mute
    pass was the one still moving layers one at a time."""
    go, stage = show
    go("sh040")

    going_away = go("sh020")
    coming_back = go("sh040")

    assert coming_back <= BUDGET
    assert coming_back <= going_away + 1, "returning must not cost more than leaving"


def test_the_gating_is_unchanged(show):
    """The whole point of batching is that only the cost changes."""
    go, stage = show

    go("sh040")
    assert _defined(stage, B) and _defined(stage, A)

    go("sh020")
    assert not _defined(stage, B), "the gated light must still go away"
    assert _defined(stage, A), "and the shared one must stay"

    go("sh040")
    assert _defined(stage, B), "and come back"


def test_a_gate_that_moves_nothing_recomposes_nothing(show):
    """Re-selecting the same shot is a no-op, and should cost nothing at all -- a mute
    re-applied to an already-muted layer still recomposes."""
    go, stage = show
    go("sh020")

    assert go("sh020") == 0
