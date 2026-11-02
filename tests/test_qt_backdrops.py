"""Backdrops and sticky notes on a real Qt node graph."""
from __future__ import annotations

import time

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore, QtGui, QtTest

from usd_chunks import chunk_registry, chunk_undo
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import export_chunks, get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.backdrop_item import BackdropTextPopup
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.graph import backdrops
from usd_chunks.graph import engine as chunk_graph


class _StageScene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()
    scene = _StageScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.resize(900, 700)
    docker.show()
    qapp.processEvents()
    yield qapp, reg, scene, docker, stage
    scene.clear_chunk_selection()
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()


def _settle(qapp, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _nodes(graph, n):
    qapp, reg, scene, docker, _stage = graph
    mod = reg.get_chunk_module("transform")
    chunks = []
    for _ in range(n):
        made = mod.create() if mod.loaded else mod.load()
        scene.add_chunk(made[0])
        chunks.append(made[0])
    view = docker.tab_widget.get_current_graph_view()
    paths = [get_chunk_prim_path(c) for c in chunks]
    end = time.monotonic() + 10.0
    while time.monotonic() < end and any(p not in view.scene()._nodes for p in paths):
        qapp.processEvents()
    graph_scene = view.scene()
    # Collapsed and spread out, so every node has a known, small rect.
    for i, path in enumerate(paths):
        node = graph_scene._nodes[path]
        node.chunk.set_state(ChunkFlags.State.COLLAPSED)
        node.setPos(100 + 400 * i, 100)
        node._write_pos_to_usd()
    _settle(qapp)
    return view, chunks, paths


def _press_drag(view, scene_from, scene_to):
    """A real left-button drag on the viewport, from one scene point to another.

    The moves are sent by hand: QTest.mouseMove carries no button state.
    """
    viewport = view.viewport()
    start = view.mapFromScene(scene_from)
    end = view.mapFromScene(scene_to)
    QtTest.QTest.mousePress(viewport, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start)
    for step in range(1, 6):
        point = QtCore.QPointF(start + (end - start) * step / 5)
        move = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove, point, viewport.mapToGlobal(point),
            QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        QtCore.QCoreApplication.sendEvent(viewport, move)
    QtTest.QTest.mouseRelease(viewport, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, end)


def _backdrop_around(graph, paths):
    """Select `paths` and press B, the way a user does."""
    qapp, _reg, scene, _docker, _stage = graph
    view = _docker.tab_widget.get_current_graph_view()
    scene.set_selected_chunks([view.scene()._nodes[p].chunk for p in paths])
    item = view._create_backdrop()
    _settle(qapp)
    return view, item


def test_b_frames_the_selected_nodes(graph):
    view, _chunks, paths = _nodes(graph, 3)
    view, item = _backdrop_around(graph, paths[:2])
    graph_scene = view.scene()
    assert item is not None and item.prim.GetTypeName() == "Backdrop"
    inside = {str(n.prim.GetPath()) for n in graph_scene.nodes_in_backdrop(item)}
    assert inside == set(paths[:2])
    # Selecting the backdrop let go of the nodes, so Delete and L act on it alone.
    assert item.selected
    assert not any(graph_scene._nodes[p].chunk.has_state(ChunkFlags.State.SELECTED) for p in paths)
    # It lives under the graph's root, next to the chunks.
    assert item.prim.GetParent().GetPath() == graph_scene._chunks_root_path


def test_dragging_the_title_bar_moves_the_nodes_inside(graph):
    qapp = graph[0]
    view, _chunks, paths = _nodes(graph, 3)
    view, item = _backdrop_around(graph, paths[:2])
    graph_scene = view.scene()
    view.resetTransform()
    x, y, w, _h = item.rect()
    view.centerOn(x + w / 2, y)
    _settle(qapp)
    before = {p: graph_scene._nodes[p].pos() for p in paths}
    grab = QtCore.QPointF(x + 40, y + backdrops.HEADER_HEIGHT / 2)
    _press_drag(view, grab, grab + QtCore.QPointF(60, 30))
    _settle(qapp)
    for path in paths[:2]:
        moved = graph_scene._nodes[path].pos() - before[path]
        assert (round(moved.x()), round(moved.y())) == (60, 30)
        saved = graph_scene._nodes[path].prim.GetAttribute("ui:nodegraph:node:pos").Get()
        assert (round(saved[0]), round(-saved[1])) == (round(graph_scene._nodes[path].pos().x()),
                                                       round(graph_scene._nodes[path].pos().y()))
    assert graph_scene._nodes[paths[2]].pos() == before[paths[2]]
    assert backdrops.read(item.prim).rect[:2] == pytest.approx((x + 60, y + 30))


def test_a_click_in_the_middle_is_the_canvas(graph):
    """The middle of a backdrop is left to the canvas: a press there box-selects."""
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    x, y, w, h = item.rect()
    middle = QtCore.QPointF(x + w / 2, y + h - 10)
    assert view.scene().itemAt(middle, view.transform()) is not item
    header = QtCore.QPointF(x + 20, y + 10)
    assert view.scene().itemAt(header, view.transform()) is item


def test_delete_removes_the_backdrop_and_keeps_its_nodes(graph):
    qapp, _reg, _scene, _docker, stage = graph
    view, _chunks, paths = _nodes(graph, 2)
    view, item = _backdrop_around(graph, paths)
    path = item.prim.GetPath()
    view._delete_selected()
    _settle(qapp)
    assert not stage.GetPrimAtPath(path)
    assert str(path) not in view.scene()._backdrops
    assert all(p in view.scene()._nodes for p in paths)



def test_n_makes_a_note_and_its_text_is_kept(graph):
    qapp = graph[0]
    view, _chunks, _paths = _nodes(graph, 1)
    item = view._create_backdrop(note=True)
    _settle(qapp)
    assert item.is_note
    popups = [w for w in view.findChildren(BackdropTextPopup) if w.isVisible()]
    assert len(popups) == 1
    popup = popups[0]
    popup.field.setPlainText("Builds the floor")
    QtTest.QTest.keyClick(popup.field, QtCore.Qt.Key_Return, QtCore.Qt.ControlModifier)
    _settle(qapp)
    assert backdrops.read(item.prim).text == "Builds the floor"


def test_esc_throws_the_edit_away(graph):
    qapp = graph[0]
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    backdrops.set_text(item.prim, "Kept")
    popup = view.scene().edit_backdrop_text(item)
    popup.field.setText("Thrown away")
    QtTest.QTest.keyClick(popup.field, QtCore.Qt.Key_Escape)
    _settle(qapp)
    assert backdrops.read(item.prim).text == "Kept"


def test_an_edit_on_the_stage_shows_on_the_canvas(graph):
    qapp, _reg, _scene, _docker, stage = graph
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    backdrops.set_color(item.prim, (1.0, 0.0, 0.0))
    backdrops.set_rect(item.prim, (-500, -400, 250, 150))
    _settle(qapp)
    assert item.info.color == pytest.approx((1.0, 0.0, 0.0))
    assert item.rect() == pytest.approx((-500, -400, 250, 150))
    # And one made or removed by a script comes and goes.
    root = view.scene()._chunks_root_path
    made = backdrops.create(stage, root, (0, 600, 200, 100), note=True, text="from a script")
    _settle(qapp)
    assert str(made.GetPath()) in view.scene()._backdrops
    backdrops.delete(made)
    _settle(qapp)
    assert str(made.GetPath()) not in view.scene()._backdrops


def test_saving_keeps_backdrops_and_notes(graph, tmp_path):
    qapp, _reg, scene, _docker, _stage = graph
    view, _chunks, paths = _nodes(graph, 2)
    view, item = _backdrop_around(graph, paths)
    backdrops.set_text(item.prim, "Floor")
    note = view.scene().create_backdrop((0, 500, 200, 100), note=True)
    backdrops.set_text(note.prim, "Why")
    # And one straight under /chunks, outside any chunkgroup: not part of a chunk's prim,
    # so it is not carried along by one.
    backdrops.create(_stage, "/chunks", (0, 900, 200, 100), text="Loose")
    path = tmp_path / "saved.usda"
    export_chunks(scene, path)
    saved = Usd.Stage.Open(str(path))
    found = {str(p.GetPath()): backdrops.read(p) for p in saved.Traverse() if backdrops.is_backdrop(p)}
    assert {info.text for info in found.values()} == {"Floor", "Why", "Loose"}
    assert sum(info.note for info in found.values()) == 1


def test_undo_puts_a_moved_backdrop_back(graph):
    qapp, _reg, _scene, _docker, stage = graph
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    history = chunk_undo.History(stage)
    history.listen()
    try:
        before_key = history.key()
        start = item.rect()
        backdrops.set_rect(item.prim, (start[0] + 300, start[1], start[2], start[3]))
        backdrops.set_text(item.prim, "Moved")
        assert history.current != 0
        assert history.move_to(before_key)
        _settle(qapp)
        assert backdrops.read(stage.GetPrimAtPath(item.prim.GetPath())).rect == pytest.approx(start)
        assert item.rect() == pytest.approx(start)
    finally:
        history.close()


def test_selecting_nodes_from_elsewhere_lets_go_of_the_backdrop(graph):
    """Seen in usdview: B left the new backdrop selected, nodes were then picked outside
    the graph, and Delete took the backdrop along with them."""
    qapp, _reg, scene, _docker, stage = graph
    view, _chunks, paths = _nodes(graph, 2)
    view, item = _backdrop_around(graph, paths[:1])
    assert item.selected
    scene.set_selected_chunks([view.scene()._nodes[paths[1]].chunk])
    _settle(qapp)
    assert not item.selected
    view._delete_selected()
    _settle(qapp)
    assert stage.GetPrimAtPath(item.prim.GetPath())


def test_the_whole_resize_corner_can_be_grabbed(graph):
    """The grip overlaps the frame, and an odd-even fill made that overlap a hole: a press
    right on the drawn grip lines missed and started a box-select."""
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    _x, _y, w, h = item.rect()
    for dx, dy in ((3, 3), (10, 3), (3, 10), (10, 10)):
        assert item.shape().contains(QtCore.QPointF(w - dx, h - dy)), (dx, dy)


def test_resizing_from_the_corner(graph):
    qapp = graph[0]
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    view.resetTransform()
    x, y, w, h = item.rect()
    view.centerOn(x + w, y + h)
    _settle(qapp)
    corner = QtCore.QPointF(x + w - 3, y + h - 3)
    _press_drag(view, corner, corner + QtCore.QPointF(80, 40))
    _settle(qapp)
    assert backdrops.read(item.prim).rect == pytest.approx((x, y, w + 80, h + 40), abs=1)




def test_selected_backdrops_drag_together(graph):
    qapp = graph[0]
    view, _chunks, paths = _nodes(graph, 2)
    graph_scene = view.scene()
    view, first = _backdrop_around(graph, paths[:1])
    view, second = _backdrop_around(graph, paths[1:])
    graph_scene.select_backdrop(first, toggle=True)
    assert first.selected and second.selected
    before = {p: graph_scene._nodes[p].pos() for p in paths}
    second_before = second.rect()
    view.resetTransform()
    x, y, w, _h = first.rect()
    view.centerOn(x + w / 2, y)
    _settle(qapp)
    grab = QtCore.QPointF(x + 30, y + backdrops.HEADER_HEIGHT / 2)
    _press_drag(view, grab, grab + QtCore.QPointF(0, 90))
    _settle(qapp)
    for path in paths:
        assert round((graph_scene._nodes[path].pos() - before[path]).y()) == 90, path
    assert backdrops.read(second.prim).rect[1] == pytest.approx(second_before[1] + 90, abs=1)


def test_a_rebuild_mid_drag_does_not_split_the_backdrop_from_its_nodes(graph):
    qapp = graph[0]
    view, _chunks, paths = _nodes(graph, 1)
    graph_scene = view.scene()
    view, item = _backdrop_around(graph, paths)
    node = graph_scene._nodes[paths[0]]
    view.resetTransform()
    x, y, w, _h = item.rect()
    view.centerOn(x + w / 2, y)
    _settle(qapp)
    viewport = view.viewport()
    start = view.mapFromScene(QtCore.QPointF(x + 30, y + 10))
    QtTest.QTest.mousePress(viewport, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start)
    for step in (1, 2):
        point = QtCore.QPointF(start + QtCore.QPoint(0, 40 * step))
        QtCore.QCoreApplication.sendEvent(viewport, QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove, point, viewport.mapToGlobal(point),
            QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
        graph_scene._rebuild_nodes()  # a resync from a cook landing mid-drag
    QtTest.QTest.mouseRelease(viewport, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start + QtCore.QPoint(0, 80))
    _settle(qapp)
    box = backdrops.read(item.prim).rect
    assert box[1] == pytest.approx(y + 80, abs=1)
    assert backdrops.node_inside(box, graph_scene._node_header_rect(node))


def test_the_editor_opens_inside_the_view(graph):
    """A wide backdrop scrolled half out of sight has its corner off every screen; the
    editor still opens where it can be seen."""
    qapp = graph[0]
    view, _chunks, paths = _nodes(graph, 1)
    view, item = _backdrop_around(graph, paths)
    x, y, w, h = item.rect()
    backdrops.set_rect(item.prim, (x - 20000, y, w + 20000, h))
    _settle(qapp)
    view.resetTransform()
    view.centerOn(x + w - 50, y + 20)
    _settle(qapp)
    popup = view.scene().edit_backdrop_text(item)
    _settle(qapp)
    viewport = view.viewport()
    area = QtCore.QRect(viewport.mapToGlobal(QtCore.QPoint(0, 0)), viewport.size())
    assert area.contains(popup.geometry().topLeft()), (popup.geometry(), area)
    popup.close()


def test_edits_never_go_to_a_chunk_layer(graph):
    """A chunk's own layer is cleared on its next cook: an edit there would vanish."""
    qapp, _reg, _scene, _docker, stage = graph
    view, chunks, paths = _nodes(graph, 1)
    layer = chunk_registry.ChunkLayerManager().get_layer(chunks[0])
    assert chunk_registry.is_chunk_internal_layer(layer)
    view, item = _backdrop_around(graph, paths)
    path = item.prim.GetPath()
    with Usd.EditContext(stage, layer):
        stage.OverridePrim(path).CreateAttribute("ui:description", Sdf.ValueTypeNames.String).Set("in a chunk layer")
    backdrops.set_text(stage.GetPrimAtPath(path), "kept")
    assert layer.GetAttributeAtPath(path.AppendProperty("ui:description")).default == "in a chunk layer"
    owned = [lyr for lyr in stage.GetLayerStack(includeSessionLayers=True)
             if lyr.GetAttributeAtPath(path.AppendProperty("ui:description")) is not None
             and lyr.GetAttributeAtPath(path.AppendProperty("ui:description")).default == "kept"]
    assert owned and not chunk_registry.is_chunk_internal_layer(owned[0])
