from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker


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


def _node_press(node, local_pos=(10, 10), modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    ev = QtWidgets.QGraphicsSceneMouseEvent(QtCore.QEvent.Type.GraphicsSceneMousePress)
    ev.setPos(QtCore.QPointF(*local_pos))
    ev.setButton(QtCore.Qt.MouseButton.LeftButton)
    ev.setButtons(QtCore.Qt.MouseButton.LeftButton)
    ev.setModifiers(modifiers)
    node.mousePressEvent(ev)


def _node_move(node, local_pos):
    ev = QtWidgets.QGraphicsSceneMouseEvent(QtCore.QEvent.Type.GraphicsSceneMouseMove)
    ev.setPos(QtCore.QPointF(*local_pos))
    ev.setButtons(QtCore.Qt.MouseButton.LeftButton)
    node.mouseMoveEvent(ev)


def _node_release(node, local_pos=(10, 10)):
    ev = QtWidgets.QGraphicsSceneMouseEvent(QtCore.QEvent.Type.GraphicsSceneMouseRelease)
    ev.setPos(QtCore.QPointF(*local_pos))
    ev.setButton(QtCore.Qt.MouseButton.LeftButton)
    node.mouseReleaseEvent(ev)


def _view_mouse(view, etype, view_pos, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    p = QtCore.QPointF(view_pos)
    ev = QtGui.QMouseEvent(
        etype, p, p,
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton, modifiers,
    )
    if etype == QtCore.QEvent.Type.MouseButtonPress:
        view.mousePressEvent(ev)
    elif etype == QtCore.QEvent.Type.MouseMove:
        view.mouseMoveEvent(ev)
    else:
        view.mouseReleaseEvent(ev)


@pytest.fixture()
def graph(qapp):
    """A stage/registry/docker per test.

    This was module-scoped, on the stated grounds that chunk_stage.initialize() leaks a global
    notice listener per call. It does not: calling it 20 times over fresh stages holds
    chunk_notice._global_dispatches at 11 subscribers total, flat from the first call.
    _GlobalDispatch keeps subscribers in a WeakSet and ChunkStageSync is a singleton, so each
    initialize() reassigns its listeners and the previous NoticeConnections disconnect when
    collected.

    The crash in a later module that motivated the shared scope was real, but its cause was a
    deferred callback outliving its object -- QTimer.singleShot with no context object, left
    queued in the shared QApplication and fired inside whatever test next drained events.
    Fixed at the source, so these can go back to per-test isolation.
    """
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
def _clear_selection_between_tests(graph):
    _qapp, _reg, scene, _docker = graph
    scene.clear_chunk_selection()
    yield
    scene.clear_chunk_selection()


def _make_nodes(graph, n=3):
    qapp, reg, scene, docker = graph
    transform_mod = reg.get_chunk_module("transform")
    chunks = [_mk(transform_mod, scene) for _ in range(n)]
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    nodes = [gv.scene()._nodes[get_chunk_prim_path(c)] for c in chunks]
    return scene, gv, chunks, nodes


def test_ctrl_click_toggles_nodes_into_and_out_of_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 2)
    a, b = nodes

    _node_press(a)
    _node_release(a)
    assert set(scene.get_selected_chunks()) == {a.chunk}

    _node_press(b, modifiers=QtCore.Qt.KeyboardModifier.ControlModifier)
    _node_release(b)
    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk}

    _node_press(a, modifiers=QtCore.Qt.KeyboardModifier.ControlModifier)
    _node_release(a)
    assert set(scene.get_selected_chunks()) == {b.chunk}


def test_plain_click_on_multiselected_node_collapses_selection_only_if_no_drag(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 2)
    a, b = nodes

    _node_press(a)
    _node_release(a)
    _node_press(b, modifiers=QtCore.Qt.KeyboardModifier.ControlModifier)
    _node_release(b)
    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk}

    # Plain (no modifier) click+release with no movement on an already-multiselected node
    # collapses the selection to just that node.
    _node_press(a)
    _node_release(a)
    assert set(scene.get_selected_chunks()) == {a.chunk}


def test_drag_on_multiselected_node_moves_the_whole_selection_together(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 2)
    a, b = nodes

    _node_press(a)
    _node_release(a)
    _node_press(b, modifiers=QtCore.Qt.KeyboardModifier.ControlModifier)
    _node_release(b)

    a_before, b_before = a.pos(), b.pos()
    _node_press(a, local_pos=(10, 10))
    _node_move(a, local_pos=(50, 40))
    _node_release(a, local_pos=(50, 40))

    a_delta = a.pos() - a_before
    b_delta = b.pos() - b_before
    assert abs(a_delta.x() - 40) < 1e-6 and abs(a_delta.y() - 30) < 1e-6
    assert abs(b_delta.x() - a_delta.x()) < 1e-6 and abs(b_delta.y() - a_delta.y()) < 1e-6


def test_drag_on_unselected_node_moves_only_itself(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 3)
    a, b, c = nodes

    _node_press(a)
    _node_release(a)
    _node_press(b, modifiers=QtCore.Qt.KeyboardModifier.ControlModifier)
    _node_release(b)
    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk}

    b_before, c_before = b.pos(), c.pos()
    _node_press(c, local_pos=(10, 10))
    _node_move(c, local_pos=(60, 60))
    _node_release(c, local_pos=(60, 60))

    assert c.pos() != c_before
    assert b.pos() == b_before
    # Clicking c (not previously selected) replaces the selection with just c.
    assert set(scene.get_selected_chunks()) == {c.chunk}


def test_locked_companion_is_not_moved_by_multiselect_drag(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 2)
    a, b = nodes
    b._locked = True

    _node_press(a)
    _node_release(a)
    _node_press(b, modifiers=QtCore.Qt.KeyboardModifier.ControlModifier)
    _node_release(b)
    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk}

    b_before = b.pos()
    _node_press(a, local_pos=(10, 10))
    _node_move(a, local_pos=(70, 20))
    _node_release(a, local_pos=(70, 20))

    assert b.pos() == b_before


def _banish_other_nodes(gv, keep) -> None:
    """Move every node except `keep` (from earlier tests sharing this module's stage) far
    out of the way, so a rubber-band box drawn near the origin can't accidentally sweep
    them up too."""
    for node in gv.scene()._nodes.values():
        if node not in keep:
            node.setPos(200000, 200000)


def test_rubber_band_box_select_replaces_selection_with_boxed_nodes(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 3)
    a, b, c = nodes
    _banish_other_nodes(gv, nodes)
    a.setPos(0, 0)
    b.setPos(300, 0)
    c.setPos(900, 0)

    p1 = gv.mapFromScene(QtCore.QPointF(-50, -50))
    p2 = gv.mapFromScene(QtCore.QPointF(350, 60))

    _view_mouse(gv, QtCore.QEvent.Type.MouseButtonPress, p1)
    _view_mouse(gv, QtCore.QEvent.Type.MouseMove, p2)
    _view_mouse(gv, QtCore.QEvent.Type.MouseButtonRelease, p2)

    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk}


def test_rubber_band_with_shift_adds_to_existing_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 3)
    a, b, c = nodes
    _banish_other_nodes(gv, nodes)
    a.setPos(0, 0)
    b.setPos(300, 0)
    c.setPos(900, 0)

    scene.set_selected_chunks([c.chunk])
    assert set(scene.get_selected_chunks()) == {c.chunk}

    p1 = gv.mapFromScene(QtCore.QPointF(-50, -50))
    p2 = gv.mapFromScene(QtCore.QPointF(350, 60))
    _view_mouse(gv, QtCore.QEvent.Type.MouseButtonPress, p1, QtCore.Qt.KeyboardModifier.ShiftModifier)
    _view_mouse(gv, QtCore.QEvent.Type.MouseMove, p2, QtCore.Qt.KeyboardModifier.ShiftModifier)
    _view_mouse(
        gv, QtCore.QEvent.Type.MouseButtonRelease, p2, QtCore.Qt.KeyboardModifier.ShiftModifier
    )

    assert set(scene.get_selected_chunks()) == {a.chunk, b.chunk, c.chunk}


def test_plain_click_on_empty_canvas_clears_selection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 2)
    a, b = nodes
    scene.set_selected_chunks([a.chunk, b.chunk])
    assert len(scene.get_selected_chunks()) == 2

    far_empty = gv.mapFromScene(QtCore.QPointF(-40000, -40000))
    _view_mouse(gv, QtCore.QEvent.Type.MouseButtonPress, far_empty)
    _view_mouse(gv, QtCore.QEvent.Type.MouseButtonRelease, far_empty)

    assert scene.get_selected_chunks() == ()


def test_delete_and_disable_act_on_full_multiselection(graph) -> None:
    scene, gv, chunks, nodes = _make_nodes(graph, 2)
    a, b = nodes
    scene.set_selected_chunks([a.chunk, b.chunk])

    gv._toggle_disable_selected()
    assert a.chunk.has_state(ChunkFlags.State.DISABLED)
    assert b.chunk.has_state(ChunkFlags.State.DISABLED)
