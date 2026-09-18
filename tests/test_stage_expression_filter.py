"""A Stage Expression Filter forwards one input always and gates the other on a pattern.

Where a Switch picks between interchangeable branches by index, this picks by a fact read
off the stage -- so one network can carry work that only happens on some shots or some
asset variants, and cooking it under a different Stage Expression produces a different
scene with nothing rewired.

The two slots are positional and mean different things: slot 0 is forwarded whatever the
test says, slot 1 contributes only on a match. Suppression is the Switch's lever (a block
plus a layer mute), never DISABLED, which belongs to the user.

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
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks import chunk_stage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
filter_mod = importlib.import_module("stage_expression_filter")
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

    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)
    return stage, sender


def _mk(sender, module, name, config_module, **config_kwargs):
    node = module.main()
    node._config = ChunkConfig(module=config_module, **config_kwargs)
    node._module = module
    node.name = name
    sender.add_chunk(node)
    return node


def _place(sender, name, prim_path, height):
    node = _mk(sender, transform_mod, name, "transform")
    node.get_param("primpath_location").value = prim_path
    node.get_param("tz").value = height
    node.execute()
    return node


def _wire(stage, dst, *sources):
    """Wire sources into dst's POSITIONAL slots -- one per slot, not stacked on slot 0.

    Which slot a branch landed in is the whole meaning here, unlike a Switch where every
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


def _filter(sender, name="Filter"):
    return _mk(sender, filter_mod, name, "stage_expression_filter", exec_inputs=2)


def _shot(stage, f, value, variable="SHOT"):
    """Declare `variable` on the stage and point the filter at it.

    The filter no longer takes a ${SHOT} reference typed into a field -- USD substitutes
    expression variables into asset paths and variant selections only, so a field that
    accepted one anywhere would be usd_chunks inventing a rule OpenUSD does not have. It
    picks a declared variable by name and reads it off the stage instead.
    """
    variables = dict(chunk_utils.stage_expression_variables(stage))
    variables[variable] = value
    chunk_utils.set_stage_expression_variables(stage, variables)
    f.get_param(filter_mod.VARIABLE_PARAM).value = variable


def _height(stage, prim_path):
    """The composed z translation, or 0 when the branch that authored it is muted."""
    prim = stage.GetPrimAtPath(prim_path)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


# ---------------------------------------------------------------------------
# gating
# ---------------------------------------------------------------------------

def test_a_match_lets_the_gated_branch_through(scene):
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)

    f = _filter(sender)
    _wire(stage, f, stream, gated)
    _shot(stage, f, "sh010")
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(7.0)
    assert not gated.is_blocked()


def test_a_miss_mutes_the_gated_branch_and_keeps_the_stream(scene):
    """The user's own framing: on a failure the filter does nothing to the stream, it just
    forwards it. Only the second input is gated."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)

    f = _filter(sender)
    _wire(stage, f, stream, gated)
    _shot(stage, f, "sh020")
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert _height(stage, "/a") == pytest.approx(5.0), "the stream is forwarded regardless"
    assert _height(stage, "/b") == pytest.approx(0.0), "the gated branch should be muted"
    assert gated.is_blocked()
    assert not stream.is_blocked()


def test_changing_the_value_reopens_the_gate(scene):
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)

    f = _filter(sender)
    _wire(stage, f, stream, gated)
    f.get_param("pattern").value = "sh01*"
    _shot(stage, f, "sh020")
    f.execute()
    assert gated.is_blocked()

    _shot(stage, f, "sh011")
    f.execute()

    assert _height(stage, "/b") == pytest.approx(7.0)
    assert not gated.is_blocked()


def test_it_mutes_the_whole_branch_not_just_the_slot(scene):
    """Suppressing only the chunk plugged into the slot would leave the rest of that branch
    still authoring into the composed stage."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    upstream = _place(sender, "Upstream", "/b", 7.0)
    tail = _mk(sender, transform_mod, "Tail", "transform")
    tail.get_param("primpath_location").value = "/b"
    tail.get_param("tz").value = 7.0
    _wire(stage, tail, upstream)
    tail.execute()

    f = _filter(sender)
    _wire(stage, f, stream, tail)
    _shot(stage, f, "sh020")
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert _height(stage, "/b") == pytest.approx(0.0)
    assert upstream.is_blocked(), "the branch behind the slot must be muted too"


def test_a_chunk_feeding_both_inputs_stays_live(scene):
    """The forwarded stream wins. Muting a shared upstream along with the gated branch
    would take the stream down with it."""
    stage, sender = scene
    shared = _place(sender, "Shared", "/a", 5.0)
    gated = _mk(sender, transform_mod, "Gated", "transform")
    gated.get_param("primpath_location").value = "/b"
    gated.get_param("tz").value = 7.0
    _wire(stage, gated, shared)
    gated.execute()

    f = _filter(sender)
    _wire(stage, f, shared, gated)
    _shot(stage, f, "sh020")
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert not shared.is_blocked()
    assert gated.is_blocked()


def test_no_gated_branch_is_a_passthrough(scene):
    """A filter still being built should forward, not report an error."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)

    f = _filter(sender)
    _wire(stage, f, stream)
    _shot(stage, f, "sh020")
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert not stream.is_blocked()


def test_an_empty_pattern_never_matches(scene):
    """Otherwise a half-configured filter would silently let everything through."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)

    f = _filter(sender)
    _wire(stage, f, stream, gated)
    _shot(stage, f, "sh010")
    f.execute()

    assert gated.is_blocked()


def test_disable_is_left_alone(scene):
    """The block is the engine's lever; DISABLED is the user's. Reopening the gate must not
    clear a disable someone set by hand."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)
    gated.set_state(ChunkFlags.State.DISABLED)

    f = _filter(sender)
    _wire(stage, f, stream, gated)
    _shot(stage, f, "sh010")
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert gated.has_state(ChunkFlags.State.DISABLED)


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
    assert filter_mod.matches(value, pattern) is expected


def test_the_variable_menu_lists_what_the_stage_declares(scene):
    """The dropdown is filled from the stage, so a variable declared by a Stage Expression
    upstream -- or by the file that was opened -- is offered by name."""
    stage, sender = scene
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010", "ASSET": "hero"})
    f = _filter(sender)

    filter_mod._sync_variable_choices(f)

    assert list(f.get_param(filter_mod.VARIABLE_PARAM).menu_items) == ["ASSET", "SHOT"]


def test_a_variable_that_is_gone_stays_selected(scene):
    """Silently moving to another variable would gate on something nobody chose."""
    stage, sender = scene
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    f = _filter(sender)
    filter_mod._sync_variable_choices(f)
    f.get_param(filter_mod.VARIABLE_PARAM).value = "SHOT"

    chunk_utils.set_stage_expression_variables(stage, {"ASSET": "hero"})
    filter_mod._sync_variable_choices(f)

    assert f.get_param(filter_mod.VARIABLE_PARAM).value == "SHOT"
    assert "SHOT" in f.get_param(filter_mod.VARIABLE_PARAM).menu_items


def test_a_typed_variable_is_gated_on_its_value(scene):
    """USD expression variables are typed -- an int IDENTIFIER against "32*" reads the way
    anyone typing that pattern expects, and a list matches on its elements."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)
    f = _filter(sender)
    _wire(stage, f, stream, gated)
    chunk_utils.set_stage_expression_variables(
        stage, {"IDENTIFIER": 3254, "renderPassList": ["foreground", "FX"]}
    )

    f.get_param(filter_mod.VARIABLE_PARAM).value = "IDENTIFIER"
    f.get_param("pattern").value = "32*"
    f.execute()
    assert _height(stage, "/b") == pytest.approx(7.0)

    f.get_param(filter_mod.VARIABLE_PARAM).value = "renderPassList"
    f.get_param("pattern").value = "FX"
    f.execute()
    assert _height(stage, "/b") == pytest.approx(7.0), "a list matches on its elements"

    f.get_param("pattern").value = "matte"
    f.execute()
    assert _height(stage, "/b") == pytest.approx(0.0)
