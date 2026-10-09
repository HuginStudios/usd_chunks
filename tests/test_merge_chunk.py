"""Merge chunk: numbered exec input slots, fan-in wiring, slot compaction and the
first-input-wins ordering.

Chunks are built straight from their modules (as tests/test_new_chunks.py does) rather than
through registry discovery, so nothing here depends on USD_CHUNK_DIRS being set.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkBase, ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import (
    _CHUNKS_ROOT,
    exec_input_index,
    exec_input_name,
    view_cook_list,
    is_exec_input_name,
    is_merge_chunk,
    is_merge_prim,
    iter_exec_inputs,
    normalize_merge_slots,
)
from chunk_prim_authoring import _author_prim
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")


def _load(module_path: str):
    return importlib.import_module(module_path)


def _mk_chunk(module_name: str):
    """A chunk built from its module, configured as if discovery had loaded it."""
    mod = _load(f"chunks.core.{module_name}")
    c = mod.main()
    c._config = ChunkConfig(module=module_name)
    return c


# ---------------------------------------------------------------------------
# Slot naming helpers -- pure functions, no stage needed
# ---------------------------------------------------------------------------

class TestSlotNaming:
    def test_slot_zero_is_the_plain_name(self):
        """Slot 0 deliberately keeps inputs:chunk so every consumer that only looks up the
        one attribute still sees the merge's primary branch."""
        assert exec_input_name(0) == "inputs:chunk"

    def test_extra_slots_are_numbered_from_one(self):
        assert exec_input_name(1) == "inputs:chunk1"
        assert exec_input_name(2) == "inputs:chunk2"

    def test_recognises_plain_and_numbered(self):
        assert is_exec_input_name("inputs:chunk")
        assert is_exec_input_name("inputs:chunk1")
        assert is_exec_input_name("inputs:chunk42")

    def test_rejects_non_exec_inputs(self):
        assert not is_exec_input_name("inputs:primpath_location")
        assert not is_exec_input_name("outputs:chunk")
        assert not is_exec_input_name("inputs:chunky")
        # "inputs:chunk0" is never authored -- slot 0 is the plain name -- so accepting it
        # would let two different names claim the same slot.
        assert not is_exec_input_name("inputs:chunk0")

    def test_index_round_trips(self):
        for i in (0, 1, 2, 9, 10, 137):
            assert exec_input_index(exec_input_name(i)) == i


# ---------------------------------------------------------------------------
# The chunk module itself
# ---------------------------------------------------------------------------

class TestMergeModule:
    def setup_method(self):
        self.mod = _load("chunks.core.merge")

    def test_main_returns_chunk(self):
        c = self.mod.main()
        assert isinstance(c, ChunkBase)
        assert c.name == "Merge"

    def test_no_params(self):
        assert len(self.mod.main().get_my_params()) == 0

    def test_config_category(self):
        assert self.mod.config().category == chunk.StandardCategory.CORE

    def test_execute_authors_nothing(self):
        """A merge is a pure topology marker -- execute must not raise and must not leave an
        opinion behind."""
        layer = Sdf.Layer.CreateAnonymous()

        class _FakeNode:
            def get_edit_context(self):
                return layer

        self.mod.execute(_FakeNode())
        assert layer.empty

    def test_execute_survives_no_edit_layer(self):
        class _FakeNode:
            def get_edit_context(self):
                return None

        self.mod.execute(_FakeNode())  # must not raise

    def test_is_merge_chunk(self):
        assert is_merge_chunk(_mk_chunk("merge"))
        assert not is_merge_chunk(_mk_chunk("dot"))


# ---------------------------------------------------------------------------
# Prim shape
# ---------------------------------------------------------------------------

class TestMergePrim:
    def test_chunk_type(self):
        stage = Usd.Stage.CreateInMemory()
        _author_prim(stage, _mk_chunk("merge"))
        prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Merge")
        assert prim.GetAttribute("info:chunkType").Get() == "merge"
        assert is_merge_prim(prim)

    def test_has_exec_output(self):
        stage = Usd.Stage.CreateInMemory()
        _author_prim(stage, _mk_chunk("merge"))
        prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Merge")
        assert prim.GetAttribute("outputs:chunk").IsValid()

    def test_unwired_merge_has_no_exec_input_attribute(self):
        """The trailing empty slot is a UI affordance only. Authoring an unconnected
        inputs:chunkN would serialize junk into every saved file."""
        stage = Usd.Stage.CreateInMemory()
        _author_prim(stage, _mk_chunk("merge"))
        prim = stage.GetPrimAtPath(f"{_CHUNKS_ROOT}/Merge")
        assert iter_exec_inputs(prim) == []

    def test_iter_exec_inputs_sorts_numerically_not_alphabetically(self):
        """GetAttributes() is alphabetical, which puts chunk10 before chunk2. Slot order
        decides which branch wins, so a string sort would silently mis-rank a wide merge."""
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/M", "Chunk")
        prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("merge")
        for i in (10, 2, 1, 0):
            prim.CreateAttribute(exec_input_name(i), Sdf.ValueTypeNames.Token, custom=False)
        assert [a.GetName() for a in iter_exec_inputs(prim)] == [
            "inputs:chunk", "inputs:chunk1", "inputs:chunk2", "inputs:chunk10",
        ]


# ---------------------------------------------------------------------------
# Slot compaction
# ---------------------------------------------------------------------------

def _merge_prim_with_slots(stage, sources):
    """A merge prim wired to `sources` (a list of prim paths), one per slot."""
    prim = stage.DefinePrim("/M", "Chunk")
    prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("merge")
    for i, src in enumerate(sources):
        attr = prim.CreateAttribute(exec_input_name(i), Sdf.ValueTypeNames.Token, custom=False)
        attr.SetConnections([Sdf.Path(f"{src}.outputs:chunk")])
    return prim


class TestCompaction:
    def test_dense_slots_are_left_alone(self):
        stage = Usd.Stage.CreateInMemory()
        prim = _merge_prim_with_slots(stage, ["/A", "/B", "/C"])
        assert normalize_merge_slots(stage, prim) is False

    def test_clearing_a_middle_slot_shifts_the_rest_up(self):
        stage = Usd.Stage.CreateInMemory()
        prim = _merge_prim_with_slots(stage, ["/A", "/B", "/C"])
        prim.GetAttribute("inputs:chunk1").ClearConnections()

        assert normalize_merge_slots(stage, prim) is True

        assert [a.GetName() for a in iter_exec_inputs(prim)] == ["inputs:chunk", "inputs:chunk1"]
        assert prim.GetAttribute("inputs:chunk").GetConnections() == [Sdf.Path("/A.outputs:chunk")]
        # C was in slot 2 and moves up into the hole B left.
        assert prim.GetAttribute("inputs:chunk1").GetConnections() == [Sdf.Path("/C.outputs:chunk")]
        assert not prim.GetAttribute("inputs:chunk2").GetConnections()

    def test_clearing_the_first_slot_promotes_the_second(self):
        """Whoever ends up in slot 0 is the strongest opinion, so this is a real semantic
        change, not just bookkeeping."""
        stage = Usd.Stage.CreateInMemory()
        prim = _merge_prim_with_slots(stage, ["/A", "/B"])
        prim.GetAttribute("inputs:chunk").ClearConnections()

        assert normalize_merge_slots(stage, prim) is True
        assert prim.GetAttribute("inputs:chunk").GetConnections() == [Sdf.Path("/B.outputs:chunk")]
        assert not prim.GetAttribute("inputs:chunk1").GetConnections()

    def test_connectionless_trailing_attribute_is_left_alone(self):
        """Deliberate: normalization runs synchronously from the notice CreateAttribute
        itself fires, so deleting a connectionless attribute would pull it out from under
        every authoring path that creates the attribute and then connects it."""
        stage = Usd.Stage.CreateInMemory()
        prim = _merge_prim_with_slots(stage, ["/A"])
        prim.CreateAttribute("inputs:chunk1", Sdf.ValueTypeNames.Token, custom=False)

        assert normalize_merge_slots(stage, prim) is False
        assert prim.GetAttribute("inputs:chunk1").IsValid()

    def test_create_then_connect_survives_normalization(self):
        """The regression guard for that: an empty merge must let CreateAttribute stand long
        enough for the caller to connect it."""
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/M", "Chunk")
        prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("merge")

        attr = prim.CreateAttribute("inputs:chunk", Sdf.ValueTypeNames.Token, custom=False)
        normalize_merge_slots(stage, prim)  # as the stage notice would
        assert attr.IsValid()
        attr.SetConnections([Sdf.Path("/A.outputs:chunk")])
        assert prim.GetAttribute("inputs:chunk").GetConnections() == [Sdf.Path("/A.outputs:chunk")]

    def test_non_merge_is_never_touched(self):
        """An ordinary chunk holding several targets on inputs:chunk is not a merge and must
        not be rewritten into slots."""
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/N", "Chunk")
        prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("transform")
        attr = prim.CreateAttribute("inputs:chunk", Sdf.ValueTypeNames.Token, custom=False)
        attr.SetConnections([Sdf.Path("/A.outputs:chunk"), Sdf.Path("/B.outputs:chunk")])

        assert normalize_merge_slots(stage, prim) is False
        assert len(prim.GetAttribute("inputs:chunk").GetConnections()) == 2


# ---------------------------------------------------------------------------
# Dynamic exec inputs shared with Switch
#
# A Switch needs the same growing-numbered-slot machinery as Merge -- one port per wired
# branch, dense and compacted -- so the Qt node graph doesn't fall back to drawing it with a
# single fixed exec port (where a second wire dropped on it silently overwrites the first).
# ---------------------------------------------------------------------------

class TestDynamicExecInputsGeneralizesToSwitch:
    def test_switch_has_dynamic_exec_inputs(self):
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/S", "Chunk")
        prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("switch")
        assert chunk_stage.has_dynamic_exec_inputs_prim(prim)

    def test_an_ordinary_chunk_does_not(self):
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/N", "Chunk")
        prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("transform")
        assert not chunk_stage.has_dynamic_exec_inputs_prim(prim)

    def test_a_switchs_slots_compact_the_same_way_a_merges_do(self):
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/S", "Chunk")
        prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set("switch")
        for i, src in enumerate(["/A", "/B", "/C"]):
            attr = prim.CreateAttribute(exec_input_name(i), Sdf.ValueTypeNames.Token, custom=False)
            attr.SetConnections([Sdf.Path(f"{src}.outputs:chunk")])
        prim.GetAttribute("inputs:chunk1").ClearConnections()

        assert normalize_merge_slots(stage, prim) is True
        assert [a.GetName() for a in iter_exec_inputs(prim)] == ["inputs:chunk", "inputs:chunk1"]
        assert prim.GetAttribute("inputs:chunk").GetConnections() == [Sdf.Path("/A.outputs:chunk")]
        assert prim.GetAttribute("inputs:chunk1").GetConnections() == [Sdf.Path("/C.outputs:chunk")]


# ---------------------------------------------------------------------------
# Live wiring -- exercises ChunkStageSync's authoring and DAG reconciliation
# ---------------------------------------------------------------------------

@pytest.fixture
def wired_merge():
    """A merge fed by three chunks, wired the way the node graph wires: the connection is
    authored straight onto the stage, and the Python DAG is expected to follow from it.

    That direction is deliberate -- it is the primary authoring path (ChunkGraphView's
    drag-to-connect never calls add_dependency; chunk_stage mirrors the stage into the DAG
    instead), and it is the direction the numbered slots had to be taught.
    """
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    branches = []
    for name in ("A", "B", "C"):
        c = _mk_chunk("dot")
        c.name = name
        scene.add_chunk(c)
        branches.append(c)
    merge = _mk_chunk("merge")
    scene.add_chunk(merge)

    prim = stage.GetPrimAtPath(Sdf.Path(chunk_stage.get_chunk_prim_path(merge)))
    # Author into the session layer, where interactively wired connections actually live.
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for i, branch in enumerate(branches):
            attr = prim.CreateAttribute(
                exec_input_name(i), Sdf.ValueTypeNames.Token, custom=False
            )
            attr.SetConnections(
                [Sdf.Path(f"{chunk_stage.get_chunk_prim_path(branch)}.outputs:chunk")]
            )
    chunk_stage.reconcile_dag_dependencies(merge)

    yield stage, scene, merge, branches, prim


class TestLiveWiring:
    def test_each_branch_lands_in_its_own_slot(self, wired_merge):
        _stage, _scene, _merge, branches, prim = wired_merge
        names = [a.GetName() for a in iter_exec_inputs(prim)]
        assert names == ["inputs:chunk", "inputs:chunk1", "inputs:chunk2"]
        for attr, branch in zip(iter_exec_inputs(prim), branches):
            assert [str(c) for c in attr.GetConnections()] == [
                f"{chunk_stage.get_chunk_prim_path(branch)}.outputs:chunk"
            ]

    def test_every_branch_becomes_a_real_dag_dependency(self, wired_merge):
        """The regression guard for the silent bug: exec_dependencies read only
        inputs:chunk, so a branch wired into slot 1 or 2 authored USD and drew an edge while
        the Python DAG never gained the edge -- no INPUT_CHANGED, so editing that branch never
        recooked the merge."""
        _stage, _scene, merge, branches, _prim = wired_merge
        deps = {id(d) for _, d in merge.get_dependencies()}
        for branch in branches:
            assert id(branch) in deps

    def test_reconcile_drops_a_branch_when_its_slot_is_cleared(self, wired_merge):
        """Reconciliation is a diff, not an append -- clearing slot 1 must remove exactly that
        dependency and leave the others."""
        _stage, _scene, merge, branches, prim = wired_merge
        with Usd.EditContext(_stage, _stage.GetSessionLayer()):
            prim.GetAttribute("inputs:chunk1").ClearConnections()
        chunk_stage.reconcile_dag_dependencies(merge)

        deps = {id(d) for _, d in merge.get_dependencies()}
        assert id(branches[0]) in deps
        assert id(branches[2]) in deps
        assert id(branches[1]) not in deps

    def test_live_dependencies_read_back_every_slot(self, wired_merge):
        _stage, _scene, merge, branches, _prim = wired_merge
        live = chunk_stage.exec_dependencies(merge)
        assert {id(c) for c in live} == {id(b) for b in branches}

    def test_first_input_is_cooked_last_so_it_wins(self, wired_merge):
        """Layer strength is cook_list position (recook_for_view ranks later = stronger), and
        visit() appends a node after its sources -- so slot 0 must be visited LAST."""
        stage, scene, merge, branches, _prim = wired_merge
        order = view_cook_list(merge, stage, scene)
        positions = [order.index(b) for b in branches]
        # branches[0] is slot 0 and must rank highest of the three.
        assert positions[0] > positions[1] > positions[2]

    def test_all_branches_are_in_the_cook_set(self, wired_merge):
        stage, scene, merge, branches, _prim = wired_merge
        order = view_cook_list(merge, stage, scene)
        for branch in branches:
            assert branch in order

    def test_every_slot_leads_downstream_to_the_merge(self, wired_merge):
        """Every slot counts going forward too, not only slot 0."""
        stage, scene, merge, branches, _prim = wired_merge
        for branch in branches:
            assert merge in chunk_stage.exec_downstream(branch, stage, scene)

    def test_branches_are_not_exec_tails(self, wired_merge):
        """A chunk feeding slot 2 is as consumed as one feeding slot 0."""
        stage, scene, _merge, branches, _prim = wired_merge
        tails = chunk_stage.exec_tails(stage, scene)
        for branch in branches:
            assert branch not in tails
