"""Dropping a dragged exec output onto an exec wire joins it in through a Merge.

A -> B with C's exec output dropped on the wire becomes A, C -> Merge -> B, A keeping
slot 0. The joining itself is chunk_graph.merge_into_wire (test_engine_headless); this
covers the view's half: which wire lights under the drag, and that the drop reaches it.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore, QtWidgets

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init, is_merge_prim
from usd_chunks import chunk_naming, chunk_registry
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
    assert slots == [[pa], [pc]]
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


def test_dragging_a_slots_wire_onto_another_slot_swaps_them(wired) -> None:
    gv, stage, (pa, _pb, pc), edge = wired
    merge, node = _joined(gv, stage, pc, edge)
    gv.scene()._build_edges()
    slot0, slot1 = node._exec_in_ports[0], node._exec_in_ports[1]

    gv._start_wire_drag(slot1, slot1.scenePos())
    gv._finish_wire_drag(slot0.scenePos())

    slots = [[str(conn.GetPrimPath()) for conn in attr.GetConnections()]
             for attr in chunk_naming.iter_exec_inputs(merge)]
    assert slots == [[pc], [pa]], "swapped, and neither branch dropped"
