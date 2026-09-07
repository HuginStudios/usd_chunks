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
from usd_chunks.plugins import chunk_usd

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
    chunk_usd.ChunkStageSync().initialize(stage, sender)
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
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(dst)))
    for i, src in enumerate(sources):
        name = chunk_usd.exec_input_name(i)
        attr = prim.GetAttribute(name) or prim.CreateAttribute(
            name, Sdf.ValueTypeNames.Token, custom=False
        )
        attr.SetConnections([
            Sdf.Path(f"{chunk_usd.get_chunk_prim_path(src)}.{chunk_usd._EXEC_OUTPUT}")
        ])


def _filter(sender, name="Filter"):
    return _mk(sender, filter_mod, name, "stage_expression_filter", exec_inputs=2)


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
    f.get_param("value").value = "sh010"
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
    f.get_param("value").value = "sh020"
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
    f.get_param("value").value = "sh020"
    f.execute()
    assert gated.is_blocked()

    f.get_param("value").value = "sh011"
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
    f.get_param("value").value = "sh020"
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
    f.get_param("value").value = "sh020"
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
    f.get_param("value").value = "sh020"
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
    f.get_param("value").value = "sh010"
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
    f.get_param("value").value = "sh010"
    f.get_param("pattern").value = "sh01*"
    f.execute()

    assert gated.has_state(ChunkFlags.State.DISABLED)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,pattern,mode,expected", [
    ("sh010", "sh01*", filter_mod.GLOB, True),
    ("sh020", "sh01*", filter_mod.GLOB, False),
    ("bg_01", "bg_??", filter_mod.GLOB, True),
    ("SH010", "sh01*", filter_mod.GLOB, False),   # case-sensitive on every platform
    ("sh010", r"^sh0\d+$", filter_mod.REGEX, True),
    ("sh010", r"^bg", filter_mod.REGEX, False),
    ("sh010", "sh010", filter_mod.EQUALS, True),
    ("sh010", "sh01", filter_mod.EQUALS, False),
])
def test_match_modes(value, pattern, mode, expected):
    assert filter_mod._matches(value, pattern, mode) is expected


def test_a_bad_regex_reports_itself_and_does_not_match(capsys):
    """Silently never matching is indistinguishable from the variable being wrong."""
    assert filter_mod._matches("sh010", "sh01[", filter_mod.REGEX) is False
    assert "not a valid regex" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the point of it: gating on a stage variable
# ---------------------------------------------------------------------------

def test_it_gates_on_a_stage_variable(scene):
    """End to end: a Stage Expression declares the variable, the filter tests a reference
    to it, and changing the variable changes which branches are in the scene."""
    stage, sender = scene
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010"})
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)

    f = _filter(sender)
    _wire(stage, f, stream, gated)
    f.get_param("value").value = "${SHOT}"
    f.get_param("pattern").value = "sh01*"
    f.execute()
    assert _height(stage, "/b") == pytest.approx(7.0)

    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh020"})
    f.execute()

    assert _height(stage, "/b") == pytest.approx(0.0)
    assert gated.is_blocked()


# ---------------------------------------------------------------------------
# positional slots
# ---------------------------------------------------------------------------

def test_the_config_declares_two_slots():
    assert filter_mod.config().exec_inputs == 2


def test_add_dependency_fills_the_slots_in_order(scene):
    """A wire made through the API rather than by dragging a port must still land one per
    slot -- filling slot 0 twice would make the gated branch mean what the stream does."""
    stage, sender = scene
    stream = _place(sender, "Stream", "/a", 5.0)
    gated = _place(sender, "Gated", "/b", 7.0)

    f = _filter(sender)
    f.add_dependency(stream)
    f.add_dependency(gated)

    prim = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(f)))
    slot0 = prim.GetAttribute(chunk_usd.exec_input_name(0)).GetConnections()
    slot1 = prim.GetAttribute(chunk_usd.exec_input_name(1)).GetConnections()
    assert len(slot0) == 1 and len(slot1) == 1
    assert chunk_usd.get_chunk_prim_path(stream) in str(slot0[0])
    assert chunk_usd.get_chunk_prim_path(gated) in str(slot1[0])


def test_an_unwired_branch_is_released_even_when_it_is_more_than_one_chunk_deep(scene):
    """The `muted` record exists so a branch that stops being gated is un-muted.

    It records the whole branch, since muting one means muting all of it -- but the release
    only ever searched the filter's DIRECT inputs, so anything further up a branch was
    never found and stayed blocked and muted for the rest of the session, with no way back.
    Only a one-chunk branch was ever released, which is why the other tests do not see it.
    """
    stage, sender = scene
    stream = _place(sender, "stream", "/a", 1.0)
    # The gated branch is two deep: inner feeds slot_chunk, which is what the filter sees.
    inner = _place(sender, "inner", "/b", 5.0)
    slot_chunk = _place(sender, "slot", "/b", 7.0)
    _wire(stage, slot_chunk, inner)

    node = _mk(sender, filter_mod, "filter", "stage_expression_filter")
    node.get_param("value").value = "sh010"
    node.get_param("pattern").value = "sh99*"  # a miss: the gated branch is muted
    _wire(stage, node, stream, slot_chunk)
    node.execute()
    assert inner.is_blocked(), "the gate is shut, so the whole gated branch is muted"

    # The user unwires the gated branch from the filter entirely. _wire only rewrites the
    # slots it is given, so slot 1 has to be cleared rather than left pointing at it.
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_usd.get_chunk_prim_path(node)))
    prim.GetAttribute(chunk_usd.exec_input_name(1)).SetConnections([])
    node.execute()

    assert not inner.is_blocked(), (
        "nothing is gating inner any more -- it is not upstream of the filter at all"
    )
