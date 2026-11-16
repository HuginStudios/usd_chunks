"""Selecting a prim in the viewport rings, in the node graph, the chunk that made it and the
chunks that changed it, and names them at the foot of the graph; a chunk's menu selects the
prims it made."""
from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets
from PySide6.QtTest import QTest

from usd_chunks import Scene, chunk_notice
from usd_chunks.chunk_stage import get_chunk_prim_path
from usd_chunks.frontends.qt import widgets
from usd_chunks.frontends.qt.nodegraph import ChunkGraphScene, ChunkGraphView


def _spin(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()


def _mark(graph, chunk):
    mark = getattr(graph._nodes[str(get_chunk_prim_path(chunk))], "_trace_mark", None)
    return (mark._role, mark._label) if mark is not None and mark.isVisible() else None


def test_selecting_a_prim_rings_its_maker_and_changers_and_names_them(qapp) -> None:
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        moved = scene.create.transform(box, primpath_location="/World/box", ty=3.0)
        other = scene.create.cube(prim_path="/World/other", width=1.0)
        stage = scene.get_stage()
        graph = ChunkGraphScene()
        view = ChunkGraphView()
        view.setScene(graph)
        try:
            graph.build(stage, scene, box.get_context())
            selected = [stage.GetPrimAtPath("/World/box")]
            scene.get_selected_prims = lambda: selected
            # The way a host says it: the selection notice, then the debounce.
            scene.primSelectionChangedCallback()
            _spin(200)
            assert _mark(graph, box) == ("made", "made")
            assert _mark(graph, moved) == ("changed", "1")
            assert _mark(graph, other) is None
            line = view._trace_bar.text()
            assert "/World/box" in line and "made by" in line and box.name in line and moved.name in line
            assert not view._trace_bar.isHidden()

            selected.clear()
            graph.trace_selection()
            assert _mark(graph, box) is None and _mark(graph, moved) is None
            assert view._trace_bar.isHidden()
        finally:
            view.deleteLater()
            graph.deleteLater()


def test_a_click_on_a_ringed_node_reaches_its_param_fields(qapp) -> None:
    """The ring sits over the node: a click on a field must still reach the field."""
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        stage = scene.get_stage()
        graph = ChunkGraphScene()
        view = ChunkGraphView()
        view.setScene(graph)
        view.resize(800, 600)
        try:
            graph.build(stage, scene, box.get_context())
            node = graph._nodes[str(get_chunk_prim_path(box))]
            assert node._proxy is not None
            # Off screen the fields never get laid out: give them the height they have on screen.
            node._proxy.resize(node._proxy.size().width(), 200)
            selected = [stage.GetPrimAtPath("/World/box")]
            scene.get_selected_prims = lambda: selected
            graph.trace_selection()
            assert _mark(graph, box) == ("made", "made")
            # A point on the fields and under the ring.
            ring = node._trace_mark.mapRectToScene(node._trace_mark.boundingRect())
            centre = ring.intersected(node._proxy.sceneBoundingRect()).center()
            assert node._proxy.sceneBoundingRect().height() > 20 and node._proxy in graph.items(centre)
            view.centerOn(centre)

            QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=view.mapFromScene(centre))
            # The key router sends typed keys to the clicked node's fields.
            assert view._proxy_key_router._active is node._chunk_widget
        finally:
            view.deleteLater()
            graph.deleteLater()


def test_select_its_prims_selects_what_the_chunk_made(qapp, monkeypatch) -> None:
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        scene.create.cube(prim_path="/World/other", width=1.0)
        asked = []
        monkeypatch.setattr(scene, "select_prims", lambda paths: asked.append([str(p) for p in paths]) or True,
                            raising=False)
        assert widgets.select_chunk_prims([box])
        assert asked == [["/World/box"]]


def test_the_selection_notice_is_one_a_frontend_may_hear() -> None:
    assert chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED in chunk_notice.SCENE_ALIASES
