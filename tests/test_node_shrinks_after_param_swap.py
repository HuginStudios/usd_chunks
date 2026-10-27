"""A node shrinks and re-places its ports when params inside a group go away.

Light rebuilds its attribute params on a type change, inside a group. Going DiskLight ->
RectLight grew the body, and growing resizes the node, which re-placed the ports. Going
back to DiskLight should shrink it, but a body never shrinks on its own and the node was
not told its params changed: it kept the RectLight height, with ports a row or two off.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets
from pxr import Usd

from usd_chunks import chunk_core, chunk_registry, chunk_stage, chunk_widget_api
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import _NODE_HEADER_H, ChunkNodeItem

from qt_wait import pump_until


@pytest.fixture
def light_node(qapp):
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
    mod = reg.get_chunk_module("light")
    light = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(light)
    pump_until(qapp, lambda: any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()))
    view = docker.tab_widget.get_current_graph_view()

    def node():
        return next((i for i in view.scene().items()
                     if isinstance(i, ChunkNodeItem) and i.chunk is light), None)

    assert pump_until(qapp, lambda: node() is not None and node()._proxy is not None
                      and node()._height > 400)
    try:
        yield qapp, light, node
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _ports_off_their_rows(node) -> list[str]:
    body = node._proxy.widget()
    widgets = {w.param: w for w in body.findChildren(QtWidgets.QWidget)
               if isinstance(w, chunk_widget_api.ChunkParamWidgetBase)}
    off = []
    for port in node._typed_ports:
        if isinstance(port.param, chunk_core.ChunkVectorBase):
            continue  # anchored at its group's header, not a row
        w = widgets.get(port.param)
        if w is None or not w.isVisible() or node._nearest_collapsed_group(port.param):
            off.append(f"{port.param.name}: no row")
            continue
        row_y = _NODE_HEADER_H + w.mapTo(body, QtCore.QPoint(0, w.height() // 2)).y()
        if abs(port.pos().y() - row_y) > 2:
            off.append(f"{port.param.name}: port {port.pos().y():.0f}, row {row_y}")
    return off


def test_switching_back_shrinks_the_node_and_moves_its_ports(light_node) -> None:
    qapp, light, node = light_node
    pump_until(qapp, lambda: False, seconds=0.5)
    disk_height = node()._height

    light.get_param("light_type").value = "RectLight"
    assert pump_until(qapp, lambda: node()._height > disk_height)

    light.get_param("light_type").value = "DiskLight"
    assert pump_until(qapp, lambda: node()._height == disk_height, seconds=3), (
        f"node kept its RectLight height: {node()._height} vs {disk_height}")
    pump_until(qapp, lambda: False, seconds=0.3)
    assert _ports_off_their_rows(node()) == []
