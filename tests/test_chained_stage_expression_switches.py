"""Switches in series: a sequence look, then notes that land on named shots.

One switch is scene 8 -- day or night. A show is this: a look chosen per sequence, and
notes chosen per shot, stacked down one thread.

    Base ─┬─ Night ──┐ normal
          └─ Day ────┴── SeqLook  (SEQ ~ "sq020")
                            │
    SeqLook ─┬──────────────┴──────┐ normal
             └─ Moon ──────────────┴── NightNote  (SHOT ~ "sq010_sh030 sq010_sh040")
                                            │
    NightNote ─┬────────────────────────────┴──┐ normal
               └─ Bounce ───────────────────────┴── AfternoonNote  (SHOT ~ "sq020_sh020")

Each note forks the thread and rejoins it: normal is the chain untouched, matched is the
chain plus one extra chunk. So a note *adds* to whatever look is underneath it, and both
notes are in the chain on every shot -- which of them does anything is read off the stage.

What is actually at risk here, and is not covered by a single switch in isolation:

* a downstream gate must not un-mute what an upstream gate suppressed. Both record what
  they muted, and an earlier bug had one gate release a branch it never touched, so the
  sequence's discarded look came back on a shot with a note on it.
* the shared thread runs through the upstream gate, so every gate below it has that gate
  as an ancestor of BOTH its inputs. A gate that muted its normal branch wholesale would
  take the whole scene with it.
* a note pattern names shots of one sequence, so on the other sequence it must simply
  miss -- the notes do not know about each other or about SeqLook.

Assertions read composed transforms, not chunk state: a branch that is "muted" but whose
transform still reaches the stage would pass a state-only check and be wrong.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager

# The directories the two chunk modules actually live in, not just scripts/: the registry
# loads chunks under their bare module name, so once it has run `import_module` finds them
# whatever the path -- but only then. Naming the directories lets this file be run on its
# own, and still returns the registry's copy when there is one (sys.modules is checked
# first), which is the copy that shares state with a live chunk.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
for _chunk_dir in (_SCRIPTS / "utils", _SCRIPTS / "transforms"):
    if str(_chunk_dir) not in sys.path:
        sys.path.insert(0, str(_chunk_dir))
switch_mod = importlib.import_module("stage_expression_switch")
transform_mod = importlib.import_module("transform")

#: One prim per contributor, so "is it in this shot" is one composed value each.
BASE, NIGHT, DAY, MOON, BOUNCE = "/base", "/night", "/day", "/moon", "/bounce"

HEIGHTS = {BASE: 1.0, NIGHT: 10.0, DAY: 20.0, MOON: 5.0, BOUNCE: 7.0}

NIGHT_NOTE = "sq010_sh030 sq010_sh040"
AFTERNOON_NOTE = "sq020_sh020"


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
        for path in HEIGHTS:
            UsdGeom.Cube.Define(stage, path)

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


def _place(sender, name, prim_path, upstream=None):
    node = _mk(sender, transform_mod, name, "transform")
    node.get_param("primpath_location").value = prim_path
    node.get_param("tz").value = HEIGHTS[prim_path]
    if upstream is not None:
        _wire(sender.get_stage(), node, upstream)
    node.execute()
    return node


def _wire(stage, dst, *sources):
    """Wire sources into dst's POSITIONAL inputs -- normal path first, matched path second."""
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(dst)))
    for i, src in enumerate(sources):
        name = chunk_stage.exec_input_name(i)
        attr = prim.GetAttribute(name) or prim.CreateAttribute(
            name, Sdf.ValueTypeNames.Token, custom=False
        )
        attr.SetConnections([
            Sdf.Path(f"{chunk_stage.get_chunk_prim_path(src)}.{chunk_stage._EXEC_OUTPUT}")
        ])


def _switch(sender, name, variable, pattern):
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
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath(prim_path)).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


@pytest.fixture
def show(scene):
    """The graph above, cooked once. Returns a function that moves it to a shot."""
    stage, sender = scene
    _declare(stage, SEQ="sq010", SHOT="sq010_sh010")

    base = _place(sender, "Base", BASE)
    night = _place(sender, "Night", NIGHT, upstream=base)
    day = _place(sender, "Day", DAY, upstream=base)
    seq_look = _switch(sender, "SeqLook", "SEQ", "sq020")
    _wire(stage, seq_look, night, day)

    moon = _place(sender, "Moon", MOON, upstream=seq_look)
    night_note = _switch(sender, "NightNote", "SHOT", NIGHT_NOTE)
    _wire(stage, night_note, seq_look, moon)

    bounce = _place(sender, "Bounce", BOUNCE, upstream=night_note)
    afternoon_note = _switch(sender, "AfternoonNote", "SHOT", AFTERNOON_NOTE)
    _wire(stage, afternoon_note, night_note, bounce)

    gates = (seq_look, night_note, afternoon_note)

    def go(seq, shot):
        _declare(stage, SEQ=seq, SHOT=shot)
        # Top down, the order a cook reaches them in: an upstream gate's choice is what the
        # ones below it are gating on top of.
        for gate in gates:
            gate.execute()
        return {path: _height(stage, path) for path in HEIGHTS}

    return go


def _expect(base=True, night=False, day=False, moon=False, bounce=False):
    on = {BASE: base, NIGHT: night, DAY: day, MOON: moon, BOUNCE: bounce}
    return {path: (HEIGHTS[path] if live else 0.0) for path, live in on.items()}


# ---------------------------------------------------------------------------
# the sequence look moves five shots at a time
# ---------------------------------------------------------------------------

def test_a_plain_shot_of_the_first_sequence_is_the_look_and_nothing_else(show):
    assert show("sq010", "sq010_sh010") == _expect(night=True)


def test_a_plain_shot_of_the_second_sequence_swaps_the_look(show):
    assert show("sq020", "sq020_sh010") == _expect(day=True)


# ---------------------------------------------------------------------------
# the notes land on named shots, on top of the look
# ---------------------------------------------------------------------------

def test_a_note_adds_to_the_look_underneath_it(show):
    """The shot the note names keeps its sequence's look and gains the extra chunk."""
    assert show("sq010", "sq010_sh030") == _expect(night=True, moon=True)


def test_the_other_shot_the_same_note_names(show):
    """Two shots, one pattern -- which is the point of a glob over a node per shot."""
    assert show("sq010", "sq010_sh040") == _expect(night=True, moon=True)


def test_a_shot_between_two_noted_ones_is_untouched(show):
    assert show("sq010", "sq010_sh050") == _expect(night=True)


def test_the_second_sequences_note_stacks_on_its_own_look(show):
    assert show("sq020", "sq020_sh020") == _expect(day=True, bounce=True)


def test_a_note_does_not_fire_outside_its_own_sequence(show):
    """NightNote names sq010 shots only, so on sq020_sh030 it simply misses -- it knows
    nothing about SeqLook or about the other note."""
    assert show("sq020", "sq020_sh030") == _expect(day=True)


# ---------------------------------------------------------------------------
# what chaining itself puts at risk
# ---------------------------------------------------------------------------

def test_a_note_does_not_revive_the_look_the_sequence_gate_discarded(show):
    """The failure this test exists for: a downstream gate releasing a branch an upstream
    gate muted. On a noted shot, both gates run -- the discarded look must stay off."""
    result = show("sq010", "sq010_sh030")

    assert result[DAY] == 0.0, "the sequence's other look must stay muted under a note"
    assert result[MOON] == HEIGHTS[MOON]


def test_the_shared_thread_survives_both_gates(show):
    """Every gate below SeqLook has it as an ancestor of both inputs. One that muted its
    normal branch wholesale would take the base -- and the look -- with it."""
    for seq, shot in (("sq010", "sq010_sh010"), ("sq010", "sq010_sh030"),
                      ("sq020", "sq020_sh020")):
        assert show(seq, shot)[BASE] == HEIGHTS[BASE], f"{shot} lost the shared thread"


def test_walking_the_whole_show_leaves_nothing_stuck_on(show):
    """Ten shots in order, the way someone reviews a sequence. A gate that failed to
    un-mute would show up as a note bleeding into the shot after it."""
    seen = {}
    for seq in ("sq010", "sq020"):
        for n in range(1, 6):
            shot = f"{seq}_sh{n * 10:03d}"
            seen[shot] = show(seq, shot)

    assert seen["sq010_sh010"] == _expect(night=True)
    assert seen["sq010_sh030"] == _expect(night=True, moon=True)
    assert seen["sq010_sh040"] == _expect(night=True, moon=True)
    assert seen["sq010_sh050"] == _expect(night=True), "the note must not bleed into the next shot"
    assert seen["sq020_sh010"] == _expect(day=True)
    assert seen["sq020_sh020"] == _expect(day=True, bounce=True)
    assert seen["sq020_sh030"] == _expect(day=True), "nor into this one"
    assert seen["sq020_sh050"] == _expect(day=True)


def test_going_back_to_a_shot_gives_the_same_scene(show):
    """Order must not matter: a shot revisited after its neighbours composes identically."""
    first = show("sq010", "sq010_sh030")
    show("sq020", "sq020_sh020")
    show("sq010", "sq010_sh010")

    assert show("sq010", "sq010_sh030") == first
