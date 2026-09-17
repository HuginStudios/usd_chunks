"""The graph's keyboard shortcuts must stand down while a popup menu owns the keyboard.

_ProxyKeyRouter is installed on the QApplication (usdview's MainWindow steals native focus
from the graph view, so normal focus-based key handling never works -- see the class
docstring). That means it keeps receiving key events straight through an open QMenu, and
the menu's own filter does not stop it: that one only forwards printable characters to the
search box.

Live-reported as pressing Tab repeatedly stacking up one chunk menu per press.
"""
from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _key(key, text=""):
    return QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, key, QtCore.Qt.KeyboardModifier.NoModifier, text
    )


@pytest.fixture
def graph(qapp):
    """A real graph view with its router installed and the cursor parked over it (every
    shortcut under test is gated on the cursor being inside the view)."""
    from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker

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
    docker.resize(600, 600)
    docker.show()
    qapp.processEvents()

    # A chunk has to exist for the tab (and so the graph view) to be built at all.
    mod = reg.get_chunk_module("prim")
    for c in (mod.create() if mod.loaded else mod.load()):
        scene.add_chunk(c)
    qapp.processEvents()

    gv = docker.tab_widget.get_current_graph_view()
    assert gv is not None
    QtGui.QCursor.setPos(gv.mapToGlobal(QtCore.QPoint(gv.width() // 2, gv.height() // 2)))
    qapp.processEvents()
    yield qapp, gv, gv._proxy_key_router
    docker.close()


def test_tab_opens_the_chunk_menu_when_none_is_open(graph):
    """The companion check: the fix must not simply stop Tab working."""
    qapp, gv, router = graph
    calls = []
    gv._open_tab_menu = lambda: calls.append(1)

    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Tab)) is True
    assert len(calls) == 1


def test_tab_does_not_stack_a_second_chunk_menu_while_one_is_open(graph):
    """Tab pressed again with the menu already up must be left to the menu.

    The chunk menu is shown with a blocking exec_(), so the Tab branch was re-entering
    _open_tab_menu from inside that menu's own nested event loop -- one more menu per
    press, stacked on top of each other.
    """
    qapp, gv, router = graph
    calls = []
    gv._open_tab_menu = lambda: calls.append(1)

    menu = QtWidgets.QMenu()
    menu.addAction("dummy")
    menu.popup(QtGui.QCursor.pos())
    qapp.processEvents()
    assert QtWidgets.QApplication.activePopupWidget() is not None, "setup must be faithful"

    try:
        assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Tab)) is False
        assert calls == [], "a second chunk menu was opened on top of the open one"
    finally:
        menu.close()


@pytest.mark.parametrize(
    "key,text,shortcut",
    [
        (QtCore.Qt.Key.Key_F, "f", "_frame_nodes"),
        (QtCore.Qt.Key.Key_L, "l", "_layout_selected_or_all"),
    ],
)
def test_typing_into_an_open_menu_is_not_hijacked_by_a_graph_shortcut(graph, key, text, shortcut):
    """Letters typed into the chunk menu's search box must reach it.

    "f" and "l" are also the graph's frame-selection and auto-layout shortcuts. Because
    this filter kept running through the open menu, typing either one ran the graph
    shortcut behind the menu AND swallowed the character, so those letters silently never
    made it into the search box.
    """
    qapp, gv, router = graph
    fired = []
    setattr(gv, shortcut, lambda: fired.append(1))

    menu = QtWidgets.QMenu()
    menu.addAction("dummy")
    menu.popup(QtGui.QCursor.pos())
    qapp.processEvents()

    try:
        assert router.eventFilter(gv, _key(key, text)) is False, (
            f"'{text}' was swallowed instead of reaching the menu's search box"
        )
        assert fired == [], f"{shortcut} ran on the graph behind the open menu"
    finally:
        menu.close()
