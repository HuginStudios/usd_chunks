"""Ctrl+F in the node graph: find a node by name or chunk type, and frame it.

Enter frames the best match and closes; Ctrl+Enter frames the next one each press.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtTest import QTest
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import GraphSearchPopup
from usd_chunks.graph import engine as chunk_graph

from qt_wait import pump_until


# --- ranking, no Qt ----------------------------------------------------------------------


def _node(stage, name, kind, pos):
    prim = stage.DefinePrim(f"/chunks/{name}", "Chunk")
    prim.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token).Set(kind)
    # Stored y-up, as USD keeps it; the graph draws y-down.
    prim.CreateAttribute(chunk_graph.POS_ATTR, Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(pos[0], -pos[1]))
    return prim


@pytest.fixture
def prims():
    stage = Usd.Stage.CreateInMemory()
    nodes = [
        _node(stage, "Trees", "scatter_points", (0, 0)),
        _node(stage, "OakTree", "reference", (300, 0)),
        _node(stage, "Rocks", "scatter_points", (0, 400)),
        _node(stage, "Grass", "scatter_points", (300, 200)),
    ]
    yield {str(p.GetPath()): p for p in nodes}


def test_a_name_match_comes_before_a_type_match(prims) -> None:
    assert chunk_graph.search_nodes(prims, "tree") == ["/chunks/Trees", "/chunks/OakTree"]


def test_a_type_finds_every_node_of_it_top_to_bottom(prims) -> None:
    assert chunk_graph.search_nodes(prims, "scatter") == ["/chunks/Trees", "/chunks/Grass", "/chunks/Rocks"]


def test_every_word_must_match(prims) -> None:
    assert chunk_graph.search_nodes(prims, "rocks scatter") == ["/chunks/Rocks"]
    assert chunk_graph.search_nodes(prims, "rocks reference") == []


def test_nothing_typed_finds_nothing(prims) -> None:
    assert chunk_graph.search_nodes(prims, "  ") == []


def test_names_only_skips_the_type(prims) -> None:
    assert chunk_graph.search_nodes(prims, "tree", types=False) == ["/chunks/Trees", "/chunks/OakTree"]
    assert chunk_graph.search_nodes(prims, "scatter", types=False) == []


def test_types_only_skips_the_name(prims) -> None:
    assert chunk_graph.search_nodes(prims, "scatter", names=False) == ["/chunks/Trees", "/chunks/Grass", "/chunks/Rocks"]
    assert chunk_graph.search_nodes(prims, "tree", names=False) == []


def test_neither_finds_nothing(prims) -> None:
    assert chunk_graph.search_nodes(prims, "tree", names=False, types=False) == []


# --- the popup -----------------------------------------------------------------------------


class _StageScene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _StageScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(700, 700)
    docker.show()
    qapp.processEvents()

    mod = reg.get_chunk_module("transform")
    chunks = {}
    for name in ("Trees", "OakTree", "Rocks"):
        c = (mod.create() if mod.loaded else mod.load())[0]
        c.name = name
        scene.add_chunk(c)
        chunks[name] = c
    qapp.processEvents()
    # The View lands on a timer and changes the selection when it does.
    pump_until(qapp, lambda: any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()))
    scene.clear_chunk_selection()
    qapp.processEvents()
    view = docker.tab_widget.get_current_graph_view()
    pump_until(qapp, lambda: all(get_chunk_prim_path(c) in view.scene()._nodes for c in chunks.values()))
    try:
        yield qapp, view, chunks
    finally:
        if (popup := QtWidgets.QApplication.activePopupWidget()) is not None:
            popup.close()
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _selected(chunks):
    return [name for name, c in chunks.items() if c.has_state(ChunkFlags.State.SELECTED)]


def _in_view(view, chunk_obj) -> bool:
    node = view.scene()._nodes[get_chunk_prim_path(chunk_obj)]
    visible = view.mapToScene(view.viewport().rect()).boundingRect()
    return visible.contains(node.sceneBoundingRect().center())


def test_enter_frames_the_best_match_and_closes(graph) -> None:
    qapp, view, chunks = graph
    view.centerOn(QtCore.QPointF(40000, 40000))  # look away from every node
    assert not _in_view(view, chunks["Rocks"])

    popup = view._open_search()
    popup.field.setText("rocks")
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return)
    qapp.processEvents()

    assert _selected(chunks) == ["Rocks"]
    assert _in_view(view, chunks["Rocks"])
    assert QtWidgets.QApplication.activePopupWidget() is None, "Enter closes the search"


def test_ctrl_enter_steps_through_the_matches_and_stays_open(graph) -> None:
    qapp, view, chunks = graph
    popup = view._open_search()
    popup.field.setText("tree")
    assert [chunk_stage.get_chunk_prim_path(chunks[n]) for n in ("Trees", "OakTree")] == popup.matches

    picked = []
    for _ in range(3):
        QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
        qapp.processEvents()
        picked.append(_selected(chunks))
        assert popup.isVisible(), "Ctrl+Enter keeps the search open"

    assert picked == [["Trees"], ["OakTree"], ["Trees"]], "the first, the second, then round again"
    assert popup.status.text() == "1 of 2: Trees"


def test_enter_after_stepping_keeps_the_match_stepped_to(graph) -> None:
    qapp, view, chunks = graph
    popup = view._open_search()
    popup.field.setText("tree")
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return)
    qapp.processEvents()

    assert _selected(chunks) == ["OakTree"]


def test_typing_again_starts_from_the_best_match(graph) -> None:
    qapp, view, chunks = graph
    popup = view._open_search()
    popup.field.setText("tree")
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
    popup.field.setText("oak")
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()

    assert _selected(chunks) == ["OakTree"]


def test_no_match_says_so_and_leaves_the_selection(graph) -> None:
    qapp, view, chunks = graph
    chunks["Rocks"].set_state(ChunkFlags.State.SELECTED)
    popup = view._open_search()
    popup.field.setText("nothing like this")
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()

    assert popup.status.text() == "No match"
    assert _selected(chunks) == ["Rocks"]


def test_the_boxes_narrow_the_search_and_are_kept_for_the_next_one(graph) -> None:
    qapp, view, chunks = graph
    popup = view._open_search()
    popup.field.setText("transform")
    assert len(popup.matches) == 3, "every node is a Transform"

    popup.by_type.setChecked(False)
    assert popup.matches == []
    assert popup.status.text() == "No match"

    popup.by_name.setChecked(False)
    assert popup.status.text() == "Tick Name or Type to search"
    popup.close()
    qapp.processEvents()

    again = view._open_search()
    assert (again.by_name.isChecked(), again.by_type.isChecked()) == (False, False)


def test_clicking_a_box_leaves_the_typing_in_the_field(graph) -> None:
    qapp, view, chunks = graph
    popup = view._open_search()
    assert popup.by_name.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    assert popup.by_type.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus


def test_escape_closes_it(graph) -> None:
    qapp, view, chunks = graph
    popup = view._open_search()
    QTest.keyClick(popup.field, QtCore.Qt.Key.Key_Escape)
    qapp.processEvents()

    assert QtWidgets.QApplication.activePopupWidget() is None


# --- reaching it -----------------------------------------------------------------------
#
# usdview keeps native focus on its MainWindow, so graph shortcuts arrive through the
# app-level _ProxyKeyRouter, not ChunkGraphView.keyPressEvent.


def _ctrl_f(etype=QtCore.QEvent.Type.KeyPress):
    return QtGui.QKeyEvent(etype, QtCore.Qt.Key.Key_F, QtCore.Qt.KeyboardModifier.ControlModifier, "f")


def test_ctrl_f_over_the_graph_opens_the_search(graph) -> None:
    qapp, view, chunks = graph
    QtGui.QCursor.setPos(view.mapToGlobal(QtCore.QPoint(view.width() // 2, view.height() // 2)))
    qapp.processEvents()
    transform_before = view.transform()

    assert view._proxy_key_router.eventFilter(view, _ctrl_f()) is True
    qapp.processEvents()

    assert isinstance(QtWidgets.QApplication.activePopupWidget(), GraphSearchPopup)
    assert view.transform() == transform_before, "Ctrl+F must not also frame, as bare F does"


def test_ctrl_f_in_the_views_own_key_handler_opens_it_too(graph) -> None:
    qapp, view, chunks = graph
    view.keyPressEvent(_ctrl_f())
    qapp.processEvents()

    assert isinstance(QtWidgets.QApplication.activePopupWidget(), GraphSearchPopup)
