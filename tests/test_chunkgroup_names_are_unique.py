"""A chunkgroup must not take the name of the group it sits inside.

The scene's flat container makes every chunk name unique as it is added, but a CONTEXT is
not in that container -- so the one collision it cannot see is the one that matters most: a
group nested directly inside another taking its parent's name. A new scene produced
/chunks/ChunkGroup/ChunkGroup, two tabs both reading "ChunkGroup", and a context path that
said "ChunkGroup > ChunkGroup".

Restoring from a file is exempt, as it already was: the file is authoritative about what a
group is called, its prim path already makes it unique, and the name is what expressions
reference.
"""
from __future__ import annotations

import weakref

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    sender = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, sender)

    def mk(context=None):
        mod = reg.get_chunk_module("chunkgroup")
        cs = mod.create() if mod.loaded else mod.load()
        prev = sender._current_context
        sender._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                sender.add_chunk(c)
        finally:
            sender._current_context = prev
        qapp.processEvents()
        return cs[0]

    return sender, mk


def test_a_group_does_not_take_its_parents_name(scene):
    sender, mk = scene
    outer = mk()
    root = sender.get_root_context()

    assert root is not None
    assert outer.name != root.name, (
        f"both called {outer.name!r} -- two tabs with one name, and a path that repeats itself"
    )


def test_nesting_keeps_numbering(scene):
    """What you actually want to see going down: ChunkGroup1, 2, 3."""
    sender, mk = scene
    outer = mk()
    middle = mk(outer)
    inner = mk(middle)

    names = [c.name for c in (outer, middle, inner)]
    assert len(set(names)) == 3, f"every level distinct, got {names}"
    root = sender.get_root_context()
    assert root.name not in names


def test_the_prim_paths_still_nest(scene):
    """The names change; where they sit does not."""
    sender, mk = scene
    outer = mk()
    middle = mk(outer)

    assert get_chunk_prim_path(middle).startswith(get_chunk_prim_path(outer) + "/")
