"""Deleting a chunkgroup must take the content of chunks nested inside it with it.

Reported: a chunkgroup containing chunkgroup/prim, deleted, and /prim is still on the
stage. The delete cascade runs through ChunkContainerBase.request_delete ->
get_my_params(), which reaches a plain chunk created inside a group -- that really is a
param of it. A nested CHUNKGROUP is not: it nests by prim path, as a context in its own
right, so the cascade never reached it, nor anything downstream of CHUNK_DELETED, and its
leaves kept their layers.

It hid well. The group's own prim removal takes the whole subtree's prim specs with it, so
the graph looks right, and a recook usually mutes the orphaned layer -- so the content
appears to be gone right up until something un-mutes it or the scene is reloaded.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def nested(qapp):
    """A one-deep group and a two-deep one, each with a Prim chunk at the bottom."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.show()
    qapp.processEvents()

    def settle():
        for _ in range(30):
            qapp.processEvents()
            QtCore.QThread.msleep(10)
        qapp.processEvents()

    def mk(key, context=None):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        prev = scene._current_context
        scene._current_context = weakref.ref(context) if context is not None else None
        try:
            for c in cs:
                scene.add_chunk(c)
        finally:
            scene._current_context = prev
        return cs[0]

    shallow = mk("chunkgroup")
    shallow_leaf = mk("prim", context=shallow)
    shallow_leaf.get_param("primpath_location").value = "/one_deep"
    settle()

    outer = mk("chunkgroup")
    inner = mk("chunkgroup", context=outer)
    deep_leaf = mk("prim", context=inner)
    deep_leaf.get_param("primpath_location").value = "/two_deep"
    settle()

    assert stage.GetPrimAtPath("/one_deep").IsValid()
    assert stage.GetPrimAtPath("/two_deep").IsValid()
    return dict(stage=stage, lm=lm, settle=settle, shallow=shallow, outer=outer,
                shallow_leaf=shallow_leaf, deep_leaf=deep_leaf, inner=inner, scene=scene)


def test_a_chunk_one_level_down_is_torn_down(nested):
    """The case that already worked -- pinned so a fix cannot trade it away."""
    nested["shallow"].request_delete()
    nested["settle"]()

    assert nested["lm"].get_layer(nested["shallow_leaf"]) is None
    assert not nested["stage"].GetPrimAtPath("/one_deep").IsValid()


def test_a_chunk_two_levels_down_is_torn_down(nested):
    """The reported case: a chunkgroup inside the chunkgroup being deleted."""
    lm, stage = nested["lm"], nested["stage"]
    leaf = nested["deep_leaf"]

    nested["outer"].request_delete()
    nested["settle"]()

    layer = lm.get_layer(leaf)
    assert layer is None, "the leaf's layer must be dropped, not left owning /two_deep"
    assert not stage.GetPrimAtPath("/two_deep").IsValid()


def test_the_nested_group_itself_is_deleted(nested):
    """Not just its content -- the group is a chunk too, and would be left orphaned."""
    nested["outer"].request_delete()
    nested["settle"]()

    assert getattr(nested["inner"], "_deleting", False), (
        "the nested chunkgroup was never asked to delete"
    )
