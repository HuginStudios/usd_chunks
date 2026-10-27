"""Dragging a node's right edge changes its width, and the width is saved in USD.

The width goes in UsdUINodeGraphNodeAPI's size, next to the position, so it survives a save
and other frontends see it. Double-clicking the edge goes back to the default width.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from pxr import Gf, Usd, UsdUI

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import _NODE_HEADER_H, _NODE_WIDTH, ChunkNodeItem, _NodeResizeHandle

from qt_wait import pump_until


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _Scene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
    docker.resize(900, 700)
    docker.show()
    mod = reg.get_chunk_module("transform")
    transform = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(transform)
    pump_until(qapp, lambda: any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()))
    view = docker.tab_widget.get_current_graph_view()

    def node():
        return next((i for i in view.scene().items()
                     if isinstance(i, ChunkNodeItem) and i.chunk is transform), None)

    assert pump_until(qapp, lambda: node() is not None and node()._resize_handle is not None
                      and node()._height > _NODE_HEADER_H)
    pump_until(qapp, lambda: False, seconds=0.3)
    view.centerOn(node())
    qapp.processEvents()
    try:
        yield qapp, view, node
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _mouse(view, kind, scene_pos, buttons=QtCore.Qt.MouseButton.LeftButton):
    pos = QtCore.QPointF(view.mapFromScene(scene_pos))
    button = (QtCore.Qt.MouseButton.NoButton if kind == QtCore.QEvent.Type.MouseMove
              else QtCore.Qt.MouseButton.LeftButton)
    event = QtGui.QMouseEvent(kind, pos, QtCore.QPointF(view.viewport().mapToGlobal(pos.toPoint())),
                              button, buttons, QtCore.Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(view.viewport(), event)


def _drag_edge(qapp, view, node, dx):
    start = node.mapToScene(QtCore.QPointF(node._width - 1, node._height - 10))
    _mouse(view, QtCore.QEvent.Type.MouseButtonPress, start)
    _mouse(view, QtCore.QEvent.Type.MouseMove, start + QtCore.QPointF(dx, 0))
    _mouse(view, QtCore.QEvent.Type.MouseButtonRelease, start + QtCore.QPointF(dx, 0),
           QtCore.Qt.MouseButton.NoButton)
    pump_until(qapp, lambda: False, seconds=0.3)


def _saved_size(node):
    attr = UsdUI.NodeGraphNodeAPI(node.prim).GetSizeAttr()
    return attr.Get() if attr and attr.HasAuthoredValue() else None


def test_dragging_the_edge_widens_the_node_and_saves_it(graph) -> None:
    qapp, view, node = graph
    assert node()._width == _NODE_WIDTH
    _drag_edge(qapp, view, node(), 120)

    n = node()
    assert n._width == pytest.approx(_NODE_WIDTH + 120, abs=2)
    assert n._chunk_widget.width() == int(n._width), "the body follows the node's width"
    outputs = [p for p in n._ports if not p.is_input]
    assert outputs and all(p.pos().x() == n._width for p in outputs), "outputs sit on the edge"
    size = _saved_size(n)
    assert size is not None and size[0] == pytest.approx(n._width)


def test_a_size_set_in_usd_is_picked_up(graph) -> None:
    qapp, _, node = graph
    UsdUI.NodeGraphNodeAPI(node().prim).CreateSizeAttr().Set(Gf.Vec2f(400.0, 100.0))
    assert pump_until(qapp, lambda: node()._width == 400.0, seconds=2)


def test_double_clicking_the_edge_resets_the_width(graph) -> None:
    qapp, view, node = graph
    _drag_edge(qapp, view, node(), 120)
    assert _saved_size(node()) is not None

    edge = node().mapToScene(QtCore.QPointF(node()._width - 1, node()._height - 10))
    _mouse(view, QtCore.QEvent.Type.MouseButtonDblClick, edge)
    pump_until(qapp, lambda: False, seconds=0.3)
    assert node()._width == _NODE_WIDTH
    assert _saved_size(node()) is None


def _port(node, suffix: str):
    return next(p for p in node._ports if p.attr_path.endswith(suffix))


def test_a_press_just_off_a_port_starts_a_wire(graph):
    qapp, view, node = graph
    n = node()
    port = _port(n, ".outputs:tx")
    width = n._width
    # Just above or below the port, past its dot, where the edge would take the press:
    # whichever side is further from the next port.
    off = 10 / view.transform().m11()
    start = min((port.scenePos() + QtCore.QPointF(0, dy) for dy in (-off, off)),
                key=lambda pos: -min(QtCore.QLineF(p.scenePos(), pos).length()
                                     for p in n._ports if p is not port))
    assert isinstance(view.scene().itemAt(start, QtGui.QTransform()), _NodeResizeHandle)

    _mouse(view, QtCore.QEvent.Type.MouseButtonPress, start)
    _mouse(view, QtCore.QEvent.Type.MouseMove, start + QtCore.QPointF(40, 0))

    assert view._drag_src_port is port
    assert n._width == width
    view._cancel_wire_drag()


def test_the_edge_away_from_ports_still_resizes(graph):
    qapp, view, node = graph
    n = node()
    width = n._width

    _drag_edge(qapp, view, n, 30)

    assert view._drag_src_port is None
    assert n._width > width


def test_a_wire_dragged_out_of_an_input_leaves_it_to_the_left(graph):
    qapp, view, node = graph
    port = _port(node(), ".inputs:tx")
    start = port.scenePos()

    view._start_wire_drag(port, start)
    view._update_wire_drag(start + QtCore.QPointF(300, 0))
    path = view._drag_edge.path()

    # The curve's first control point: left of the port, as a wire into an input arrives.
    assert path.elementAt(1).x < start.x()
    view._cancel_wire_drag()
