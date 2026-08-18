"""Delete-selected must fire via the app-level key router, not native view focus alone.

ChunkGraphView.keyPressEvent's own Key_Delete handler only runs when the view holds real
native Qt focus -- unreliable in usdview, whose MainWindow reclaims native focus from any
child widget (see _ProxyKeyRouter's class docstring). Every other single-key graph
shortcut (F, L, Tab, Ctrl+C/V/D/X, 1/2/3) was already hardened with an app-level fallback
in the router for exactly this reason; Delete was the one shortcut left depending on
native focus alone. Live-reported as a just-clicked, clearly-selected node sometimes not
deleting on Delete -- clicking a node lands on its embedded proxy body, which is exactly
when the view does NOT reclaim native focus (see ChunkGraphView.mousePressEvent).
"""
from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui, QtWidgets

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


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
    """A real graph view with its router installed and the cursor parked over it (the
    fallback is gated on the cursor being inside the view, same as its siblings)."""
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


def test_delete_fires_through_the_router_when_a_node_proxy_is_last_active(graph):
    """The exact reported case: a node was selected by clicking its embedded body (making
    it the router's _active proxy), so ChunkGraphView never reclaimed native focus -- Delete
    must still reach _delete_selected via the router's own fallback."""
    qapp, gv, router = graph
    calls = []
    gv._delete_selected = lambda: calls.append(1)

    router.set_active(QtWidgets.QWidget())  # stand-in for a node's embedded proxy body
    router.set_view_active(True)

    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Delete)) is True
    assert calls == [1]


def test_delete_edits_text_instead_of_deleting_the_node_while_typing(graph):
    """Delete pressed while a proxy's own text field has logical focus must edit the
    field, not delete the node underneath it."""
    qapp, gv, router = graph
    calls = []
    gv._delete_selected = lambda: calls.append(1)

    container = QtWidgets.QWidget()
    field = QtWidgets.QLineEdit(container)
    container.show()
    field.setFocus()
    qapp.processEvents()
    assert container.focusWidget() is field, "test setup must be faithful"

    router.set_active(container)
    router.set_view_active(True)

    router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Delete))
    assert calls == [], "Delete deleted the node instead of reaching the focused text field"

    container.close()


def test_bare_d_toggles_disable_through_the_router(graph):
    """Same case as Delete: the node was selected by clicking its embedded body, so the
    view never reclaimed native focus and its own Key_D handler cannot fire."""
    qapp, gv, router = graph
    calls = []
    gv._toggle_disable_selected = lambda: calls.append(1)
    gv._duplicate_selected = lambda: calls.append("dup")

    router.set_active(QtWidgets.QWidget())
    router.set_view_active(True)

    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_D, "d")) is True
    assert calls == [1]


def test_ctrl_d_still_duplicates_rather_than_disabling(graph):
    """Ctrl+D is duplicate, and is claimed by the Ctrl+C/V/D/X block. The bare-D fallback
    must not hijack it."""
    qapp, gv, router = graph
    disabled, duplicated = [], []
    gv._toggle_disable_selected = lambda: disabled.append(1)
    gv._duplicate_selected = lambda: duplicated.append(1)

    router.set_active(QtWidgets.QWidget())
    router.set_view_active(True)

    ev = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress,
        QtCore.Qt.Key.Key_D,
        QtCore.Qt.KeyboardModifier.ControlModifier,
        "d",
    )
    assert router.eventFilter(gv, ev) is True
    assert duplicated == [1] and disabled == []


def test_typing_d_into_a_proxy_field_is_not_hijacked(graph):
    """D is a printable character -- typing it into a node's own text field must reach the
    field, not toggle the node disabled underneath it."""
    qapp, gv, router = graph
    calls = []
    gv._toggle_disable_selected = lambda: calls.append(1)

    container = QtWidgets.QWidget()
    field = QtWidgets.QLineEdit(container)
    container.show()
    field.setFocus()
    qapp.processEvents()
    assert container.focusWidget() is field, "test setup must be faithful"

    router.set_active(container)
    router.set_view_active(True)

    router.eventFilter(gv, _key(QtCore.Qt.Key.Key_D, "d"))
    assert calls == [], "'d' typed into a focused field must not toggle disable"

    container.close()


def test_escape_drops_the_proxy_focus_through_the_router(graph):
    qapp, gv, router = graph
    gv._drag_src_port = None
    router.set_active(QtWidgets.QWidget())
    router.set_view_active(True)

    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Escape)) is True
    assert router._active is None, "Escape must release the active proxy"


def test_escape_cancels_an_in_flight_wire_drag(graph):
    qapp, gv, router = graph
    cancelled = []
    gv._drag_src_port = object()  # stand-in for a live drag
    gv._cancel_wire_drag = lambda: cancelled.append(1)
    router.set_active(QtWidgets.QWidget())
    router.set_view_active(True)

    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Escape)) is True
    assert cancelled == [1], "a wire drag in flight takes priority over defocusing"
    gv._drag_src_port = None


def test_escape_is_left_to_a_field_that_holds_real_native_focus(graph):
    """The router deliberately stands down for a widget with genuine NATIVE focus -- Qt is
    delivering keys correctly there, so there is nothing to route around. Escape's fallback
    targets the other case: the proxy holds only LOGICAL focus because usdview stole the
    native focus, which is what test_escape_drops_the_proxy_focus_through_the_router covers.
    """
    qapp, gv, router = graph
    gv._drag_src_port = None

    container = QtWidgets.QWidget()
    field = QtWidgets.QLineEdit(container)
    container.show()
    field.setFocus()
    qapp.processEvents()
    if QtWidgets.QApplication.focusWidget() is not field:
        pytest.skip("platform did not grant the field real native focus")

    router.set_active(container)
    router.set_view_active(True)

    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_Escape)) is False, (
        "a natively-focused field must receive Escape itself"
    )

    container.close()
