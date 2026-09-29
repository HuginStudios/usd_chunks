"""A Switch passes one upstream branch and mutes the rest.

Merge is fan-in -- every branch contributes and slot order decides who wins. Switch is the
choice instead of the sum. It authors nothing itself; it suppresses the branches it did not
pick by blocking them and muting their layers, so their opinions stop reaching the composed
stage.

A block rather than DISABLED, which belongs to the user. The two are independent levers, so
selecting a branch can never clear a disable someone set by hand -- pinned below, because
the first version used DISABLED and needed bookkeeping to avoid exactly that.

The assertions check composed values, not just chunk state: a branch that is "muted" but
whose transform still reaches the stage would pass a state-only check and be wrong.
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
switch_mod = importlib.import_module("switch")
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


def _mk(sender, module, name, config_module):
    node = module.main()
    node._config = ChunkConfig(module=config_module)
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
    prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(dst)))
    attr = prim.GetAttribute(chunk_stage._EXEC_INPUT) or prim.CreateAttribute(
        chunk_stage._EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
    )
    attr.SetConnections([
        Sdf.Path(f"{chunk_stage.get_chunk_prim_path(s)}.{chunk_stage._EXEC_OUTPUT}")
        for s in sources
    ])


def _height(stage, prim_path):
    """The composed z translation, or None when the branch that authored it is muted."""
    prim = stage.GetPrimAtPath(prim_path)
    matrix = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()[2]


def test_only_the_selected_branch_reaches_the_stage(scene):
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 0
    sw.execute()

    assert _height(stage, "/a") == pytest.approx(5.0)
    assert _height(stage, "/b") == pytest.approx(0.0), "branch B should be muted"
    assert branch_b.is_blocked()
    assert not branch_a.is_blocked()


def test_flipping_the_index_swaps_which_branch_is_live(scene):
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 0
    sw.execute()
    sw.get_param("index").value = 1
    sw.execute()

    assert _height(stage, "/b") == pytest.approx(7.0)
    assert _height(stage, "/a") == pytest.approx(0.0)
    assert branch_a.is_blocked()
    assert not branch_b.is_blocked()


def test_it_mutes_the_whole_branch_not_just_the_slot(scene):
    """Suppressing only the chunk plugged into the slot would leave the rest of that branch
    still authoring into the composed stage."""
    stage, sender = scene
    upstream = _place(sender, "Upstream", "/b", 7.0)
    tail = _mk(sender, transform_mod, "Tail", "transform")
    tail.get_param("primpath_location").value = "/b"
    tail.get_param("tx").value = 3.0
    _wire(stage, tail, upstream)
    tail.execute()

    live = _place(sender, "Live", "/a", 5.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, live, tail)
    sw.get_param("index").value = 0
    sw.execute()

    assert upstream.is_blocked(), "the branch's interior stayed live"
    assert tail.is_blocked()


def test_a_chunk_feeding_both_branches_stays_live(scene):
    """A diamond: the shared source is upstream of the muted branch *and* the chosen one,
    so muting it would break the branch that was selected."""
    stage, sender = scene
    shared = _place(sender, "Shared", "/a", 5.0)

    kept = _mk(sender, transform_mod, "Kept", "transform")
    kept.get_param("primpath_location").value = "/a"
    _wire(stage, kept, shared)
    kept.execute()

    dropped = _mk(sender, transform_mod, "Dropped", "transform")
    dropped.get_param("primpath_location").value = "/b"
    dropped.get_param("tz").value = 7.0
    _wire(stage, dropped, shared)
    dropped.execute()

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, kept, dropped)
    sw.get_param("index").value = 0
    sw.execute()

    assert not shared.is_blocked()
    assert dropped.is_blocked()
    assert _height(stage, "/a") == pytest.approx(5.0)


def test_a_hand_disabled_chunk_keeps_its_disable(scene):
    """DISABLED belongs to the user. The Switch suppresses with a block instead, so
    selecting a branch never clears a disable someone set by hand -- the two levers are
    independent, which is why no bookkeeping is needed to keep them apart."""
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)
    branch_a.set_state(ChunkFlags.State.DISABLED)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 0   # select the branch the user disabled
    sw.execute()

    assert branch_a.has_state(ChunkFlags.State.DISABLED), "the user's own disable was cleared"


def test_an_index_past_the_last_slot_clamps(scene):
    """Out of range picks the last connected branch rather than muting everything."""
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = 99
    sw.execute()

    assert not branch_b.is_blocked()
    assert branch_a.is_blocked()


def test_a_switch_with_nothing_wired_does_nothing(scene):
    stage, sender = scene
    sw = _mk(sender, switch_mod, "Switch", "switch")
    sw.execute()  # must not raise


def test_the_index_can_be_driven_by_an_expression(scene):
    """A Wedge picking the branch, not just the values inside one.

    This is the pairing that makes a sweep vary *structure*: different geometry, different
    lighting rigs, different asset variants per index, rather than the same network with
    different numbers in it.
    """
    stage, sender = scene
    branch_a = _place(sender, "A", "/a", 5.0)
    branch_b = _place(sender, "B", "/b", 7.0)

    wedge = _mk(sender, importlib.import_module("wedge"), "Wedge", "wedge")
    wedge.get_param("seed").value = 4
    # 0..2, not 0..1: a range samples the half-open interval and the index truncates, so
    # "branch 0 1" would floor every draw to slot 0. Driving an index from a wedge means
    # [0, number-of-slots).
    wedge.get_param("ranges").value = "branch 0 2"

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, branch_a, branch_b)
    sw.get_param("index").value = "@Wedge.branch@"

    seen = set()
    for i in range(12):
        wedge.get_param("current_index").value = i
        wedge.execute()
        sw.execute()
        seen.add(0 if branch_b.is_blocked() else 1)

    assert seen == {0, 1}, (
        f"the wedge never flipped the switch: only branch {seen} was ever selected"
    )


def test_an_unwired_branch_is_released_even_when_it_is_more_than_one_chunk_deep(scene):
    """The `muted` record exists so a branch that stops being upstream is un-muted.

    It records the whole branch, since the walk is transitive -- that is the point of it --
    but the release only ever searched the Switch's DIRECT inputs, so anything further up a
    branch was never found and stayed blocked and muted for the rest of the session, with
    no way back. Only a one-chunk branch was ever released.
    """
    stage, sender = scene
    a = _place(sender, "a_move", "/a", 1.0)
    # Branch B is two deep: b_inner feeds b_slot, and b_slot is what the Switch sees.
    b_inner = _place(sender, "b_inner", "/b", 5.0)
    b_slot = _place(sender, "b_slot", "/b", 7.0)
    _wire(stage, b_slot, b_inner)

    sw = _mk(sender, switch_mod, "switch", "switch")
    _wire(stage, sw, a, b_slot)
    sw.get_param("index").value = 0
    sw.execute()
    assert b_inner.is_blocked(), "branch B is not selected, so the whole branch is muted"

    # The user unwires branch B from the Switch entirely.
    _wire(stage, sw, a)
    sw.execute()

    assert not b_inner.is_blocked(), (
        "nothing is suppressing b_inner any more -- it is not upstream of the Switch at all"
    )
    assert _height(stage, "/b") == 7.0 + 5.0, "and its edit composes again"


def test_the_selected_branch_keeps_an_upstream_switchs_choice(scene):
    """A Stage Expression Switch upstream of the chosen input has muted the branch it did
    not pick; selecting that input must not un-mute it along with everything else."""
    stage, sender = scene
    expr_mod = importlib.import_module("stage_expression_switch")
    day = _place(sender, "Day", "/a", 5.0)
    night = _place(sender, "Night", "/b", 7.0)

    inner = expr_mod.main()
    inner._config = ChunkConfig(module="stage_expression_switch", exec_inputs=2)
    inner.name = "ShotSwitch"
    sender.add_chunk(inner)
    chunk_utils.set_stage_expression_variables(stage, {"SHOT": "sh010_day"})
    inner.get_param(expr_mod.VARIABLE_PARAM).value = "SHOT"
    inner.get_param(expr_mod.PATTERN_PARAM).value = "*_night"
    # Positional: the day branch is the normal path, the night branch the matched one.
    inner_prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(inner)))
    for i, src in enumerate((day, night)):
        name = chunk_stage.exec_input_name(i)
        attr = inner_prim.GetAttribute(name) or inner_prim.CreateAttribute(
            name, Sdf.ValueTypeNames.Token, custom=False
        )
        attr.SetConnections([
            Sdf.Path(f"{chunk_stage.get_chunk_prim_path(src)}.{chunk_stage._EXEC_OUTPUT}")
        ])
    inner.execute()
    assert night.is_blocked()

    sw = _mk(sender, switch_mod, "Switch", "switch")
    _wire(stage, sw, inner)
    sw.get_param("index").value = 0
    sw.execute()

    assert night.is_blocked(), "the inner switch's choice must stand"
    assert _height(stage, "/b") == pytest.approx(0.0)
    assert _height(stage, "/a") == pytest.approx(5.0)
