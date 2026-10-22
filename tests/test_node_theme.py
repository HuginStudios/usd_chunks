"""Which icon and colour a node or group shows, and the header's menu button.

A node's icon comes from the prim's UsdUI icon first, then the chunk's own `icon`, then a
built-in default for its type and its category. Its colour is the prim's UsdUI displayColor
when authored, else the icon's own.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdUI
from PySide6 import QtCore, QtGui

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt import node_theme
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.nodegraph import _PORT_TYPE_COLOR, ChunkGraphView, ChunkNodeItem

from qt_wait import pump_until


class _Chunk:
    """Enough of a chunk for node_icon: an icon and a config with a category."""

    def __init__(self, icon="", category=""):
        self.icon = icon
        self._category = category

    def get_config(self):
        return type("Config", (), {"category": self._category})()


def _prim():
    """A prim, and the stage that owns it: a prim outlives a stage that has gone."""
    stage = Usd.Stage.CreateInMemory()
    return stage, stage.DefinePrim("/n")


def test_a_type_with_a_default_gets_it() -> None:
    icon, color = node_theme.node_icon(_Chunk(), None, "light")
    assert icon == "sun"
    assert color == node_theme.icon_color("sun")


def test_a_type_without_one_falls_back_to_its_category() -> None:
    icon, _ = node_theme.node_icon(_Chunk(category=chunk.StandardCategory.MATERIALS), None,
                                   "some_new_shader")
    assert icon == "sphere"
    icon, _ = node_theme.node_icon(_Chunk(category="Dynamics/Sims"), None, "some_new_sim")
    assert icon == "waves"


def test_the_chunks_own_icon_beats_the_type_default() -> None:
    icon, _ = node_theme.node_icon(_Chunk(icon="zap"), None, "light")
    assert icon == "zap"


def test_an_authored_usdui_icon_and_colour_win() -> None:
    _stage, prim = _prim()
    api = UsdUI.NodeGraphNodeAPI.Apply(prim)
    api.CreateIconAttr().Set(Sdf.AssetPath("camera"))
    api.CreateDisplayColorAttr().Set(Gf.Vec3f(1.0, 0.0, 0.0))
    icon, color = node_theme.node_icon(_Chunk(icon="zap"), prim, "light")
    assert icon == "camera"
    assert color == QtGui.QColor.fromRgbF(1.0, 0.0, 0.0)


def test_groups_pick_an_icon_from_their_name_or_none(qapp) -> None:
    assert node_theme.group_icon(chunk.Group("translate_grp", "Translate")) == "move"
    assert node_theme.group_icon(chunk.Group("vis", "Visibility")) == "eye"
    assert node_theme.group_icon(chunk.Group("zzz", "Something Else")) == ""
    assert node_theme.group_icon(chunk.Group("zzz", "Something", icon="dice")) == "dice"


def test_icon_takes_a_builtin_name_or_an_existing_path(tmp_path) -> None:
    assert chunk.Group("g", "G", icon="sun").icon == "sun"
    image = tmp_path / "mine.png"
    image.write_bytes(b"")
    assert chunk.Group("g", "G", icon=str(image)).icon == str(image)
    with pytest.raises(ValueError):
        chunk.Group("g", "G", icon="not_a_builtin_or_a_file")


def test_every_builtin_icon_draws(qapp) -> None:
    names = {p.stem for p in (node_theme._SVG_DIR).glob("*.svg")}
    assert len(names) > 30
    for name in names:
        pixmap = node_theme.icon_pixmap(name, node_theme.icon_color(name), 24, 2.0)
        assert pixmap is not None and not pixmap.isNull(), name
        assert pixmap.toImage().pixelColor(0, 0).alpha() == 0, f"{name}: background not clear"
    assert node_theme.icon_pixmap("no_such_icon", QtGui.QColor("white"), 24) is None


@pytest.fixture
def graph(qapp, request):
    """A graph holding one chunk: a Light, or the module a test names via indirect."""
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
    docker.resize(900, 700)
    docker.show()
    mod = reg.get_chunk_module(getattr(request, "param", "light"))
    light = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(light)
    pump_until(qapp, lambda: any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()))
    view = docker.tab_widget.get_current_graph_view()

    def node():
        return next((i for i in view.scene().items()
                     if isinstance(i, ChunkNodeItem) and i.chunk is light), None)

    assert pump_until(qapp, lambda: node() is not None)
    try:
        yield qapp, view, node
    finally:
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


def test_a_light_node_shows_the_sun_and_follows_a_usdui_change(graph) -> None:
    qapp, _, node = graph
    assert node()._icon == "sun"
    UsdUI.NodeGraphNodeAPI.Apply(node().prim).CreateIconAttr().Set(Sdf.AssetPath("camera"))
    assert pump_until(qapp, lambda: node()._icon == "camera", seconds=2)


def test_the_header_menu_button_opens_the_node_menu(graph, monkeypatch) -> None:
    qapp, view, node = graph
    opened = []
    monkeypatch.setattr(ChunkGraphView, "_show_chunk_menu",
                        lambda self, n, pos: opened.append(n))
    view.centerOn(node())
    qapp.processEvents()
    point = node().mapToScene(node()._menu_button_rect().center())
    pos = QtCore.QPointF(view.mapFromScene(point))
    for kind in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonRelease):
        buttons = (QtCore.Qt.MouseButton.LeftButton if kind == QtCore.QEvent.Type.MouseButtonPress
                   else QtCore.Qt.MouseButton.NoButton)
        event = QtGui.QMouseEvent(kind, pos, QtCore.QPointF(view.viewport().mapToGlobal(pos.toPoint())),
                                  QtCore.Qt.MouseButton.LeftButton, buttons,
                                  QtCore.Qt.KeyboardModifier.NoModifier)
        QtCore.QCoreApplication.sendEvent(view.viewport(), event)
    assert pump_until(qapp, lambda: bool(opened), seconds=1)
    assert opened == [node()]


@pytest.mark.parametrize("graph", ["transform"], indirect=True)
def test_a_ports_colour_is_always_its_type(graph) -> None:
    """Even on rows labelled X, Y and Z: they are floats, and are coloured as floats."""
    qapp, _, node = graph
    assert pump_until(qapp, lambda: len(node()._typed_ports) >= 6, seconds=3)
    labels = {port.param.get_label() for port in node()._typed_ports if port.param is not None}
    assert {"X", "Y", "Z"} <= labels
    for port in node()._ports:
        assert port.brush().color() == _PORT_TYPE_COLOR[port.port_type], port.attr_path
