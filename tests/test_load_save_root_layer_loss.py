"""Opening a network and saving it must not empty the file.

A network containing an `edit_target` chunk loses every chunk prim from its root layer the
moment it is opened and saved. Three prims go in, one comes out. The composed stage still
shows all three in memory -- the specs have been removed from the ROOT layer and now
resolve from elsewhere -- so nothing looks wrong until the save lands, and then the file on
disk is gutted.

Reproduced with no frontend at all: `Usd.Stage.Open` + `load_chunks` + `Save()`. It is not
a graph, Qt, Flet or ImGui problem. Verified present on `main` as well as on the branch
this test was written on, so it is pre-existing rather than a regression -- but it is
reachable from every frontend, because saving after a load is what happens when a user
drags a node or runs auto-layout.

Related history: docs record an edit-target migration during load deleting specs from the
opened file's layer, fixed 2026-09-02. This is the same shape and is either a recurrence or
a sibling case that fix did not cover.

Marked xfail(strict=True) rather than deleted or skipped: the suite stays green, the bug
stays recorded, and the day it is fixed this file fails loudly and tells whoever fixed it
to drop the marker.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.plugins.chunk_usd import export_chunks, initialize, load_chunks


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

    @pytest.mark.xfail(strict=True, reason="edit_target chunk: load empties the root layer")
    def test_an_edit_target_network_survives(self, tmp_path):
        """The bug. Remove the xfail marker when this starts passing."""
        path = str(tmp_path / "et.usda")
        before = _build(path, ("transform", "edit_target"))
        _open_and_save(path)
        assert _count(path) == before

    def test_the_loss_is_invisible_until_the_save(self, tmp_path):
        """Why it is dangerous rather than merely wrong.

        The composed stage still reports every chunk after the load, so a frontend has no
        way to notice: the specs are gone from the ROOT layer while still resolving from
        somewhere else. Only writing that layer reveals it.
        """
        path = str(tmp_path / "et2.usda")
        before = _build(path, ("transform", "edit_target"))
        in_memory = _open_and_save(path)
        assert in_memory >= before, "the stage would have to look wrong for this to be catchable"
        assert _count(path) < before, "if this stops holding, the bug is fixed -- see above"
