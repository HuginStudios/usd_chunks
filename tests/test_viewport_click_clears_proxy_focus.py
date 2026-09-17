"""A click outside the graph view (typically usdview's own 3D viewport) must release the
key router's proxy focus.

Live-reported: add a Script Runner node, start typing in its embedded text field (making
it the router's `_active` proxy), then click in the viewport and press a viewport shortcut
like R (rotate) or F (frame selected) -- the keystroke still landed in the Script Runner's
field instead of reaching the viewport. Nothing ever cleared `_active`/`_view_active` on a
click outside the graph view, so the router's unconditional key-forwarding fallback (see
_ProxyKeyRouter.eventFilter's tail) kept hijacking every later keystroke regardless of
where the cursor now was.
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


def _mouse_press(pos=QtCore.QPointF(0, 0)):
    return QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress, pos, pos, QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier,
    )


@pytest.fixture
def graph(qapp):
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
    yield qapp, gv, gv._proxy_key_router
    docker.close()


def test_click_outside_the_view_clears_active_proxy_and_view_active(graph):
    qapp, gv, router = graph
    QtGui.QCursor.setPos(gv.mapToGlobal(QtCore.QPoint(gv.width() // 2, gv.height() // 2)))
    router.set_active(QtWidgets.QWidget())  # stand-in for a Script Runner's text field
    router.set_view_active(True)

    # Move the cursor well outside the graph view's screen rect -- standing in for a click
    # landing in usdview's own viewport -- then deliver the MouseButtonPress the app-level
    # filter would actually see.
    outside = gv.mapToGlobal(QtCore.QPoint(gv.width() + 500, gv.height() + 500))
    QtGui.QCursor.setPos(outside)
    router.eventFilter(gv, _mouse_press())

    assert router._active is None
    assert router._view_active is False


def test_r_and_f_reach_the_viewport_instead_of_a_stale_active_proxy(graph):
    """The exact reported symptom: R/F pressed over the viewport must not be swallowed by
    a proxy that was merely the last thing clicked before the viewport click."""
    qapp, gv, router = graph
    gv._frame_nodes = lambda: (_ for _ in ()).throw(AssertionError("must not fire off-view"))

    router.set_active(QtWidgets.QWidget())
    router.set_view_active(True)

    outside = gv.mapToGlobal(QtCore.QPoint(gv.width() + 500, gv.height() + 500))
    QtGui.QCursor.setPos(outside)
    router.eventFilter(gv, _mouse_press())

    # Neither key is consumed by the router once the click has moved focus off the graph --
    # letting usdview's own viewport shortcut handling receive them natively.
    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_F)) is False
    assert router.eventFilter(gv, _key(QtCore.Qt.Key.Key_R, "r")) is False
