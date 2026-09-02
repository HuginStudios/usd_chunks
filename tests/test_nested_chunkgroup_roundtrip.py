"""A chunkgroup inside another must come back inside it.

load_chunks sends every CONTEXT chunk to scene.add_chunk, which is the scene root --
so a nested chunkgroup was restored as a SIBLING of the group it was saved in, lost
the name collision with it, and was renamed. Its contents came along to the new
address, leaving the parent empty.

That also broke cook order. A group's inner chunks are cooked in prim-path depth
order, so an upstream Sublayer at one depth runs before a BindMaterial nested deeper
that reads what it brings onto the stage. Flattening the nesting made them siblings
at the same depth, the sort had nothing left to order by, and a reopened scene kept
its original material.

The save side was never wrong: the file holds the full nesting.
"""
from __future__ import annotations

import weakref

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkBase, SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins.chunk_usd import (
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
    save_scene,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return s


def _mk(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _contexts(scene):
    return [
        c for c in scene.chunks.get_my_params()
        if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT)
    ]


@pytest.fixture
def reopened(qapp, tmp_path):
    """A group inside the default group, holding a chunk -- saved and reopened."""
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    group = _mk(scene, "chunkgroup")
    scene._current_context = weakref.ref(group)
    _mk(scene, "prim")
    scene._current_context = None
    qapp.processEvents()

    live = sorted(get_chunk_prim_path(c) for c in scene.get_chunks())

    out = tmp_path / "nested.usda"
    save_scene(scene, out)

    stage2 = Usd.Stage.Open(str(out))
    scene2 = _scene_for(stage2)
    qapp.processEvents()
    return live, scene2, stage2, out


def test_the_saved_file_holds_the_nesting(reopened):
    """The save side was never the problem -- pinned so a fix cannot drift there."""
    _live, _scene2, _stage2, out = reopened

    stage = Usd.Stage.Open(str(out))
    paths = [str(p.GetPath()) for p in stage.TraverseAll()]
    assert any(p.count("/") == 4 for p in paths), (
        f"expected a chunk nested two groups deep in the file, got {paths}"
    )


def test_a_nested_group_comes_back_nested(reopened):
    """The group stays inside its parent, and its contents stay inside it.

    Asserted structurally rather than as exact path equality, because one thing is still
    not right: a nested group whose name matches its parent's comes back with a
    deduplicating "1" suffix. That is worth fixing on its own -- expressions address
    chunks by name -- but it no longer moves anything, which is what broke cook order.
    """
    live, scene2, _stage2, _out = reopened

    depth_by_name = {p.rsplit("/", 1)[-1]: p.count("/") for p in live}

    for c in scene2.get_chunks():
        path = get_chunk_prim_path(c)
        assert path.startswith("/chunks/"), path

    prim_chunk = next(c for c in scene2.get_chunks() if c.name == "Prim")
    prim_path = get_chunk_prim_path(prim_chunk)
    assert prim_path.count("/") == depth_by_name["Prim"], (
        f"the chunk must come back at the depth it was saved at, not hoisted: {prim_path}"
    )

    group_path = prim_path.rsplit("/", 1)[0]
    assert group_path.count("/") == 3, (
        f"its group must still sit inside the default group, not beside it: {group_path}"
    )


def test_the_reopened_scene_has_one_default_group(reopened):
    """A nested group restored at the root collides with the default and gets renamed."""
    _live, scene2, _stage2, _out = reopened

    names = [c.name for c in _contexts(scene2)]
    assert not any(n.endswith("1") for n in names), (
        f"a collision-renamed context means one was restored to the wrong parent: {names}"
    )
