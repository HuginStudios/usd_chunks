"""A view chunk living inside a chunkgroup must not crash the CHUNK_LOADED handler.

_reposition_view_chunk assumed a view's context always has a TAB. A view inside a
chunkgroup belongs to that group, which only has a tab of its own while the user is dived
into it -- so get_chunk_context_index returned None and QTabWidget.widget(None) raised
TypeError. That happened from inside the CHUNK_LOADED dispatch, which aborted the rest of
load_chunks' restore pass: every chunk after it never got its DAG dependencies rebuilt or
its execute() called.

Live-reported: reopening a saved scene left an AMD MaterialX Library's inner
Reference/BindMaterial chunks uninitialized -- empty layer, original material still bound
-- until the user dived into the group and back out, which finally cooked them.
"""
from __future__ import annotations

import weakref

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.chunk_stage import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def graph(qapp):
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
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    yield qapp, reg, scene, stage, docker
    docker.close()


def test_view_inside_a_group_does_not_crash_the_loaded_handler(graph) -> None:
    qapp, reg, scene, stage, docker = graph

    def _mk(key):
        mod = reg.get_chunk_module(key)
        cs = mod.create() if mod.loaded else mod.load()
        for c in cs:
            scene.add_chunk(c)
        return cs[0]

    def _wire(dst, src):
        p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
        a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(
            _EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False
        )
        a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])

    grp = _mk("chunkgroup")
    scene._current_context = weakref.ref(grp)
    inner_node = _mk("prim")
    inner_view = _mk("view")
    _wire(inner_view, inner_node)
    scene._current_context = None
    qapp.processEvents()

    # Reproduce the real load-time condition: a view whose context has no tab of its own.
    # In the app a chunkgroup never gets one until the user dives into it -- see
    # create_chunk_widget, which returns early for a group that has a parent context, and
    # which already guards `idx is not None` twice for this same reason. Here the group was
    # created without a parent context so it did get a tab; drop it to match the real state.
    idx = docker.tab_widget.get_chunk_context_index(grp)
    if idx is not None:
        docker.tab_widget.removeTab(idx)
    assert docker.tab_widget.get_chunk_context_index(grp) is None, (
        "test setup must be faithful -- the group must have no tab of its own"
    )

    # This is what CHUNK_LOADED calls for every restored view chunk. It must not raise.
    docker._reposition_view_chunk(inner_view)
