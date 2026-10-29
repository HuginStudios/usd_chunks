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


def test_an_exec_wire_never_outlines_a_node(graph):
    gv, _stage, _chunks, (node_a, node_b) = graph
    pa = str(node_a.prim.GetPath())
    out = node_a.get_port(f"{pa}.outputs:chunk")

    gv._start_wire_drag(out, out.scenePos())
    gv._update_wire_drag(node_b.mapToScene(node_b.boundingRect().center()))

    assert gv._drag_hint_node is None
    gv._cancel_wire_drag()
