"""A gated light goes away when the shot changes, even though cooking it races the gate.

Live-reported on the shot-pipeline demo: add a light at the end behind a Stage Expression
Switch, set the pattern to one shot, then change SHOT -- and the light stays on the stage
until a param on it (or on its transform) is touched.

The sequence underneath it:

1. the variable changes, so the switch evaluates and mutes the branch it is suppressing;
2. `gate_to_branches` then writes its record of what it muted, which is an ordinary param
   write and therefore fires notices;
3. one of those notices cooks the Light on the branch that was just muted;
4. the Light re-reads its prim to rebuild its params -- but the prim went out of the
   composed stage in step 1, and a resync EXPIRES the handle, so the read raises;
5. that exception escapes into `gate_to_branches`, which never finishes. The mute is half
   applied, the record is never written, and the branch stays composed.

So it is not that the gate decided wrongly -- it never got to decide. Which is why touching
the light fixed it: that cooked the chunk again on a settled stage.

Two tests here, at the two levels it can be pinned: the graph-level behaviour, and the
guarantee `show_attribute` now makes -- it reads the prim before it touches anything, so a
prim that vanishes costs a cook rather than the chunk's params.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
for _dir in (_SCRIPTS / "utils", _SCRIPTS / "modelling"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
switch_mod = importlib.import_module("stage_expression_switch")

RIM = "/World/lights/rim"
KEY = "/World/lights/key"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def graph(qapp):
    """A docker as well as a stage: the cascade that causes this runs through the widgets
    a chunk builds when its params change, so it does not happen headlessly at all."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(900, 700)
    docker.show()
    qapp.processEvents()
    try:
        yield qapp, scene, stage, docker
    finally:
        docker.close()


def _make(scene, module_name, name):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    chunks = mod.create() if mod.loaded else mod.load()
    for c in chunks:
        scene.add_chunk(c)
    chunks[0].name = name
    return chunks[0]


def _light(scene, name, path, intensity):
    node = _make(scene, "light", name)
    node.get_param("primpath_location").value = path
    node.get_param("light_type").value = "SphereLight"
    node.execute()
    node.get_param("attr_grp.inputs.intensity").value = intensity
    return node


def _defined(stage, path):
    prim = stage.GetPrimAtPath(path)
    return bool(prim) and prim.IsDefined()


@pytest.fixture
def show(graph):
    """key light always on; rim light behind a switch on SHOT ~ "sh040"."""
    qapp, scene, stage, docker = graph
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})

    ctx = _make(scene, "stage_expression", "ShotContext")
    rows = ctx.get_param("variables")
    rows.size = 1
    rows.get_indexed_param("var_name", 0).value = "SHOT"
    rows.get_indexed_param("var_type", 0).value = "string"
    rows.get_indexed_param("var_value", 0).value = "sh010"
    ctx.execute()

    key = _light(scene, "Key", KEY, 10.0)
    key.add_dependency(ctx)
    rim = _light(scene, "Rim", RIM, 900.0)
    rim.add_dependency(key)

    gate = _make(scene, "stage_expression_switch", "RimNote")
    gate._config = ChunkConfig(module="stage_expression_switch", exec_inputs=2)
    gate.get_param(switch_mod.VARIABLE_PARAM).value = "SHOT"
    gate.get_param(switch_mod.PATTERN_PARAM).value = "sh040"
    gate.add_dependency(key)   # normal path
    gate.add_dependency(rim)   # matched path
    gate.execute()
    qapp.processEvents()

    def go(shot):
        rows.get_indexed_param("var_value", 0).value = shot
        ctx.execute()
        qapp.processEvents()

    return go, stage, rim, qapp


def test_the_gated_light_follows_the_shot(show):
    """The reported failure: it stayed on the stage after the shot changed."""
    go, stage, rim, _qapp = show

    go("sh040")
    assert _defined(stage, RIM), "the shot the pattern names must switch it on"

    go("sh020")
    assert not _defined(stage, RIM), "and any other shot must switch it off again"


def test_it_keeps_switching_both_ways(show):
    go, stage, rim, _qapp = show

    seen = []
    for shot in ("sh040", "sh020", "sh040", "sh010", "sh040"):
        go(shot)
        seen.append(_defined(stage, RIM))

    assert seen == [True, False, True, False, True]


def test_the_shared_light_is_never_affected(show):
    go, stage, _rim, _qapp = show
    for shot in ("sh040", "sh020", "sh010"):
        go(shot)
        assert _defined(stage, KEY), f"{shot} took the shared light with it"


def test_the_gate_records_what_it_muted(show):
    """The record write is the step the exception used to abort, so an empty record after a
    mute is the signature of the bug rather than an unrelated detail."""
    go, stage, rim, _qapp = show
    gate = next(c for c in chunk_registry.ChunkRegistry().scene.get_chunks()
                if c.name == "RimNote")

    go("sh020")

    record = gate.get_param(chunk_stage.GATE_RECORD_PARAM).value or ""
    assert chunk_stage.get_chunk_prim_path(rim) in record


def test_the_light_still_has_its_params(show):
    """The other half. Rebuilding params starts by deleting the ones a chunk has, so a read
    that fails part way used to leave the chunk half emptied -- and a Light with no
    intensity param has nothing to drive its light with."""
    go, stage, rim, _qapp = show

    go("sh040")
    go("sh020")
    go("sh040")

    assert rim.get_param("attr_grp.inputs.intensity") is not None
    assert rim.get_param("attr_grp.inputs.intensity").value == pytest.approx(900.0)


def test_show_attribute_leaves_the_params_alone_when_the_prim_goes(graph, monkeypatch):
    """The guarantee itself, forced rather than raced.

    A prim that stops being readable part way through must cost a cook, not the params the
    group already has: they are what saved values are restored onto, and what drives the
    real attributes. Read the prim first, install second.
    """
    qapp, scene, stage, docker = graph
    UsdGeom.Sphere.Define(stage, "/probe").GetPrim().CreateAttribute(
        "inputs:one", Sdf.ValueTypeNames.Float).Set(1.0)

    group = chunk.Group("attr_grp")
    keep_me = chunk.Slider("already_here", default_value=3.0)
    group.add_param(keep_me)
    qapp.processEvents()
    before = [p.name for p in group.get_my_params()]
    assert before == ["already_here"], "test setup"

    calls = {"n": 0}
    real = chunk.param_from_attr

    def explode(attr, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("Accessed invalid attribute on expired prim")
        return real(attr, *args, **kwargs)

    monkeypatch.setattr(chunk, "param_from_attr", explode)
    chunk_utils.show_attribute(stage.GetPrimAtPath("/probe"), group)
    qapp.processEvents()

    assert calls["n"] > 1, "test setup: the read must have failed part way"
    assert [p.name for p in group.get_my_params()] == before, (
        "the params were torn down for a rebuild that never happened"
    )
