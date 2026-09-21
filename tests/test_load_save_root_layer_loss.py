"""Opening a network and saving it must not empty the file.

A network containing an `edit_target` chunk used to lose every chunk prim from its root
layer the moment it was opened and saved: three prims in, one out. The composed stage still
showed all three, because the specs had been moved out of the ROOT layer and resolved from
where they landed -- so nothing looked wrong until the save wrote the gutted layer.

It reproduced with no frontend at all -- `Usd.Stage.Open` + `load_chunks` + `Save()` -- and
identically on `main`, so it was pre-existing rather than a regression, and reachable from
every frontend: saving after a load is what happens when a user drags a node.

Related history: docs record an edit-target migration during load deleting specs from the
opened file's layer, fixed 2026-09-02. This is the same shape and is either a recurrence or
a sibling case that fix did not cover.

FIXED: publish_edit_target called reconcile_edit_targets without protect_root_layer, and a
load takes that branch every single time -- the Edit Target marker stores a layer
identifier, and an anonymous layer gets a new identifier in every process, so `old_ident !=
ident` even when nothing changed. chunk_stage._loading now scopes the protection to a load.
These tests stay as the regression guard.
"""
from __future__ import annotations


from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import ChunkStageSync, export_chunks, initialize, load_chunks


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def _count(path: str) -> int:
    stage = Usd.Stage.Open(path)
    return sum(1 for p in stage.Traverse() if p.GetTypeName() == "Chunk")


def _build(path: str, modules) -> int:
    """Write a real network and return how many Chunk prims it holds."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    initialize(stage, scene)
    for name in modules:
        module = registry.get_chunk_module(name)
        assert module is not None, f"chunk module {name!r} not found"
        for chunk in (module.create() if module.loaded else module.load()):
            scene.add_chunk(chunk)
    export_chunks(scene, path)
    for chunk in list(scene.get_chunks()):
        chunk.request_delete()
    chunk_registry.ChunkLayerManager().reset()
    return _count(path)


def _open_and_save(path: str) -> int:
    """What every frontend does: open, load, then save because something moved."""
    stage = Usd.Stage.Open(path)
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    initialize(stage, scene)
    load_chunks(stage, scene)
    in_memory = sum(1 for p in stage.Traverse() if p.GetTypeName() == "Chunk")
    stage.GetRootLayer().Save()
    return in_memory


class TestRoundTripKeepsTheFile:
    def test_a_plain_network_survives(self, tmp_path):
        """The control. Without an edit_target chunk the round trip is lossless, which is
        what makes the failure below specific rather than 'saving is broken'."""
        path = str(tmp_path / "plain.usda")
        before = _build(path, ("transform", "transform"))
        _open_and_save(path)
        assert _count(path) == before

    def test_a_reference_and_sublayer_network_survives(self, tmp_path):
        path = str(tmp_path / "mixed.usda")
        before = _build(path, ("reference", "sublayer"))
        _open_and_save(path)
        assert _count(path) == before

    def test_an_edit_target_network_survives(self, tmp_path):
        """The bug this file was written for. Fixed: publish_edit_target reconciled without
        protect_root_layer, and a load takes that branch every time because an anonymous
        layer's identifier is new in each process."""
        path = str(tmp_path / "et.usda")
        before = _build(path, ("transform", "edit_target"))
        _open_and_save(path)
        assert _count(path) == before

    def test_the_root_layer_keeps_its_specs_not_just_the_composed_stage(self, tmp_path):
        """The check that would have caught it, kept as the regression guard.

        Why it needs saying separately: the composed stage reported every chunk even while
        the bug was live, because the specs had been MOVED out of the root layer and still
        resolved from where they landed. Nothing looked wrong until a save wrote the gutted
        layer, so asserting on the stage alone would pass against the broken code.
        """
        path = str(tmp_path / "et2.usda")
        before = _build(path, ("transform", "edit_target"))
        in_memory = _open_and_save(path)
        assert in_memory >= before, "the stage should still resolve every chunk"
        assert _count(path) == before, "the root layer lost specs the stage still showed"


def _open_the_way_usdview_does_and_save(path: str) -> int:
    """usdview registers the plugin before it has a stage, so ChunkStageSync.initialize
    sees None and the actual load runs later from signalStageReplaced ->
    reload_for_stage. A different code path from _open_and_save, with its own reconcile."""
    stage = Usd.Stage.Open(path)
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    sync = ChunkStageSync()
    sync.initialize(None, scene)
    sync.reload_for_stage(stage)
    in_memory = sum(1 for p in stage.Traverse() if p.GetTypeName() == "Chunk")
    stage.GetRootLayer().Save()
    return in_memory


class TestTheHostHandingOverItsStageLater:
    """The same guarantee on the reload path. reload_for_stage reconciled WITHOUT
    protect_root_layer -- the initialize() fix never reached it -- so the usdview open of a
    34-chunk network left 6 Chunk specs in the file's root layer (measured), and re-cooked
    every chunk it had just moved."""

    def test_an_edit_target_network_survives_the_reload_path(self, tmp_path):
        path = str(tmp_path / "et_reload.usda")
        before = _build(path, ("transform", "edit_target"))
        in_memory = _open_the_way_usdview_does_and_save(path)
        assert in_memory >= before, "the stage should still resolve every chunk"
        assert _count(path) == before, "the root layer lost specs the stage still showed"

    def test_a_chained_network_survives_the_reload_path(self, tmp_path):
        """No Edit Target needed: with the session layer as every chunk's resolved target,
        the unprotected pass still moved each chunk's bookkeeping out of the root layer."""
        path = str(tmp_path / "chain_reload.usda")
        before = _build(path, ("transform", "transform", "reference"))
        _open_the_way_usdview_does_and_save(path)
        assert _count(path) == before
