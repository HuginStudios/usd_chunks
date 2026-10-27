"""Which icon and colour a node or group shows, and the header's menu button.

A node's icon comes from the prim's UsdUI icon first, then the chunk's own `icon`, then the
`icon` in its config(). Nothing else picks one: a chunk that sets no icon shows none, and a
group shows only the icon it sets. Its colour is the prim's UsdUI displayColor when
authored, else the icon's own.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdUI
from PySide6 import QtCore, QtGui

from usd_chunks import chunk, chunk_icons, chunk_registry, chunk_resources, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks.frontends.qt import node_theme
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.widgets import ChunkWidget, ContainerWidget
from usd_chunks.frontends.qt.nodegraph import _PORT_TYPE_COLOR, ChunkGraphView, ChunkNodeItem

from qt_wait import pump_until


class _Chunk:
    """Enough of a chunk for node_icon: its own icon, and a config with one."""

    def __init__(self, icon="", config_icon=""):
        self.icon = icon
        self._config_icon = config_icon

    def get_config(self):
        return type("Config", (), {"icon": self._config_icon})()


def _prim():
    """A prim, and the stage that owns it: a prim outlives a stage that has gone."""
    stage = Usd.Stage.CreateInMemory()
    return stage, stage.DefinePrim("/n")


def test_a_chunk_shows_the_icon_its_config_sets() -> None:
    icon, color = node_theme.node_icon(_Chunk(config_icon="posts"), None)
    assert icon == "posts"
    assert color == node_theme.icon_color("posts")


def test_a_chunk_that_sets_no_icon_shows_none() -> None:
    """Nothing guesses one: not from its type, its name or its category."""
    assert chunk_icons.node_icon(_Chunk(), None)[0] == ""
    assert chunk_icons.node_icon(chunk.Chunk("Light"), None)[0] == ""


def test_the_chunks_own_icon_beats_its_config() -> None:
    assert chunk_icons.node_icon(_Chunk(icon="zap", config_icon="posts"), None)[0] == "zap"


def test_an_authored_usdui_icon_and_colour_win() -> None:
    _stage, prim = _prim()
    api = UsdUI.NodeGraphNodeAPI.Apply(prim)
    api.CreateIconAttr().Set(Sdf.AssetPath("camera"))
    api.CreateDisplayColorAttr().Set(Gf.Vec3f(1.0, 0.0, 0.0))
    icon, color = node_theme.node_icon(_Chunk(icon="zap", config_icon="posts"), prim)
    assert icon == "camera"
    assert color == QtGui.QColor.fromRgbF(1.0, 0.0, 0.0)


def test_every_bundled_chunk_names_a_builtin_icon() -> None:
    """A typo in a chunk's config() icon would quietly show nothing; catch it here."""
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(SceneSender(), "pytest")
    named = 0
    for module in reg.get_chunk_modules():
        icon = getattr(module.config, "icon", "")
        if icon:
            named += 1
            assert chunk_resources.get_node_icon(icon) is not None, (module.module_name, icon)
    assert named > 50


def test_a_group_shows_only_the_icon_it_sets(qapp) -> None:
    c = chunk.Chunk("Groups")
    c.add_param(chunk.Group("translate_grp", "Translate"))
    c.add_param(chunk.Group("moved_grp", "Moved", icon="move"))
    widget = ChunkWidget(c, for_proxy=True)
    try:
        headers = {w.group.name: w.minimize_button for w in widget.findChildren(ContainerWidget)
                   if w is not widget}
        plain = headers["translate_grp"].icon().pixmap(32, 16).toImage()
        with_icon = headers["moved_grp"].icon().pixmap(32, 16).toImage()

        def lit(image):
            return sum(image.pixelColor(x, y).alpha() > 0
                       for x in range(image.width()) for y in range(image.height()))

        # Both draw their fold arrow; only the one that set an icon draws more than that.
        assert lit(with_icon) > lit(plain) * 2
    finally:
        widget.deleteLater()


def test_the_params_panel_shows_the_chunk_and_group_icons(qapp) -> None:
    c = chunk.Chunk("Groups")
    c.icon = "posts"
    c.add_param(chunk.Group("translate_grp", "Translate",
                            flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED))
    c.add_param(chunk.Group("moved_grp", "Moved", icon="move",
                            flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED))
    panel = ChunkWidget(c)  # the Params panel's, not a node body
    try:
        headers = {w.group.name: w.minimize_button for w in (panel, *panel.findChildren(ContainerWidget))}

        assert not headers["Groups"].icon().isNull()
        assert not headers["moved_grp"].icon().isNull()
        # A group that sets no icon keeps the panel's own fold arrow.
        assert headers["translate_grp"].icon().isNull()
        assert headers["translate_grp"].arrowType() != QtCore.Qt.ArrowType.NoArrow
    finally:
        panel.deleteLater()


def test_icon_takes_a_builtin_name_or_an_existing_path(tmp_path) -> None:
    assert chunk.Group("g", "G", icon="sun").icon == "sun"
    image = tmp_path / "mine.png"
    image.write_bytes(b"")
    assert chunk.Group("g", "G", icon=str(image)).icon == str(image)
    with pytest.raises(ValueError):
        chunk.Group("g", "G", icon="not_a_builtin_or_a_file")


def test_every_builtin_icon_draws(qapp) -> None:
    names = {p.stem for p in (chunk_resources.get_icons_path() / "node").glob("*.svg")}
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


@pytest.mark.parametrize("graph", ["transform"], indirect=True)
def test_a_node_body_is_see_through_and_its_groups_are_not(graph) -> None:
    """The node paints its rounded frame behind the body; an opaque body squares it off.

    The chunk sits inside a chunkgroup here, a level deeper on the stage than a chunk at the
    root -- the case where its body came out opaque.
    """
    qapp, _, node = graph
    assert pump_until(qapp, lambda: node()._proxy is not None, seconds=3)
    body = node()._proxy.widget()
    assert body.group.get_identifier().count(".") >= 1, "expected a chunk inside a group"
    assert body._group_bg == "transparent"
    groups = [w for w in body.findChildren(ContainerWidget) if w is not body]
    assert groups and all(w._group_bg != "transparent" for w in groups)
