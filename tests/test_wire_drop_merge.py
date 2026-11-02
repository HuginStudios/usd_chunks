"""Dropping a dragged exec output onto an exec wire joins it in through a Merge.

A -> B with C's exec output dropped on the wire becomes C, A -> Merge -> B: C goes in
below A, so it wins over A as a node chained after A would. The joining itself is chunk_graph.merge_into_wire (test_engine_headless); this
covers the view's half: which wire lights under the drag, and that the drop reaches it.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init, is_merge_prim
from usd_chunks import chunk_naming, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import _merge_radius

from qt_wait import pump_until


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def wired(qapp):
    """A -> B wired across the canvas, C below it unwired."""
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

    mod = reg.get_chunk_module("prim")
    chunks = []
    for _ in range(3):
        (chunk,) = mod.create() if mod.loaded else mod.load()
        scene.add_chunk(chunk)
        chunks.append(chunk)
    qapp.processEvents()
    a, b, c = chunks
    gv = docker.tab_widget.get_current_graph_view()
    nodes = gv.scene()._nodes
    pa, pb, pc = (get_chunk_prim_path(x) for x in (a, b, c))
    nodes[pa].setPos(0, 0)
    nodes[pb].setPos(900, 0)
    nodes[pc].setPos(400, 600)
    gv._author_connection(nodes[pa].get_port(f"{pa}.outputs:chunk"), nodes[pb].get_port(f"{pb}.inputs:chunk"))
    gv.scene()._build_edges()
    (edge,) = [e for e in gv.scene()._edges if e.dst_port.attr_path == f"{pb}.inputs:chunk"]
    yield gv, stage, (pa, pb, pc), edge


def test_dragging_an_exec_output_over_a_wire_lights_it(wired) -> None:
    gv, _stage, (_pa, _pb, pc), edge = wired
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")

    gv._start_wire_drag(c_out, c_out.scenePos())
    gv._update_wire_drag(edge.path().pointAtPercent(0.5))
    assert gv._drag_merge_wire is edge
    lit = edge.pen().widthF()

    gv._update_wire_drag(edge.path().pointAtPercent(0.5) + QtCore.QPointF(0, 300))
    assert gv._drag_merge_wire is None
    assert edge.pen().widthF() < lit, "leaving the wire gives it its own pen back"
    gv._cancel_wire_drag()


def test_dropping_an_exec_output_on_a_wire_joins_it_through_a_merge(wired) -> None:
    gv, stage, (pa, pb, pc), edge = wired
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")

    gv._start_wire_drag(c_out, c_out.scenePos())
    drop = edge.path().pointAtPercent(0.5)
    gv._update_wire_drag(drop)
    gv._finish_wire_drag(drop)

    (merge,) = [p for p in stage.Traverse() if is_merge_prim(p)]
    slots = [[str(conn.GetPrimPath()) for conn in attr.GetConnections()]
             for attr in chunk_naming.iter_exec_inputs(merge)]
    assert slots == [[pc], [pa]]
    b_in = stage.GetAttributeAtPath(Sdf.Path(f"{pb}.inputs:chunk"))
    assert [str(conn.GetPrimPath()) for conn in b_in.GetConnections()] == [str(merge.GetPath())]
    assert gv._drag_merge_wire is None


def test_a_picked_up_input_wire_never_lights_a_wire(wired) -> None:
    gv, _stage, (_pa, pb, _pc), edge = wired
    b_in = gv.scene()._nodes[pb].get_port(f"{pb}.inputs:chunk")

    gv._start_wire_drag(b_in, b_in.scenePos())
    gv._update_wire_drag(edge.path().pointAtPercent(0.5))

    assert gv._drag_merge_wire is None
    gv._cancel_wire_drag()


def _joined(gv, stage, pc, edge):
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")
    gv._start_wire_drag(c_out, c_out.scenePos())
    drop = edge.path().pointAtPercent(0.5)
    gv._update_wire_drag(drop)
    gv._finish_wire_drag(drop)
    (merge,) = [p for p in stage.Traverse() if is_merge_prim(p)]
    pump_until(QtWidgets.QApplication.instance(), lambda: str(merge.GetPath()) in gv.scene()._nodes)
    return merge, gv.scene()._nodes[str(merge.GetPath())]


def test_a_merge_is_a_small_circle_centred_on_the_drop(wired) -> None:
    gv, stage, (_pa, _pb, pc), edge = wired
    drop = edge.path().pointAtPercent(0.5)

    _merge, node = _joined(gv, stage, pc, edge)

    assert node._width == node._height == pytest.approx(2 * _merge_radius(3))
    centre = node.mapToScene(QtCore.QPointF(node._width / 2, node._height / 2))
    assert (centre.x(), centre.y()) == pytest.approx((drop.x(), drop.y()), abs=1)
    ins = [p.pos().y() for p in node._exec_in_ports]
    assert len(ins) == 3, "two branches and the empty trailer"
    assert node._exec_out_port.pos().y() == pytest.approx(node._height / 2)


@pytest.fixture
def fanned(wired, qapp):
    """wired, plus D and E below C, and C, D and E selected."""
    gv, stage, (pa, pb, pc), edge = wired
    mod = chunk_registry.ChunkRegistry().get_chunk_module("prim")
    scene = chunk_registry.ChunkRegistry().scene
    extra = []
    for _ in range(2):
        (chunk,) = mod.create()
        scene.add_chunk(chunk)
        extra.append(get_chunk_prim_path(chunk))
    qapp.processEvents()
    nodes = gv.scene()._nodes
    pd, pe = extra
    # E above D: the Merge takes them bottom to top, strongest first, not in creation order.
    nodes[pd].setPos(400, 1000)
    nodes[pe].setPos(400, 800)
    for path in (pc, pd, pe):
        nodes[path].chunk.set_state(ChunkFlags.State.SELECTED)
    gv.scene()._build_edges()
    yield gv, stage, (pa, pb, pc, pd, pe), edge


def _slots_of(merge):
    return [[str(conn.GetPrimPath()) for conn in attr.GetConnections()]
            for attr in chunk_naming.iter_exec_inputs(merge)]


def test_wires_from_every_selected_node_join_a_wire(fanned) -> None:
    gv, stage, (pa, pb, pc, pd, pe), edge = fanned
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")

    gv._start_wire_drag(c_out, c_out.scenePos())
    assert len(gv._drag_fan) == 2
    drop = edge.path().pointAtPercent(0.5)
    gv._update_wire_drag(drop)
    assert all(wire.isVisible() for _port, wire in gv._drag_fan)
    gv._finish_wire_drag(drop)

    (merge,) = [p for p in stage.Traverse() if is_merge_prim(p)]
    assert _slots_of(merge) == [[pd], [pe], [pc], [pa]]
    b_in = stage.GetAttributeAtPath(Sdf.Path(f"{pb}.inputs:chunk"))
    assert [str(conn.GetPrimPath()) for conn in b_in.GetConnections()] == [str(merge.GetPath())]
    assert gv._drag_fan == []


def test_wires_from_every_selected_node_fill_a_merges_slots(fanned) -> None:
    gv, stage, (pa, _pb, pc, pd, pe), edge = fanned
    gv.scene()._nodes[pc].chunk.unset_state(ChunkFlags.State.SELECTED)
    merge, node = _joined(gv, stage, pc, edge)
    gv.scene()._build_edges()
    for path in (pd, pe):  # the new Merge took the selection
        gv.scene()._nodes[path].chunk.set_state(ChunkFlags.State.SELECTED)
    d_out = gv.scene()._nodes[pd].get_port(f"{pd}.outputs:chunk")
    slot = node._exec_in_ports[1]  # A's: the fan goes in there, stronger than A

    gv._start_wire_drag(d_out, d_out.scenePos())
    gv._update_wire_drag(slot.scenePos())
    gv._finish_wire_drag(slot.scenePos())

    assert _slots_of(merge) == [[pc], [pd], [pe], [pa]]


def test_the_empty_slot_is_drawn_at_the_bottom(wired) -> None:
    gv, stage, (_pa, _pb, pc), edge = wired
    _merge, node = _joined(gv, stage, pc, edge)

    *wired_slots, empty = node._exec_in_ports
    assert all(empty.pos().y() > port.pos().y() for port in wired_slots)
    assert wired_slots[0].pos().y() > wired_slots[1].pos().y(), "the strongest just above it"


def _drop_on_empty(gv, stage, src_path, pc, edge):
    """Join C into the wire, then drop src_path's exec output on the Merge's empty slot."""
    merge, _node = _joined(gv, stage, pc, edge)
    gv.scene()._build_edges()
    node = gv.scene()._nodes[str(merge.GetPath())]
    src = gv.scene()._nodes[src_path].get_port(f"{src_path}.outputs:chunk")
    empty = node._exec_in_ports[-1]
    gv._start_wire_drag(src, src.scenePos())
    gv._update_wire_drag(empty.scenePos())
    gv._finish_wire_drag(empty.scenePos())
    return merge


def test_a_wire_dropped_on_the_empty_slot_goes_in_strongest(fanned) -> None:
    gv, stage, (pa, _pb, pc, pd, pe), edge = fanned
    for path in (pc, pe):
        gv.scene()._nodes[path].chunk.unset_state(ChunkFlags.State.SELECTED)

    merge = _drop_on_empty(gv, stage, pd, pc, edge)

    assert _slots_of(merge) == [[pd], [pc], [pa]]


def test_a_fan_dropped_on_the_empty_slot_goes_in_strongest(fanned) -> None:
    gv, stage, (pa, _pb, pc, pd, pe), edge = fanned
    gv.scene()._nodes[pc].chunk.unset_state(ChunkFlags.State.SELECTED)
    merge, _node = _joined(gv, stage, pc, edge)
    gv.scene()._build_edges()
    for path in (pd, pe):  # the new Merge took the selection
        gv.scene()._nodes[path].chunk.set_state(ChunkFlags.State.SELECTED)
    node = gv.scene()._nodes[str(merge.GetPath())]
    d_out, empty = gv.scene()._nodes[pd].get_port(f"{pd}.outputs:chunk"), node._exec_in_ports[-1]

    gv._start_wire_drag(d_out, d_out.scenePos())
    gv._update_wire_drag(empty.scenePos())
    gv._finish_wire_drag(empty.scenePos())

    assert _slots_of(merge) == [[pd], [pe], [pc], [pa]]


def test_a_slots_wire_dropped_on_the_empty_slot_moves_to_slot_0(wired) -> None:
    gv, stage, (pa, _pb, pc), edge = wired
    merge, node = _joined(gv, stage, pc, edge)
    gv.scene()._build_edges()
    node = gv.scene()._nodes[str(merge.GetPath())]
    weakest, empty = node._exec_in_ports[1], node._exec_in_ports[-1]

    gv._start_wire_drag(weakest, weakest.scenePos())
    gv._update_wire_drag(empty.scenePos())
    gv._finish_wire_drag(empty.scenePos())

    assert _slots_of(merge) == [[pa], [pc]]


def test_over_a_plain_input_only_the_grabbed_wire_shows(fanned) -> None:
    gv, _stage, (_pa, pb, pc, _pd, _pe), _edge = fanned
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")
    b_in = gv.scene()._nodes[pb].get_port(f"{pb}.inputs:chunk")

    gv._start_wire_drag(c_out, c_out.scenePos())
    gv._update_wire_drag(b_in.scenePos())

    assert gv._drag_fan and not any(wire.isVisible() for _port, wire in gv._drag_fan)
    gv._cancel_wire_drag()


def test_an_unselected_node_drags_one_wire(wired) -> None:
    gv, _stage, (_pa, _pb, pc), _edge = wired
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")

    gv._start_wire_drag(c_out, c_out.scenePos())

    assert gv._drag_fan == []
    gv._cancel_wire_drag()


def _mouse(gv, kind, button, pos, buttons=None):
    event = QtGui.QMouseEvent(kind, QtCore.QPointF(pos), QtCore.QPointF(gv.viewport().mapToGlobal(pos)),
                              button, buttons if buttons is not None else button,
                              QtCore.Qt.KeyboardModifier.NoModifier)
    {QtCore.QEvent.Type.MouseButtonPress: gv.mousePressEvent,
     QtCore.QEvent.Type.MouseMove: gv.mouseMoveEvent,
     QtCore.QEvent.Type.MouseButtonRelease: gv.mouseReleaseEvent}[kind](event)


@pytest.mark.parametrize("by_click", [False, True])
def test_middle_drag_pans_without_letting_go_of_the_wire(wired, by_click) -> None:
    gv, _stage, (_pa, _pb, pc), _edge = wired
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")
    gv._start_wire_drag(c_out, c_out.scenePos())
    gv._drag_by_click = by_click
    # A "+" click whose release the menu took leaves this set; the middle release is not it.
    gv._swallow_release = by_click
    left =QtCore.Qt.MouseButton.NoButton if by_click else QtCore.Qt.MouseButton.LeftButton
    middle = QtCore.Qt.MouseButton.MiddleButton
    start, end = QtCore.QPoint(200, 200), QtCore.QPoint(150, 120)

    _mouse(gv, QtCore.QEvent.Type.MouseButtonPress, middle, start, middle | left)
    assert gv._drag_src_port is c_out and gv._pan_active
    _mouse(gv, QtCore.QEvent.Type.MouseMove, QtCore.Qt.MouseButton.NoButton, end, middle | left)
    assert gv._drag_edge.path().currentPosition() == gv.mapToScene(end), "the wire follows the cursor"
    _mouse(gv, QtCore.QEvent.Type.MouseButtonRelease, middle, end, left)

    assert gv._drag_src_port is c_out and not gv._pan_active, "still holding the wire"
    gv._cancel_wire_drag()


def test_dragging_a_slots_wire_onto_another_slot_swaps_them(wired) -> None:
    gv, stage, (pa, _pb, pc), edge = wired
    merge, node = _joined(gv, stage, pc, edge)
    gv.scene()._build_edges()
    slot0, slot1 = node._exec_in_ports[0], node._exec_in_ports[1]

    gv._start_wire_drag(slot1, slot1.scenePos())
    gv._finish_wire_drag(slot0.scenePos())

    slots = [[str(conn.GetPrimPath()) for conn in attr.GetConnections()]
             for attr in chunk_naming.iter_exec_inputs(merge)]
    assert slots == [[pa], [pc]], "swapped, and neither branch dropped"


def test_a_wire_dragged_to_the_views_edge_scrolls_the_graph(wired) -> None:
    """To reach a port off screen: near an edge the graph scrolls, the wire's end with it."""
    gv, _stage, (_pa, _pb, pc), _edge = wired
    c_out = gv.scene()._nodes[pc].get_port(f"{pc}.outputs:chunk")
    gv._start_wire_drag(c_out, c_out.scenePos())
    assert gv._edge_pan_timer.isActive()
    rect = gv.viewport().rect()
    hbar, vbar = gv.horizontalScrollBar(), gv.verticalScrollBar()

    assert not gv._edge_pan_step(rect.center()), "nowhere near an edge"
    before = (hbar.value(), vbar.value())
    near_right = QtCore.QPoint(rect.right() - 5, rect.center().y())
    assert gv._edge_pan_step(near_right)
    assert hbar.value() > before[0] and vbar.value() == before[1]
    assert gv._drag_edge.path().currentPosition() == gv.mapToScene(near_right)

    x = hbar.value()
    assert gv._edge_pan_step(QtCore.QPoint(rect.left() + 5, rect.top() + 5))
    assert hbar.value() < x and vbar.value() < before[1], "up and to the left"

    gv._cancel_wire_drag()
    gv._edge_pan_tick()
    assert not gv._edge_pan_timer.isActive(), "stops once the wire is let go"
