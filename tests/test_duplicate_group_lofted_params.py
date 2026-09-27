"""Duplicating a chunkgroup carries the params it publishes.

A loft creates a param on the GROUP and connects the inner param's attribute up to it.
The copy machinery walks a group's children and their wiring, but a loft is not a wire
between two children -- it points up at the group itself -- so it was discarded as "a
source outside the copied children" and the duplicate came back with an empty interface:
the group's published params simply gone, and nothing on the node to drive its contents.

Re-lofted rather than re-connected, because the pasted group has no such param until
something lofts one.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init

LOFTED = "Transform_tx"
INNER_ATTR = "inputs:translate_grp:tx"


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def duplicated(qapp):
    """A group holding a Transform with tx lofted to the group, then duplicated."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.resize(700, 700)
    docker.show()
    qapp.processEvents()

    def settle():
        for _ in range(25):
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

    group = mk("chunkgroup")
    inner = mk("transform", context=group)
    settle()
    inner.get_param("tx").value = 3.5
    settle()
    chunk_stage.loft_param_to_context(inner.get_param("tx"), group)
    settle()

    graph_view = docker.tab_widget.get_current_graph_view()
    scene.set_selected_chunks([group])
    graph_view._duplicate_selected()
    settle()

    # get_context() returns self for a context chunk, so it cannot be used to tell the
    # duplicate from the original -- identity is the only thing that separates them.
    copy = next(
        (c for c in scene.get_chunks() if chunk_stage.is_chunkgroup(c) and c is not group), None
    )
    assert copy is not None, "the duplicate itself must exist"
    return dict(stage=stage, scene=scene, group=group, inner=inner, copy=copy)


def _connections(stage, chunk, attr_name):
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    attr = prim.GetAttribute(attr_name)
    return [str(c) for c in attr.GetConnections()] if attr and attr.IsValid() else []


def test_the_copy_publishes_the_same_param(duplicated):
    copy, stage = duplicated["copy"], duplicated["stage"]

    assert copy.get_param(LOFTED) is not None, "the duplicate has none of the group's interface"
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(copy)))
    assert prim.GetAttribute(f"inputs:{LOFTED}").IsValid()


def test_the_lofted_value_comes_across(duplicated):
    assert duplicated["copy"].get_param(LOFTED).value == 3.5


def test_the_copy_drives_its_own_child(duplicated):
    """The copy's inner param must point at the COPY's interface.

    Wired to the original's instead, the two groups would share one control -- editing the
    duplicate would move the original's geometry.
    """
    stage, copy = duplicated["stage"], duplicated["copy"]
    inner_copies = [
        c for c in duplicated["scene"].get_chunks() if c.get_context() is copy and c is not copy
    ]
    assert len(inner_copies) == 1

    conns = _connections(stage, inner_copies[0], INNER_ATTR)
    assert conns == [f"{get_chunk_prim_path(copy)}.inputs:{LOFTED}"], (
        f"the copy's child should be lofted to the copy, got {conns}"
    )


def test_the_original_is_untouched(duplicated):
    """Pinned: the duplicate must not steal or rewrite the original's own loft."""
    stage, group = duplicated["stage"], duplicated["group"]

    assert group.get_param(LOFTED) is not None
    conns = _connections(stage, duplicated["inner"], INNER_ATTR)
    assert conns == [f"{get_chunk_prim_path(group)}.inputs:{LOFTED}"]
