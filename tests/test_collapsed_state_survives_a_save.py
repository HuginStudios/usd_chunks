"""A collapsed node comes back collapsed.

Live-reported: collapse a few nodes, save, reopen, and the graph is fully expanded again --
every node's tidying undone. Disabling was already recorded (as active=false on the prim)
and read back on load; collapsing was recorded nowhere at all.

It is authored through UsdUINodeGraphNodeAPI's expansionState, the same schema the node's
position already uses, so the file says what it means to anything that reads that schema
rather than only to us.
"""

from __future__ import annotations

import pathlib

import pytest
from pxr import Usd, UsdUI

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import (
    get_chunk_prim_path,
    initialize as chunk_stage_initialize,
    save_scene,
)

from chunk_stubs import attach_stub_module

EXPANSION = UsdUI.Tokens.uiNodegraphNodeExpansionState


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage_initialize(stage, scene)
    return scene


def _add(scene, name):
    c = chunk.Chunk(name)
    attach_stub_module(c)
    c.add_param(chunk.LineEdit("primpath_location", default_value=f"/{name}"))
    scene.add_chunk(c)
    return c


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    yield _scene_for(stage), stage


def _reopen(path):
    chunk_registry.ChunkLayerManager().reset()
    stage = Usd.Stage.Open(str(path))
    return _scene_for(stage), stage


def _by_name(scene):
    return {c.name: c for c in scene.get_chunks()}


def test_a_collapsed_node_is_still_collapsed_after_a_reopen(scene, tmp_path) -> None:
    sc, _ = scene
    _add(sc, "Open")
    closed = _add(sc, "Closed")
    closed.set_state(ChunkFlags.State.COLLAPSED)

    out = pathlib.Path(tmp_path) / "collapsed.usda"
    save_scene(sc, out)
    reopened, _ = _reopen(out)

    chunks = _by_name(reopened)
    assert chunks["Closed"].has_state(ChunkFlags.State.COLLAPSED)
    assert not chunks["Open"].has_state(ChunkFlags.State.COLLAPSED), "and an open one stays open"


def test_expanding_again_is_saved_too(scene, tmp_path) -> None:
    """The other direction: tidy up, change your mind, save -- it must not come back
    collapsed from the earlier write."""
    sc, _ = scene
    node = _add(sc, "Node")
    node.set_state(ChunkFlags.State.COLLAPSED)
    node.unset_state(ChunkFlags.State.COLLAPSED)

    out = pathlib.Path(tmp_path) / "expanded.usda"
    save_scene(sc, out)
    reopened, _ = _reopen(out)

    assert not _by_name(reopened)["Node"].has_state(ChunkFlags.State.COLLAPSED)


def test_it_is_written_as_the_usd_schema_says(scene) -> None:
    """Not a private attribute: anything that reads UsdUINodeGraphNodeAPI sees it."""
    sc, stage = scene
    node = _add(sc, "Node")
    node.set_state(ChunkFlags.State.COLLAPSED)

    prim = stage.GetPrimAtPath(get_chunk_prim_path(node))
    assert UsdUI.NodeGraphNodeAPI(prim).GetExpansionStateAttr().Get() == UsdUI.Tokens.closed


def test_an_open_node_authors_nothing(scene) -> None:
    """Open is what a chunk is born as, so writing it would put a line on every chunk prim
    in every scene to say nothing -- the same reasoning chunk:pinned follows."""
    sc, stage = scene
    node = _add(sc, "Node")

    # HasAuthoredValue, not IsValid: applying NodeGraphNodeAPI for the node's position
    # defines this attribute too, so it is valid-but-unauthored on every chunk prim.
    prim = stage.GetPrimAtPath(get_chunk_prim_path(node))
    assert not prim.GetAttribute(EXPANSION).HasAuthoredValue()


def test_expanding_removes_the_opinion(scene) -> None:
    sc, stage = scene
    node = _add(sc, "Node")
    node.set_state(ChunkFlags.State.COLLAPSED)
    prim = stage.GetPrimAtPath(get_chunk_prim_path(node))
    assert prim.GetAttribute(EXPANSION).HasAuthoredValue()

    node.unset_state(ChunkFlags.State.COLLAPSED)

    assert not stage.GetPrimAtPath(get_chunk_prim_path(node)).GetAttribute(EXPANSION).HasAuthoredValue()
