"""New nodes start collapsed; open ones float above; a wire dropped on a collapsed node lists
its params (chunk_graph.start_collapsed, ChunkNodeItem._apply_z, ChunkGraphView._pick_port_on).

Branches stacked under each other only stay readable with collapsed nodes, and a collapsed
node shows only its wired ports -- so a new wire has to be able to find the rest.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdUI
from PySide6 import QtCore, QtWidgets

from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.graph import engine as chunk_graph

from qt_wait import pump_until


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def session():
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return stage, scene, reg


def _add(scene, module, collapsed=True):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module)
    created = mod.create() if mod.loaded else mod.load()
    if collapsed:
        chunk_graph.start_collapsed(created)
    for c in created:
        scene.add_chunk(c)
    return created[0]


def test_start_collapsed_collapses_chunks_with_a_body(session):
    stage, scene, _reg = session

    c = _add(scene, "transform")

    assert c.has_state(ChunkFlags.State.COLLAPSED)
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
    attr = UsdUI.NodeGraphNodeAPI(prim).GetExpansionStateAttr()
    assert attr.Get() == UsdUI.Tokens.closed, "saved collapsed, so it reopens collapsed"


@pytest.mark.parametrize("module", ["merge", "output"])
def test_start_collapsed_leaves_bodiless_chunks_open(session, module):
    _stage, scene, _reg = session

    c = _add(scene, module)

    assert not c.has_state(ChunkFlags.State.COLLAPSED)


def test_start_collapsed_can_be_turned_off(session, monkeypatch):
    _stage, scene, _reg = session
    monkeypatch.setenv("USD_CHUNKS_START_COLLAPSED", "0")

    c = _add(scene, "transform")

    assert not c.has_state(ChunkFlags.State.COLLAPSED)


def test_collapsed_ports_shows_only_the_wired_end_and_a_plus_per_side(session):
    _stage, scene, _reg = session
    c = _add(scene, "transform")

    shown = chunk_graph.collapsed_ports(c, fed={"tx"}, feeding={"ty"})

    assert [(p.name, i, o) for p, i, o in shown.rows] == [("tx", True, False), ("ty", False, True)]
    assert shown.plus_in and shown.plus_out


def test_collapsed_ports_offers_no_plus_on_a_bodiless_node(session):
    _stage, scene, _reg = session
    c = _add(scene, "merge")

    shown = chunk_graph.collapsed_ports(c, fed=set(), feeding=set())

    assert not shown.plus_in and not shown.plus_out


def test_pick_candidates_and_output_choices_label_by_group(session):
    _stage, scene, _reg = session
    c = _add(scene, "transform")
    path = get_chunk_prim_path(c)
    src = chunk_graph.PortRef("/other", "/other.outputs:x", chunk_graph.PORT_FLOAT, False)

    onto = chunk_graph.pick_candidates(c, path, src)
    out = chunk_graph.output_choices(c, path)

    assert f"{path}.inputs:tx" in [r.attr_path for r in onto]
    assert all(r.is_input and r.port_type == src.port_type for r in onto)
    assert f"{path}.outputs:tx" in [r.attr_path for r in out]
    assert not any(r.port_type == chunk_graph.PORT_EXEC for r in out)
    tx = next(r for r in out if r.attr_path.endswith(":tx"))
    assert chunk_graph.param_menu_label(tx.param, c) == "Translate › X"


def test_an_open_chunk_writes_no_expansion_state(session):
    stage, scene, _reg = session

    c = _add(scene, "transform", collapsed=False)

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
    assert not UsdUI.NodeGraphNodeAPI(prim).GetExpansionStateAttr().HasAuthoredValue()


@pytest.fixture
def graph(qapp, session):
    stage, scene, _reg = session
    docker = ChunkWidgetDocker()
    docker.resize(700, 600)
    docker.show()
    qapp.processEvents()
    a = _add(scene, "transform", collapsed=False)
    b = _add(scene, "transform")
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    nodes = gv.scene()._nodes
    pa, pb = get_chunk_prim_path(a), get_chunk_prim_path(b)
    nodes[pa].setPos(0, 0)
    nodes[pb].setPos(600, 400)  # clear of the Output the view adds beside them
    pump_until(qapp, lambda: nodes[pa].get_port(f"{pa}.outputs:tx") is not None)
    scene.set_selected_chunks([])  # adding selected the last one
    qapp.processEvents()
    yield gv, stage, (a, b), (nodes[pa], nodes[pb])
    docker.close()  # held until here: the view dies with it


def test_open_nodes_draw_above_collapsed_and_selected_above_both(graph):
    gv, _stage, (a, b), (node_a, node_b) = graph

    assert node_a.zValue() > node_b.zValue(), "open over collapsed"
    b.set_state(ChunkFlags.State.SELECTED)
    QtWidgets.QApplication.processEvents()
    assert node_b.zValue() > node_a.zValue(), "selected over open"


def test_a_typed_wire_over_a_collapsed_node_outlines_it(graph):
    gv, _stage, _chunks, (node_a, node_b) = graph
    pa = str(node_a.prim.GetPath())
    out = node_a.get_port(f"{pa}.outputs:tx")
    centre = node_b.mapToScene(node_b.boundingRect().center())

    gv._start_wire_drag(out, out.scenePos())
    gv._update_wire_drag(centre)
    assert gv._drag_hint_node is node_b and node_b._drop_hint

    gv._update_wire_drag(centre + QtCore.QPointF(0, 500))
    assert gv._drag_hint_node is None and not node_b._drop_hint
    gv._cancel_wire_drag()


def test_dropping_on_a_collapsed_node_wires_the_picked_param(graph, monkeypatch):
    gv, stage, (_a, b), (node_a, node_b) = graph
    pa, pb = str(node_a.prim.GetPath()), str(node_b.prim.GetPath())
    out = node_a.get_port(f"{pa}.outputs:tx")
    offered = []

    def pick(menu, _pos):
        offered.extend(action.data() for action in menu.actions())
        return next(action for action in menu.actions() if action.data() == f"{pb}.inputs:tx")

    monkeypatch.setattr(gv, "_exec_menu", pick)  # QMenu.exec itself can't be patched
    centre = node_b.mapToScene(node_b.boundingRect().center())
    gv._start_wire_drag(out, out.scenePos())
    gv._update_wire_drag(centre)
    gv._finish_wire_drag(centre)
    pump_until(QtWidgets.QApplication.instance(), lambda: offered)

    assert f"{pb}.inputs:rx" in offered, "every float param is offered"
    attr = stage.GetAttributeAtPath(Sdf.Path(f"{pb}.inputs:tx"))
    assert [str(c) for c in attr.GetConnections()] == [f"{pa}.inputs:tx"]
    pump_until(QtWidgets.QApplication.instance(), lambda: node_b.get_port(f"{pb}.inputs:tx") is not None)
    assert b.has_state(ChunkFlags.State.COLLAPSED), "wiring it does not open it"
    assert node_b.get_port(f"{pb}.outputs:tx") is None, "only the wired end shows"


def test_a_collapsed_node_shows_a_plus_the_wire_snaps_to(graph):
    gv, _stage, _chunks, (node_a, node_b) = graph
    pa = str(node_a.prim.GetPath())
    out = node_a.get_port(f"{pa}.outputs:tx")

    assert node_a._plus_y is None, "an open node has a port per param"
    assert node_b._plus_y is not None and node_b._plus_y < node_b._height
    gv._start_wire_drag(out, out.scenePos())
    gv._update_wire_drag(node_b.mapToScene(node_b.boundingRect().center()))

    plus = node_b.mapToScene(QtCore.QPointF(0, node_b._plus_y))
    assert gv._drag_edge.path().currentPosition() == plus
    gv._cancel_wire_drag()


def test_hovering_near_the_plus_outside_the_node_takes_the_wire(graph):
    gv, _stage, _chunks, (node_a, node_b) = graph
    pa = str(node_a.prim.GetPath())
    out = node_a.get_port(f"{pa}.outputs:tx")
    plus = node_b.mapToScene(QtCore.QPointF(0, node_b._plus_y))
    beside = plus - QtCore.QPointF(10, 0)  # left of the node's edge, off its body

    assert not node_b.sceneBoundingRect().adjusted(5, 0, 0, 0).contains(beside)
    gv._start_wire_drag(out, out.scenePos())
    gv._update_wire_drag(beside)

    assert gv._drag_hint_node is node_b
    assert gv._drag_edge.path().currentPosition() == plus
    gv._cancel_wire_drag()


def test_the_output_plus_picks_a_param_and_drags_a_wire_from_it(graph, monkeypatch):
    gv, stage, (_a, b), (node_a, node_b) = graph
    pa, pb = str(node_a.prim.GetPath()), str(node_b.prim.GetPath())
    out_plus = node_b.mapToScene(QtCore.QPointF(node_b._width, node_b._plus_y))
    assert node_b._plus_out and gv._out_plus_at(out_plus) is node_b

    def pick(menu, _pos):
        return next(action for action in menu.actions() if action.data() == f"{pb}.outputs:tx")

    monkeypatch.setattr(gv, "_exec_menu", pick)
    gv._pick_output_and_drag(node_b, QtCore.QPoint())
    assert gv._drag_by_click and gv._drag_src_port.attr_path == f"{pb}.outputs:tx"
    temp = gv._drag_temp_port

    target = node_a.get_port(f"{pa}.inputs:tx").scenePos()
    gv._update_wire_drag(target)
    # Whichever of A's float inputs the drop snaps to: offscreen, A's body is never laid
    # out, so its X, Y and Z ports sit within a couple of pixels of each other.
    landed, _hint = gv._drop_choice(target)
    assert landed is not None and landed.attr_path.startswith(f"{pa}.inputs:")
    gv._finish_wire_drag(target)  # what the next click does

    attr = stage.GetAttributeAtPath(Sdf.Path(landed.attr_path))
    assert [str(c) for c in attr.GetConnections()] == [f"{pb}.inputs:tx"]
    assert gv._drag_temp_port is None and temp.scene() is None, "the drag's own port goes with it"
    assert not gv._drag_by_click
    pump_until(QtWidgets.QApplication.instance(), lambda: node_b.get_port(f"{pb}.outputs:tx") is not None)
    assert b.has_state(ChunkFlags.State.COLLAPSED)


def test_plus_to_plus_draws_the_wire_and_cutting_it_drops_both_ports(graph, monkeypatch):
    gv, stage, (a, _b), (node_a, node_b) = graph
    app = QtWidgets.QApplication.instance()
    a.set_state(ChunkFlags.State.COLLAPSED)
    pump_until(app, lambda: node_a._plus_in)
    pa, pb = str(node_a.prim.GetPath()), str(node_b.prim.GetPath())
    picks = iter([f"{pb}.outputs:tx", f"{pa}.inputs:tx"])

    def pick(menu, _pos):
        want = next(picks)
        return next(action for action in menu.actions() if action.data() == want)

    monkeypatch.setattr(gv, "_exec_menu", pick)
    gv._pick_output_and_drag(node_b, QtCore.QPoint())
    in_plus = node_a.mapToScene(QtCore.QPointF(0, node_a._plus_y))
    gv._update_wire_drag(in_plus)
    assert gv._drag_hint_node is node_a
    gv._finish_wire_drag(in_plus)  # the click; A's own menu is deferred past it
    pump_until(app, lambda: node_a.get_port(f"{pa}.inputs:tx") is not None)

    attr = stage.GetAttributeAtPath(Sdf.Path(f"{pa}.inputs:tx"))
    assert [str(c) for c in attr.GetConnections()] == [f"{pb}.inputs:tx"]
    src = node_b.get_port(f"{pb}.outputs:tx")
    assert src is not None, "the node the wire left from shows its end too"
    assert any(e.src_port is src for e in gv.scene()._edges), "and the wire is drawn"

    gv._disconnect_port(node_a.get_port(f"{pa}.inputs:tx"), attr, gv.scene())
    pump_until(app, lambda: node_b.get_port(f"{pb}.outputs:tx") is None)
    assert node_a.get_port(f"{pa}.inputs:tx") is None


def test_an_exec_wire_never_outlines_a_node(graph):
    gv, _stage, _chunks, (node_a, node_b) = graph
    pa = str(node_a.prim.GetPath())
    out = node_a.get_port(f"{pa}.outputs:chunk")

    gv._start_wire_drag(out, out.scenePos())
    gv._update_wire_drag(node_b.mapToScene(node_b.boundingRect().center()))

    assert gv._drag_hint_node is None
    gv._cancel_wire_drag()
