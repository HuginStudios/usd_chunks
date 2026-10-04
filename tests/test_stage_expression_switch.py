"""A Stage Expression Switch picks between two paths from what the stage says.

The shape Solaris uses: the matched path when the variable matches the pattern, the normal
path when it does not, and never both. So the graph stays a single thread with a fork that
rejoins, rather than a branch hanging off the side of the chain, and the work above the
fork is shared between the two paths.

The variable is picked by name from what the stage declares, never typed as a ${SHOT}
reference: USD substitutes expression variables into asset paths and variant selections and
nowhere else, and a field accepting one anywhere would be usd_chunks inventing a rule
OpenUSD does not have.

Assertions check composed values, not just chunk state: a branch that is "muted" but whose
transform still reaches the stage would pass a state-only check and be wrong.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks import chunk_stage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chunks"))
switch_mod = importlib.import_module("stage_expression_switch")
transform_mod = importlib.import_module("transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    with Usd.EditContext(stage, layer):
        UsdGeom.Cube.Define(stage, "/a")
        UsdGeom.Cube.Define(stage, "/b")
        UsdGeom.Cube.Define(stage, "/c")

    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)
    return stage, sender


def _mk(sender, module, name, config_module, **config_kwargs):
    node = module.main()
    node._config = ChunkConfig(module=config_module, **config_kwargs)
    node.name = name
    sender.add_chunk(node)
    return node


def _place(sender, name, prim_path, height):
    node = _mk(sender, transform_mod, name, "transform")
    node.get_param("primpath_location").value = prim_path
    node.get_param("tz").value = height
    node.recook()
    return node


def _wire(stage, dst, *sources):
    """Wire sources into dst's POSITIONAL inputs -- normal path first, matched path second.

    Which input a path landed in is the whole meaning here, unlike a Switch where every
    slot is interchangeable, so the test has to author them the way the ports do.
    """
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(dst)))
    for i, src in enumerate(sources):
        name = chunk_stage.exec_input_name(i)
        attr = prim.GetAttribute(name) or prim.CreateAttribute(
            name, Sdf.ValueTypeNames.Token, custom=False
        )
        attr.SetConnections([
            Sdf.Path(f"{chunk_stage.get_chunk_prim_path(src)}.{chunk_stage._EXEC_OUTPUT}")
        ])


def _switch(sender, pattern="", name="ShotSwitch", variable="SHOT"):
    node = _mk(sender, switch_mod, name, "stage_expression_switch", exec_inputs=2)
    node.get_param(switch_mod.VARIABLE_PARAM).value = variable
    node.get_param(switch_mod.PATTERN_PARAM).value = pattern
    return node


def _declare(stage, **variables):
    current = dict(chunk_utils.stage_expression_variables(stage))
    current.update(variables)
    chunk_utils.set_stage_expression_variables(stage, current)


def _height(stage, prim_path):
    """The composed z translation, or 0 when the branch that authored it is muted."""
    prim = stage.GetPrimAtPath(prim_path)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


# ---------------------------------------------------------------------------
# choosing a branch
# ---------------------------------------------------------------------------

def test_a_match_takes_the_matched_path(scene):
    stage, sender = scene
    _declare(stage, SHOT="sh030_night")
    normal = _place(sender, "Day", "/a", 5.0)
    matched = _place(sender, "Night", "/b", 7.0)

    sw = _switch(sender, "*_night")
    _wire(stage, sw, normal, matched)
    sw.recook()

    assert _height(stage, "/a") == pytest.approx(0.0), "the normal path is muted"
    assert _height(stage, "/b") == pytest.approx(7.0)
    assert normal.is_blocked()
    assert not matched.is_blocked()


def test_a_miss_takes_the_normal_path(scene):
    """Solaris' rule: no match means the scene runs as it normally would."""
    stage, sender = scene
    _declare(stage, SHOT="sh010_day")
    normal = _place(sender, "Day", "/a", 5.0)
    matched = _place(sender, "Night", "/b", 7.0)

    sw = _switch(sender, "*_night")
    _wire(stage, sw, normal, matched)
    sw.recook()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(0.0)
    assert not normal.is_blocked()
    assert matched.is_blocked()


def test_changing_the_variable_swaps_the_path(scene):
    stage, sender = scene
    _declare(stage, SHOT="sh030_night")
    normal = _place(sender, "Day", "/a", 5.0)
    matched = _place(sender, "Night", "/b", 7.0)
    sw = _switch(sender, "*_night")
    _wire(stage, sw, normal, matched)
    sw.recook()

    _declare(stage, SHOT="sh010_day")
    sw.recook()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(0.0)


def test_an_undeclared_variable_runs_the_normal_path(scene):
    """Rather than dropping into a branch meant for a shot nobody selected."""
    stage, sender = scene
    normal = _place(sender, "Day", "/a", 5.0)
    matched = _place(sender, "Night", "/b", 7.0)

    sw = _switch(sender, "*_night")
    _wire(stage, sw, normal, matched)
    sw.recook()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(0.0)


def test_an_empty_pattern_runs_the_normal_path(scene):
    stage, sender = scene
    _declare(stage, SHOT="sh030_night")
    normal = _place(sender, "Day", "/a", 5.0)
    matched = _place(sender, "Night", "/b", 7.0)

    sw = _switch(sender)
    _wire(stage, sw, normal, matched)
    sw.recook()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(0.0)


def test_it_mutes_the_whole_branch_not_just_the_input(scene):
    """Suppressing only the chunk plugged into the input would leave the rest of that
    branch still authoring into the composed stage."""
    stage, sender = scene
    _declare(stage, SHOT="sh010_day")   # the normal path, so the matched one is muted
    day = _place(sender, "Day", "/a", 5.0)
    upstream = _place(sender, "NightUpstream", "/b", 7.0)
    tail = _mk(sender, transform_mod, "NightTail", "transform")
    tail.get_param("primpath_location").value = "/b"
    tail.get_param("tz").value = 7.0
    _wire(stage, tail, upstream)
    tail.recook()

    sw = _switch(sender, "*_night")
    _wire(stage, sw, day, tail)
    sw.recook()

    assert _height(stage, "/b") == pytest.approx(0.0)
    assert upstream.is_blocked(), "the branch behind the input must be muted too"


def test_the_thread_above_the_fork_is_shared(scene):
    """The whole point of wiring the branches in rather than hanging them off the side: a
    chunk feeding both stays live whichever branch is chosen."""
    stage, sender = scene
    _declare(stage, SHOT="sh030_night")
    shared = _place(sender, "Shared", "/a", 5.0)
    day = _mk(sender, transform_mod, "Day", "transform")
    day.get_param("primpath_location").value = "/b"
    day.get_param("tz").value = 7.0
    _wire(stage, day, shared)
    day.recook()
    night = _mk(sender, transform_mod, "Night", "transform")
    night.get_param("primpath_location").value = "/c"
    night.get_param("tz").value = 9.0
    _wire(stage, night, shared)
    night.recook()

    sw = _switch(sender, "*_night")
    _wire(stage, sw, day, night)
    sw.recook()

    assert _height(stage, "/a") == pytest.approx(5.0), "the shared upstream stays live"
    assert _height(stage, "/b") == pytest.approx(0.0)
    assert _height(stage, "/c") == pytest.approx(9.0)
    assert not shared.is_blocked()


def test_disable_is_left_alone(scene):
    """The block is the engine's lever; DISABLED is the user's. Choosing a branch must not
    clear a disable someone set by hand."""
    stage, sender = scene
    _declare(stage, SHOT="sh010_day")
    day = _place(sender, "Day", "/a", 5.0)
    day.set_state(ChunkFlags.State.DISABLED)

    sw = _switch(sender, "*_night")
    _wire(stage, sw, day)
    sw.recook()

    assert day.has_state(ChunkFlags.State.DISABLED)


def test_a_released_branch_is_unblocked_when_it_is_unwired(scene):
    """Muted by an earlier evaluation and no longer an input at all: leaving it blocked
    would strand a branch the user has already disconnected."""
    stage, sender = scene
    _declare(stage, SHOT="sh010_day")
    day = _place(sender, "Day", "/a", 5.0)
    night = _place(sender, "Night", "/b", 7.0)
    sw = _switch(sender, "*_night")
    _wire(stage, sw, day, night)
    sw.recook()
    assert night.is_blocked()

    _wire(stage, sw, day)
    stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(sw))).GetAttribute(
        chunk_stage.exec_input_name(1)
    ).SetConnections([])
    sw.recook()

    assert not night.is_blocked()
    assert _height(stage, "/b") == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,pattern,expected", [
    ("sh010", "sh01*", True),
    ("sh020", "sh01*", False),
    ("bg_01", "bg_??", True),
    ("SH010", "sh01*", False),          # case-sensitive on every platform
    ("sh020", "sh01* sh02*", True),     # space-separated: either one matches
    ("sh030", "sh01* sh02*", False),
    ("sh010", "", False),               # nothing to match against
    ("sh010", "   ", False),
])
def test_glob_matching(value, pattern, expected):
    assert switch_mod.matches(value, pattern) is expected


def test_a_typed_variable_is_matched_on_its_value(scene):
    """USD expression variables are typed -- an int IDENTIFIER against "32*" reads the way
    anyone typing that pattern expects, and a list matches on its elements."""
    stage, sender = scene
    _declare(stage, IDENTIFIER=3254, renderPassList=["foreground", "FX"])
    normal = _place(sender, "Normal", "/a", 5.0)
    matched = _place(sender, "Matched", "/b", 7.0)

    sw = _switch(sender, "32*", variable="IDENTIFIER")
    _wire(stage, sw, normal, matched)
    sw.recook()
    assert _height(stage, "/b") == pytest.approx(7.0)

    sw.get_param(switch_mod.VARIABLE_PARAM).value = "renderPassList"
    sw.get_param(switch_mod.PATTERN_PARAM).value = "FX"
    sw.recook()
    assert _height(stage, "/b") == pytest.approx(7.0), "a list matches on its elements"

    sw.get_param(switch_mod.PATTERN_PARAM).value = "matte"
    sw.recook()
    assert _height(stage, "/b") == pytest.approx(0.0), "no match runs the normal path"
    assert _height(stage, "/a") == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# the variable menu
# ---------------------------------------------------------------------------

def test_the_variable_menu_lists_what_the_stage_declares(scene):
    stage, sender = scene
    _declare(stage, SHOT="sh010", ASSET="hero")
    sw = _switch(sender)

    switch_mod._sync_variable_choices(sw)

    assert list(sw.get_param(switch_mod.VARIABLE_PARAM).menu_items) == ["ASSET", "SHOT"]


def test_a_variable_that_is_gone_stays_selected(scene):
    """Silently moving to another variable would switch on something nobody chose."""
    stage, sender = scene
    _declare(stage, SHOT="sh010")
    sw = _switch(sender)
    switch_mod._sync_variable_choices(sw)

    chunk_utils.set_stage_expression_variables(stage, {"ASSET": "hero"})
    switch_mod._sync_variable_choices(sw)

    assert sw.get_param(switch_mod.VARIABLE_PARAM).value == "SHOT"
    assert "SHOT" in sw.get_param(switch_mod.VARIABLE_PARAM).menu_items


# ---------------------------------------------------------------------------
# what muting does to a shared parent
# ---------------------------------------------------------------------------

def test_muting_a_branch_does_not_orphan_the_other_ones_prims(scene):
    """Both branches author under one parent, and whichever chunk cooked first owns its
    `def` -- UsdStage.DefinePrim gives every later one a bare `over`. Mute the first and
    the parent is defined by nobody, so the *other* branch's prim is valid but not defined:
    Hydra skips it, and a chunk nobody gated disappears. Live-reported as a night shot
    rendering a flat grey frame, camera and all.

    The gate repairs that when it mutes, because it is the only thing in the engine that
    mutes after its branches have cooked -- view-driven cooking mutes first, so DefinePrim
    already sees the final state there.
    """
    stage, sender = scene
    prim_mod = importlib.import_module("prim")

    def prim_chunk(name, path):
        node = _mk(sender, prim_mod, name, "prim")
        node.get_param("primpath_location").value = path
        node.get_param("prim_type").value = "Xform"
        node.recook()
        return node

    first = prim_chunk("First", "/shared/first")    # cooks first, so it owns `def /shared`
    second = prim_chunk("Second", "/shared/second")  # gets a bare `over /shared`
    _declare(stage, SHOT="sh030_night")

    sw = _switch(sender, "*_night")
    _wire(stage, sw, first, second)   # normal path, matched path
    sw.recook()

    def defined(path):
        prim = stage.GetPrimAtPath(path)
        return bool(prim) and prim.IsDefined()

    assert defined("/shared/second"), "the live branch's prim must survive the mute"
    assert defined("/shared"), "and so must the parent they share"
    assert not defined("/shared/first"), "the muted branch is gone"
