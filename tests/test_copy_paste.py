from __future__ import annotations

import time

import pytest
from pxr import Sdf, Usd
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
    nodegraph._edge_clipboard.clear()
    yield
    scene.clear_chunk_selection()
    nodegraph._node_clipboard.clear()
    nodegraph._edge_clipboard.clear()


def _make_nodes(graph, module="transform", n=1):
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module(module)
    chunks = [_mk(mod, scene) for _ in range(n)]
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    nodes = [gv.scene()._nodes[get_chunk_prim_path(c)] for c in chunks]
    return scene, gv, chunks, nodes


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _exec_ports(node):
    prim_path = str(node.prim.GetPath())
    return node.get_port(f"{prim_path}.inputs:chunk"), node.get_port(f"{prim_path}.outputs:chunk")


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


def test_copying_a_connected_chain_preserves_internal_wiring_on_paste(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=3)
    a, b, c = nodes

    a_in, a_out = _exec_ports(a)
    b_in, b_out = _exec_ports(b)
    c_in, c_out = _exec_ports(c)
    gv._author_connection(a_out, b_in)
    gv._author_connection(b_out, c_in)
    gv.scene()._build_edges()

    # Copy only A and B -- C (and the real B->C wire) is deliberately left out.
    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._copy_selected_to_clipboard()
    assert len(nodegraph._edge_clipboard) == 1  # only the internal A->B wire

    pasted = gv._paste_clipboard_at(QtCore.QPointF(3000, 3000))
    assert len(pasted) == 2
    _pump(qapp, 0.3)  # let the deferred (80ms) wiring pass run

    a2, b2 = pasted
    a2_path = get_chunk_prim_path(a2)
    b2_path = get_chunk_prim_path(b2)
    stage = docker.tab_widget.get_current_graph_view().scene()._stage

    b2_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:chunk"))
    assert b2_in_attr and b2_in_attr.IsValid()
    conns = [str(p) for p in b2_in_attr.GetConnections()]
    assert conns == [f"{a2_path}.outputs:chunk"]

    # The pasted B' must NOT pick up any connection to the real (uncopied) C.
    b2_out_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.outputs:chunk"))
    if b2_out_attr and b2_out_attr.IsValid():
        assert str(c.prim.GetPath()) not in "".join(str(p) for p in b2_out_attr.GetConnections())


def test_copying_without_the_wires_source_drops_it_on_paste(graph) -> None:
    qapp, reg, scene, docker = graph
    _scene, gv, chunks, nodes = _make_nodes(graph, n=2)
    a, b = nodes

    a_in, a_out = _exec_ports(a)
    b_in, b_out = _exec_ports(b)
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()

    # Copy only B -- A (the real source of B's exec input) is left out.
    scene.set_selected_chunks([b.chunk])
    gv._copy_selected_to_clipboard()
    assert nodegraph._edge_clipboard == []

    pasted = gv._paste_clipboard_at(QtCore.QPointF(3500, 3500))
    assert len(pasted) == 1
    _pump(qapp, 0.3)

    b2_path = get_chunk_prim_path(pasted[0])
    stage = docker.tab_widget.get_current_graph_view().scene()._stage
    b2_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:chunk"))
    assert not b2_in_attr or not b2_in_attr.IsValid() or not b2_in_attr.GetConnections()


def test_typed_param_connection_is_preserved_on_paste(qapp) -> None:
    # Deliberately NOT the shared module-scoped `graph` fixture: a "no active view in this
    # context" recook falls back to re-executing every chunk (see
    # ChunkGraphView._recook_active_view's docstring), and transform.py's own execute()
    # unconditionally Clear()s its chunk's bookkeeping layer on every run -- which, with 9
    # other tests' worth of nodes and timers sharing one stage, was observed to occasionally
    # race and wipe the very connection this test just authored before it could assert on it.
    # An isolated stage sidesteps that cross-test interference for this one assertion.
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

    mod = reg.get_chunk_module("transform")
    a, b = _mk(mod, scene), _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    a = gv.scene()._nodes[get_chunk_prim_path(a)]
    b = gv.scene()._nodes[get_chunk_prim_path(b)]
    _pump(qapp, 0.5)  # let proxy widgets + typed ports build (deferred singleShot chain)

    a_path = str(a.prim.GetPath())
    b_path = str(b.prim.GetPath())
    a_tx_out = a.get_port(f"{a_path}.outputs:tx")
    b_tx_in = b.get_port(f"{b_path}.inputs:tx")
    assert a_tx_out is not None and b_tx_in is not None, "typed ports must exist by now"
    gv._author_connection(a_tx_out, b_tx_in)
    gv.scene()._build_edges()

    scene.set_selected_chunks([a.chunk, b.chunk])
    gv._copy_selected_to_clipboard()
    assert any(e.dst_attr == "inputs:tx" for e in nodegraph._edge_clipboard)

    pasted = gv._paste_clipboard_at(QtCore.QPointF(4000, 4000))
    assert len(pasted) == 2
    _pump(qapp, 0.5)

    a2, b2 = pasted
    b2_path = get_chunk_prim_path(b2)
    stage = docker.tab_widget.get_current_graph_view().scene()._stage
    b2_tx_in_attr = stage.GetAttributeAtPath(Sdf.Path(f"{b2_path}.inputs:tx"))
    assert b2_tx_in_attr and b2_tx_in_attr.IsValid() and b2_tx_in_attr.GetConnections()
