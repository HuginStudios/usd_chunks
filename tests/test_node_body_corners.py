"""A node's body stays clear of its rounded bottom corners.

The body is a square widget drawn over the node. Reaching the bottom edge, its square
corners covered the rounded outline: a ChunkGroup's grey Description box, and the frame
round a selected chunk.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt import node_theme
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import ChunkNodeItem

from qt_wait import pump_until


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _Scene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
    docker.resize(700, 700)
    docker.show()

    def add_node(key):
        mod = reg.get_chunk_module(key)
        chunk = (mod.create() if mod.loaded else mod.load())[0]
        scene.add_chunk(chunk)

        def node():
            view = docker.tab_widget.get_current_graph_view()
            items = view.scene().items() if view is not None else ()
            return next((i for i in items if isinstance(i, ChunkNodeItem) and i.chunk is chunk), None)

        assert pump_until(qapp, lambda: node() is not None and node()._proxy is not None)
        return node()

    try:
        yield qapp, add_node
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


@pytest.mark.parametrize("key", ["chunkgroup", "camera"])
def test_the_body_ends_above_the_rounded_corners(graph, key):
    qapp, add_node = graph
    node = add_node(key)
    qapp.processEvents()

    body_bottom = node._proxy.geometry().bottom()
    assert node._height - body_bottom >= node_theme.NODE_RADIUS


def test_a_selected_node_body_draws_no_frame_of_its_own(graph):
    qapp, add_node = graph
    body = add_node("camera")._proxy.widget()
    body.set_selected(True)
    qapp.processEvents()
    assert body.property("chunkSelected") is True

    image = QtGui.QImage(body.size(), QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    body.render(image)

    # The left edge, halfway down: the frame's colour there, or the canvas showing through.
    assert image.pixelColor(0, body.height() // 2).alpha() == 0
