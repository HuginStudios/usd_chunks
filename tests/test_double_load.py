"""Loading the same network twice in one process restores it twice.

It used to restore only the context the second time, and silently: a batch tool that loads
a network, renders it, then loads it again for the next configuration rendered the *first*
configuration twice with nothing indicating a problem.

The cause was not in loading. `reconcile_edit_targets` runs during load to home each chunk's
bookkeeping, and `_migrate_chunk_bookkeeping` moves a spec by DELETING the original -- which
on a freshly opened file is the spec in that file's own root layer. The composed stage stayed
correct (the spec now lived in the session layer) so the first load looked fine, but Sdf
caches layers per process, so the *next* `Usd.Stage.Open` of that path received the emptied
layer along with a fresh, empty session layer. The file on disk was never touched.
"""
from __future__ import annotations

from pxr import Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import (
    _CHUNK_PRIM_TYPENAME,
    export_chunks,
    initialize as chunk_usd_init,
)

from chunk_stubs import attach_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _session(stage) -> _Scene:
    """A fresh scene over stage, wired the way opening a file wires one."""
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_init(stage, scene)  # runs load_chunks internally
    return scene


def _authored_network(path) -> list[str]:
    stage = Usd.Stage.CreateInMemory()
    scene = _session(stage)
    for i in range(3):
        c = chunk.Chunk(f"node{i}")
        attach_stub_module(c)
        scene.add_chunk(c)
    export_chunks(scene, path)
    return sorted(c.name for c in scene.get_chunks())


def test_loading_the_same_file_twice_restores_it_twice(tmp_path) -> None:
    path = str(tmp_path / "net.usda")
    authored = _authored_network(path)

    restored = []
    for _ in range(3):
        scene = _session(Usd.Stage.Open(path))
        restored.append(sorted(c.name for c in scene.get_chunks()))

    assert restored[0] == authored
    assert restored[1] == restored[0], "the second load must restore the whole network"
    assert restored[2] == restored[0], "and so must every load after it"


def test_loading_does_not_empty_the_file_s_root_layer(tmp_path) -> None:
    """The direct cause, asserted on its own: opening a file must not rewrite it.

    Checked on the root LAYER rather than the composed stage -- the stage stayed correct
    even while the layer was being gutted, which is why this went unnoticed.
    """
    path = str(tmp_path / "net.usda")
    _authored_network(path)

    stage = Usd.Stage.Open(path)
    root = stage.GetRootLayer()
    before = sorted(
        str(p.GetPath()) for p in stage.Traverse()
        if p.GetTypeName() == _CHUNK_PRIM_TYPENAME
    )
    assert before, "precondition: the exported file has chunk prims"

    _session(stage)

    still_there = sorted(p for p in before if root.GetPrimAtPath(p) is not None)
    assert still_there == before, "load unauthored chunk prims from the file's own layer"


def test_the_second_stage_shares_the_cached_layer(tmp_path) -> None:
    """Why the damage carries across loads at all, pinned so the premise can't rot.

    If Sdf ever stopped handing back the same layer object for one path, the bug above
    would vanish for a reason unrelated to the fix, and this test says so out loud.
    """
    path = str(tmp_path / "net.usda")
    _authored_network(path)

    first = Usd.Stage.Open(path)
    second = Usd.Stage.Open(path)

    assert first.GetRootLayer() is second.GetRootLayer()
