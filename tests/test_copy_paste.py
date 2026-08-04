from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins import chunk_qt_nodegraph as nodegraph


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


@pytest.fixture(scope="module")
def graph(qapp):
    """See tests/test_multi_select.py's `graph` fixture docstring for why this is
    module-scoped: chunk_usd.initialize() leaks a global notice listener per call with no
    reset hook, so a handful of test functions each building their own fresh stage was
    observed to destabilize an unrelated, later-running test module."""
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
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    return qapp, reg, scene, docker


@pytest.fixture(autouse=True)
def _clear_selection_and_clipboard(graph):
    _qapp, _reg, scene, _docker = graph
    scene.clear_chunk_selection()
    nodegraph._node_clipboard.clear()
    yield
    scene.clear_chunk_selection()
    nodegraph._node_clipboard.clear()


def _make_nodes(graph, module="transform", n=1):
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module(module)
    chunks = [_mk(mod, scene) for _ in range(n)]
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    nodes = [gv.scene()._nodes[get_chunk_prim_path(c)] for c in chunks]
    return scene, gv, chunks, nodes


def test_copy_paste_creates_independent_duplicate_with_same_param_values(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    tx = a.chunk.get_param("tx")
    tx.value = 5.5

    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()
    assert len(nodegraph._node_clipboard) == 1

    pasted = gv._paste_clipboard_at(QtCore.QPointF(500, 500))
    assert len(pasted) == 1
    new_chunk = pasted[0]

    assert new_chunk is not a.chunk
    assert new_chunk.get_param("tx").value == 5.5

    # Independence: editing the new one must not affect the original.
    new_chunk.get_param("tx").value = 99.0
    assert a.chunk.get_param("tx").value == 5.5


def test_paste_excludes_view_chunks_from_clipboard(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes
    # Simulate a view node the same way test_multi_select.py simulates a locked node --
    # flipping the cached flag directly rather than going through full view-creation
    # machinery, since only the clipboard's own filtering logic is under test here.
    b._is_view = True

    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._copy_selected_to_clipboard()

    assert len(nodegraph._node_clipboard) == 1
    assert nodegraph._node_clipboard[0].module_name == a.chunk.get_config().module


def test_paste_lands_at_the_given_scene_position(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    a.setPos(0, 0)

    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(1234, 5678))
    new_chunk = pasted[0]

    new_node = gv.scene()._nodes[get_chunk_prim_path(new_chunk)]
    assert abs(new_node.pos().x() - 1234) < 1e-6
    assert abs(new_node.pos().y() - 5678) < 1e-6


def test_pasted_selection_becomes_the_new_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes

    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(10, 10))

    assert set(scene.get_selected_chunks()) == set(pasted)


def test_paste_nests_into_the_current_chunkgroup_context(graph) -> None:
    qapp, reg, scene, docker = graph
    group_mod = reg.get_chunk_module("chunkgroup")
    group = _mk(group_mod, scene)
    qapp.processEvents()

    scene2, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()

    graph_scene = gv.scene()
    prev_context = graph_scene._built_context
    graph_scene._built_context = group
    try:
        pasted = gv._paste_clipboard_at(QtCore.QPointF(0, 0))
    finally:
        graph_scene._built_context = prev_context

    assert len(pasted) == 1
    assert pasted[0].get_context() is group


def test_ctrl_v_pastes_and_does_not_also_create_a_view_node(graph, monkeypatch) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])
    gv._copy_selected_to_clipboard()

    view_created = []
    pasted_calls = []
    monkeypatch.setattr(gv, "_create_view_node", lambda: view_created.append(True))
    monkeypatch.setattr(gv, "_paste_at_cursor", lambda: pasted_calls.append(True))

    ctrl_v = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    gv.keyPressEvent(ctrl_v)
    assert pasted_calls == [True]
    assert view_created == []

    plain_v = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.NoModifier
    )
    gv.keyPressEvent(plain_v)
    assert view_created == [True]
    assert pasted_calls == [True]


def test_ctrl_c_copies_current_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph)
    (a,) = nodes
    scene.set_selected_chunks([a.chunk])

    ctrl_c = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    gv.keyPressEvent(ctrl_c)

    assert len(nodegraph._node_clipboard) == 1
    assert nodegraph._node_clipboard[0].module_name == a.chunk.get_config().module
