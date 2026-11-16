"""Selecting a prim in the viewport rings, in the node graph, the chunk that made it and the
chunks that changed it, and names them at the foot of the graph; a chunk's menu selects the
prims it made."""
from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from usd_chunks import Scene, chunk_notice
from usd_chunks.chunk_stage import get_chunk_prim_path
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.frontends.qt import widgets
from usd_chunks.frontends.qt.nodegraph import ChunkGraphScene, ChunkGraphView


def _spin(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()


def _mark(graph, chunk):
    node = graph._nodes[str(get_chunk_prim_path(chunk))]
    mark = getattr(node, "_trace_mark", None)
    # Shown on its node; the node itself may be hidden while the graph is off screen.
    return (mark._role, mark._label) if mark is not None and mark.isVisibleTo(node) else None


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


def test_the_ring_follows_the_node_when_it_collapses_expands_or_resizes(qapp) -> None:
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        graph = ChunkGraphScene()
        view = ChunkGraphView()
        view.setScene(graph)
        try:
            graph.build(scene.get_stage(), scene, box.get_context())
            _spin(300)  # the first cook rebuilds the nodes; then the body is built
            node = graph._nodes[str(get_chunk_prim_path(box))]
            # What trace_selection does for a node that made the selected prim.
            node.set_trace("made", "made")

            def ring():
                rect = node._trace_mark._rect
                return rect.width(), rect.height()

            assert ring() == (node._width, node._height)
            open_height = node._height
            box.set_state(ChunkFlags.State.COLLAPSED)
            _spin(300)
            assert node._height < open_height
            assert ring() == (node._width, node._height)
            box.unset_state(ChunkFlags.State.COLLAPSED)
            _spin(300)
            assert node._height == open_height
            assert ring() == (node._width, node._height)
            node.set_width(node._width + 80)
            assert ring() == (node._width, node._height)
        finally:
            view.deleteLater()
            graph.deleteLater()


def test_the_rings_come_back_with_a_selected_prim_the_view_hid(qapp) -> None:
    # A graph an earlier test left for deleteLater still hears every notice: delete it now.
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    with Scene() as scene:
        box = scene.create.cube(prim_path="/World/box", width=2.0)
        moved = scene.create.transform(box, primpath_location="/World/box", ty=3.0)
        scene.set_view(moved)
        stage = scene.get_stage()
        graph = ChunkGraphScene()
        view = ChunkGraphView()
        view.setScene(graph)
        try:
            graph.build(stage, scene, box.get_context())
            _spin(300)
            # A host holds the selection as paths and leaves out the ones with no prim.
            scene.get_selected_prims = lambda: [p for p in [stage.GetPrimAtPath("/World/box")] if p]
            scene.primSelectionChangedCallback()
            _spin(200)
            assert _mark(graph, box) == ("made", "made")

            other = scene.create.cube(prim_path="/World/other", width=1.0)
            scene.set_view(other)  # mutes the box's layers: the prim is gone
            _spin(300)
            assert not stage.GetPrimAtPath("/World/box")
            assert _mark(graph, box) is None and _mark(graph, moved) is None

            scene.set_view(moved)
            _spin(300)
            assert _mark(graph, box) == ("made", "made")
            assert _mark(graph, moved) == ("changed", "1")
        finally:
            view.deleteLater()
            graph.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


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
