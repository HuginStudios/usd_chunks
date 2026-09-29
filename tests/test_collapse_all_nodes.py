"""C collapses the graph -- the selection if there is one, everything otherwise.

One direction for the whole set, rather than a per-node toggle: a graph where half the
nodes are already collapsed should end up entirely collapsed, not inverted.

The assertions look at the three chunks these tests create; the graph also holds an
auto-created View node, which collapses along with everything else and is not interesting
here.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from pxr import Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.graph import engine as chunk_graph


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


@pytest.fixture
def graph(qapp):
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
    docker.resize(700, 700)
    docker.show()
    qapp.processEvents()

    mod = reg.get_chunk_module("transform")
    chunks = [_mk(mod, scene) for _ in range(3)]
    qapp.processEvents()
    # The View is created on a timer and changes the selection when it lands. Wait for it,
    # or it can land after a test has selected a node and undo that selection.
    end = time.monotonic() + 10.0
    while time.monotonic() < end and not any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()):
        qapp.processEvents()
    # Creating a chunk selects it, and the graph auto-creates a View node beside them.
    # Start from "nothing selected", which is what the no-selection path means.
    scene.clear_chunk_selection()
    qapp.processEvents()
    view = docker.tab_widget.get_current_graph_view()
    try:
        yield qapp, view, chunks
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _press_c(view, ctrl=False):
    modifier = QtCore.Qt.KeyboardModifier.ControlModifier if ctrl else QtCore.Qt.KeyboardModifier.NoModifier
    view.keyPressEvent(QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_C, modifier
    ))


def _collapsed(chunks):
    return [c.has_state(ChunkFlags.State.COLLAPSED) for c in chunks]


def test_c_collapses_every_node_when_nothing_is_selected(graph) -> None:
    qapp, view, chunks = graph
    assert _collapsed(chunks) == [False, False, False]

    _press_c(view)
    qapp.processEvents()

    assert _collapsed(chunks) == [True, True, True]


def test_pressing_it_again_expands_them(graph) -> None:
    qapp, view, chunks = graph
    _press_c(view)
    qapp.processEvents()

    _press_c(view)
    qapp.processEvents()

    assert _collapsed(chunks) == [False, False, False]


def test_a_half_collapsed_graph_collapses_rather_than_inverting(graph) -> None:
    """The reason this is not a per-node toggle: inverting a mixed graph is never what
    "collapse all" is asked for."""
    qapp, view, chunks = graph
    chunks[1].set_state(ChunkFlags.State.COLLAPSED)
    qapp.processEvents()

    _press_c(view)
    qapp.processEvents()

    assert _collapsed(chunks) == [True, True, True]


def test_a_selection_limits_it_to_those_nodes(graph) -> None:
    qapp, view, chunks = graph
    chunks[0].set_state(ChunkFlags.State.SELECTED)
    qapp.processEvents()

    _press_c(view)
    qapp.processEvents()

    assert _collapsed(chunks) == [True, False, False]


def test_ctrl_c_still_copies(graph) -> None:
    """The bare key had to share with an existing shortcut, so pin that it still copies."""

    qapp, view, chunks = graph
    chunks[0].set_state(ChunkFlags.State.SELECTED)
    qapp.processEvents()

    _press_c(view, ctrl=True)
    qapp.processEvents()

    assert chunk_graph.node_clipboard, "Ctrl+C must still put the selection on the clipboard"
    assert _collapsed(chunks) == [False, False, False], "and must not collapse anything"


def test_the_node_follows_the_state(graph) -> None:
    """The state is the model's; this checks the graph item actually redraws collapsed,
    rather than the flag being set on a node that still shows its whole body."""
    qapp, view, chunks = graph
    node = view.scene()._nodes[get_chunk_prim_path(chunks[0])]
    full_height = node.boundingRect().height()

    _press_c(view)
    qapp.processEvents()

    assert node.boundingRect().height() < full_height


# --- reaching the graph at all -----------------------------------------------------------
#
# The tests above drive ChunkGraphView.keyPressEvent directly, which is a fair test of what
# C *does* and no test at all of whether the key ever gets there. Live-reported: pressing C
# over the graph in usdview collapsed nothing and printed "Camera: Free AutoClip" in the
# viewport -- usdview binds "c" itself, so the view's own handler never ran. Every other
# single-key graph shortcut already has an app-level fallback in _ProxyKeyRouter for the
# related (weaker) reason that usdview's MainWindow reclaims native focus; C was missing it.


def _router_key(view, etype=QtCore.QEvent.Type.KeyPress, ctrl=False):
    modifier = QtCore.Qt.KeyboardModifier.ControlModifier if ctrl else QtCore.Qt.KeyboardModifier.NoModifier
    return QtGui.QKeyEvent(etype, QtCore.Qt.Key.Key_C, modifier, "c")


@pytest.fixture
def over_the_graph(graph):
    """The fallback only claims the key while the cursor is inside the view."""
    qapp, view, chunks = graph
    QtGui.QCursor.setPos(view.mapToGlobal(QtCore.QPoint(view.width() // 2, view.height() // 2)))
    qapp.processEvents()
    yield qapp, view, view._proxy_key_router, chunks


def test_c_collapses_through_the_router_not_just_the_view(over_the_graph) -> None:
    qapp, view, router, chunks = over_the_graph

    assert router.eventFilter(view, _router_key(view)) is True
    qapp.processEvents()

    assert _collapsed(chunks) == [True, True, True]


def test_usdview_never_gets_the_key_to_cycle_its_camera(over_the_graph) -> None:
    """Claiming the ShortcutOverride is what takes "c" back from usdview's own binding --
    without it the camera cycles and the graph does nothing."""
    qapp, view, router, chunks = over_the_graph

    event = _router_key(view, QtCore.QEvent.Type.ShortcutOverride)
    assert router.eventFilter(view, event) is True
    assert event.isAccepted()


def test_the_key_is_left_alone_when_the_cursor_is_elsewhere(graph) -> None:
    """Outside the graph, "c" is usdview's."""
    qapp, view, chunks = graph
    QtGui.QCursor.setPos(QtCore.QPoint(0, 0))
    qapp.processEvents()
    if QtCore.QRect(view.mapToGlobal(QtCore.QPoint(0, 0)), view.size()).contains(QtGui.QCursor.pos()):
        pytest.skip("platform ignored the cursor move")

    assert view._proxy_key_router.eventFilter(view, _router_key(view)) is not True
    assert _collapsed(chunks) == [False, False, False]


def test_ctrl_c_is_not_swallowed_by_the_bare_c_fallback(over_the_graph) -> None:
    qapp, view, router, chunks = over_the_graph
    chunks[0].set_state(ChunkFlags.State.SELECTED)
    qapp.processEvents()

    assert router.eventFilter(view, _router_key(view, ctrl=True)) is True
    qapp.processEvents()

    assert chunk_graph.node_clipboard, "Ctrl+C must still copy"
    assert _collapsed(chunks) == [False, False, False]


def test_typing_c_into_a_node_field_is_not_hijacked(over_the_graph) -> None:
    """C is a printable character: typed into a node's own text field it must reach the
    field, not collapse the graph underneath it."""

    qapp, view, router, chunks = over_the_graph
    container = QtWidgets.QWidget()
    field = QtWidgets.QLineEdit(container)
    container.show()
    field.setFocus()
    qapp.processEvents()
    assert container.focusWidget() is field, "test setup must be faithful"

    router.set_active(container)
    router.set_view_active(True)
    router.eventFilter(view, _router_key(view))
    qapp.processEvents()

    assert _collapsed(chunks) == [False, False, False]
    container.close()
