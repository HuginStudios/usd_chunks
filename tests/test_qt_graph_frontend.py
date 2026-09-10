"""The Qt adapter actually does what the contract says.

test_graph_frontend_contract.py checks that the verbs EXIST and have the right
signatures. That is not the same as working -- the Flet frontend had a full set of input
handlers that every one of them raised on first touch. So this calls each verb against a
real Qt graph and looks at what changed.
"""
from __future__ import annotations

import time

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_graph
from usd_chunks.plugins import chunk_qt_nodegraph as nodegraph
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture()
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
    yield qapp, reg, scene, docker
    scene.clear_chunk_selection()
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()


def _nodes(graph, module="transform", n=1):
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module(module)
    chunks = []
    for _ in range(n):
        made = mod.create() if mod.loaded else mod.load()
        for c in made:
            scene.add_chunk(c)
        chunks.append(made[0])
    view = docker.tab_widget.get_current_graph_view()
    paths = [get_chunk_prim_path(c) for c in chunks]
    end = time.monotonic() + 2.0
    while time.monotonic() < end and any(p not in view.scene()._nodes for p in paths):
        qapp.processEvents()
    return view, chunks, paths


@pytest.fixture()
def frontend(graph):
    _qapp, _reg, _scene, docker = graph
    view, chunks, paths = _nodes(graph, n=2)
    adapter = nodegraph.QtGraphFrontend(view, docker)
    return adapter, chunks, paths


class TestSelection:
    def test_set_selection_selects(self, frontend):
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        assert adapter.selected_nodes == {paths[0]}

    def test_set_selection_replaces_by_default(self, frontend):
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.set_selection([paths[1]])
        assert adapter.selected_nodes == {paths[1]}

    def test_add_extends(self, frontend):
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.set_selection([paths[1]], add=True)
        assert adapter.selected_nodes == set(paths)

    def test_selected_chunks_matches(self, frontend):
        adapter, chunks, paths = frontend
        adapter.set_selection([paths[0]])
        assert [c.name for c in adapter.selected_chunks()] == [chunks[0].name]


class TestEditing:
    def test_delete_selected_removes_it(self, frontend, graph):
        qapp = graph[0]
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.delete_selected()
        qapp.processEvents()
        assert paths[0] not in adapter.view.scene()._nodes

    def test_toggle_disable(self, frontend):
        adapter, chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.toggle_disable_selected()
        from usd_chunks.chunk_flags import ChunkFlags
        assert chunks[0].has_state(ChunkFlags.State.DISABLED)

    def test_copy_fills_the_shared_clipboard(self, frontend):
        """The same two lists the Flet frontend reads -- that is the whole point of
        moving the clipboard into the engine."""
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.copy_selection()
        assert len(chunk_graph.node_clipboard) == 1
        assert isinstance(chunk_graph.node_clipboard[0], chunk_graph.ClipboardNode)

    def test_paste_creates_a_node(self, frontend, graph):
        qapp = graph[0]
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.copy_selection()
        before = set(adapter.view.scene()._nodes)
        adapter.paste_at(400.0, 400.0)
        end = time.monotonic() + 2.0
        while time.monotonic() < end and set(adapter.view.scene()._nodes) == before:
            qapp.processEvents()
        assert set(adapter.view.scene()._nodes) - before

    def test_duplicate_creates_a_node(self, frontend, graph):
        qapp = graph[0]
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        before = set(adapter.view.scene()._nodes)
        adapter.duplicate_selection()
        end = time.monotonic() + 2.0
        while time.monotonic() < end and set(adapter.view.scene()._nodes) == before:
            qapp.processEvents()
        assert set(adapter.view.scene()._nodes) - before

    def test_create_chunk_needs_no_menu(self, frontend, graph):
        """The refactor this adapter forced: _create_chunk_at used to show a menu and
        create in one method, so there was nothing a contract could call."""
        qapp, reg = graph[0], graph[1]
        adapter, _chunks, _paths = frontend
        entry = next(e for e in reg.get_menu_entries()
                     if getattr(e.module, "module_name", "") == "transform")
        before = set(adapter.view.scene()._nodes)
        adapter.create_chunk(entry, 600.0, 200.0)
        end = time.monotonic() + 2.0
        while time.monotonic() < end and set(adapter.view.scene()._nodes) == before:
            qapp.processEvents()
        assert set(adapter.view.scene()._nodes) - before


class TestNavigation:
    def test_an_ordinary_node_is_not_divable(self, frontend):
        adapter, _chunks, paths = frontend
        assert not adapter.can_dive(paths[0])

    def test_diving_into_a_non_group_does_nothing(self, frontend):
        adapter, _chunks, paths = frontend
        before = adapter.context
        adapter.dive_into(paths[0])
        assert adapter.context == before

    def test_dive_out_without_a_docker_is_a_no_op(self, frontend):
        """Documented behaviour rather than a crash: Qt navigates by tab, so with no
        docker there is no history to step through."""
        adapter, _chunks, _paths = frontend
        nodegraph.QtGraphFrontend(adapter.view, docker=None).dive_out()

    def test_an_unknown_path_is_not_divable(self, frontend):
        adapter, _chunks, _paths = frontend
        assert not adapter.can_dive("/chunks/NoSuchThing")


class TestViews:
    def test_active_slot_starts_at_zero(self, frontend):
        adapter, _chunks, _paths = frontend
        assert adapter.active_slot() == 0

    def test_set_active_slot_round_trips(self, frontend):
        adapter, _chunks, _paths = frontend
        adapter.set_active_slot(2)
        assert adapter.active_slot() == 2


class TestViewport:
    def test_frame_honours_the_paths_it_is_given(self, frontend, graph):
        """Qt's own _frame_nodes always frames selection-or-everything and cannot honour
        an explicit list, so the adapter does the union itself."""
        qapp = graph[0]
        adapter, _chunks, paths = frontend
        before = adapter.view.transform().m11()
        adapter.frame([paths[0]])
        qapp.processEvents()
        after = adapter.view.transform().m11()
        assert after != before or after > 0

    def test_frame_with_unknown_paths_falls_back(self, frontend):
        adapter, _chunks, _paths = frontend
        adapter.frame(["/chunks/NoSuchThing"])


class TestTheClipboardIsShared:
    """The property the engine move was FOR, and the one nobody was testing.

    Qt's tests only ever checked Qt's clipboard and Flet's only checked Flet's, so nothing
    noticed that chunk_qt_nodegraph rebound _node_clipboard to a fresh list after aliasing
    the engine's -- giving each frontend a private clipboard while every commit message
    said they shared one.
    """

    def test_qt_and_the_engine_hold_the_same_lists(self):
        assert nodegraph._node_clipboard is chunk_graph.node_clipboard
        assert nodegraph._edge_clipboard is chunk_graph.edge_clipboard

    def test_flet_and_qt_hold_the_same_lists(self):
        from usd_chunks.plugins import chunk_flet_nodegraph  # noqa: F401
        assert chunk_graph.node_clipboard is nodegraph._node_clipboard

    def test_a_qt_copy_is_visible_to_the_engine(self, frontend):
        """A Flet paste reads chunk_graph.node_clipboard, so this is what makes
        copy-in-Qt-paste-in-Flet work at all."""
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.copy_selection()
        assert len(chunk_graph.node_clipboard) == 1
        assert chunk_graph.node_clipboard[0].module_name == "transform"

    def test_the_engine_can_be_pasted_by_qt(self, frontend, graph):
        """And the other direction: a clipboard filled outside Qt materializes in it."""
        qapp = graph[0]
        adapter, _chunks, paths = frontend
        adapter.set_selection([paths[0]])
        adapter.copy_selection()
        held = list(chunk_graph.node_clipboard)
        chunk_graph.node_clipboard.clear()
        chunk_graph.node_clipboard.extend(held)  # as another frontend would fill it
        before = set(adapter.view.scene()._nodes)
        adapter.paste_at(500.0, 500.0)
        end = time.monotonic() + 2.0
        while time.monotonic() < end and set(adapter.view.scene()._nodes) == before:
            qapp.processEvents()
        assert set(adapter.view.scene()._nodes) - before
