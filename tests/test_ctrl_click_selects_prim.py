"""Ctrl+click a chunk's prim path field to select that prim in the viewport.

The pin button already goes one way -- viewport selection into the field. This is the other
way, for finding what a Reference or Transform chunk is pointing at in a scene with dozens
of them. It applies to any field holding existing prim paths, and stays an ordinary click
everywhere else.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore
from PySide6.QtTest import QTest

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins.chunk_qt_widgets import ChunkLineEditWidget
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init

CTRL = QtCore.Qt.KeyboardModifier.ControlModifier
LEFT = QtCore.Qt.MouseButton.LeftButton


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Kettle", "Xform")
    stage.DefinePrim("/World/Toaster", "Xform")

    class _Scene(SceneSender):
        def __init__(self):
            super().__init__()
            self.selected = []

        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

        def select_prims(self, paths):
            self.selected.append([str(p) for p in paths])
            return True

    s = _Scene()
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return s


def _mk(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


_built_widgets = []


@pytest.fixture(autouse=True)
def _delete_built_widgets(qapp):
    """The field widgets here have no parent, so nothing else ever deletes them."""
    yield
    while _built_widgets:
        _built_widgets.pop().deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _field(qapp, chunk_obj, value):
    param = chunk_obj.get_param("primpath_location")
    param.value = value
    qapp.processEvents()
    widget = ChunkLineEditWidget(param)
    widget.set_value(value)
    _built_widgets.append(widget)
    return widget


def test_ctrl_click_on_a_transform_target_selects_it(qapp, scene):
    widget = _field(qapp, _mk(scene, "transform"), "/World/Kettle")

    QTest.mouseClick(widget.text, LEFT, CTRL)

    assert scene.selected == [["/World/Kettle"]]


def test_it_works_on_a_reference_location_too(qapp, scene):
    """Not a PRIM_SELECTION field -- it still names a prim."""
    ref = _mk(scene, "reference")
    assert not ref.get_param("primpath_location").has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION)
    widget = _field(qapp, ref, "/World/Toaster")

    QTest.mouseClick(widget.text, LEFT, CTRL)

    assert scene.selected == [["/World/Toaster"]]


def test_a_plain_click_selects_nothing(qapp, scene):
    widget = _field(qapp, _mk(scene, "transform"), "/World/Kettle")

    QTest.mouseClick(widget.text, LEFT)

    assert scene.selected == []


def test_every_path_in_a_list_is_selected(qapp, scene):
    widget = _field(qapp, _mk(scene, "transform"), "/World/Kettle,/World/Toaster")

    QTest.mouseClick(widget.text, LEFT, CTRL)

    assert scene.selected == [["/World/Kettle", "/World/Toaster"]]


@pytest.mark.parametrize("value", [
    "/World/NotThere",          # a path nothing is authored at yet
    "Kettle",                   # a name, not a path
    "/World/*",                 # a glob
    "/World/Kettle,/World/Gone",  # one real, one not: all or nothing
    "",
])
def test_anything_but_existing_prims_is_an_ordinary_click(qapp, scene, value):
    widget = _field(qapp, _mk(scene, "transform"), value)

    QTest.mouseClick(widget.text, LEFT, CTRL)

    assert scene.selected == []


def test_an_expression_field_selects_what_it_resolves_to(qapp, scene):
    """The box shows `@Ref.primpath_location@`; the prim is the value it resolves to."""
    ref = _mk(scene, "reference")
    ref.get_param("primpath_location").value = "/World/Toaster"
    qapp.processEvents()
    xf = _mk(scene, "transform")
    widget = _field(qapp, xf, f"@{ref.name}.primpath_location@")

    QTest.mouseClick(widget.text, LEFT, CTRL)

    assert scene.selected == [["/World/Toaster"]]


def test_a_click_that_selected_does_not_also_move_the_text_cursor(qapp, scene):
    widget = _field(qapp, _mk(scene, "transform"), "/World/Kettle")
    widget.text.setCursorPosition(0)

    QTest.mouseClick(widget.text, LEFT, CTRL, QtCore.QPoint(widget.text.width() - 2, 5))

    assert widget.text.cursorPosition() == 0


def test_a_host_that_cannot_select_leaves_the_click_alone(qapp, scene):
    scene.select_prims = lambda paths: False
    widget = _field(qapp, _mk(scene, "transform"), "/World/Kettle")

    assert widget.select_in_scene() is False


def test_the_base_scene_has_no_selection_to_drive():
    class _Bare(SceneSender):
        def get_stage(self):
            return None

        def get_selected_prims(self):
            return []

    assert _Bare().select_prims([Sdf.Path("/World")]) is False


# --- usdview ------------------------------------------------------------------------------

class _Selection:
    """usdview's selection data model, as far as select_prims touches it."""

    def __init__(self):
        self.paths = ["/Old"]
        self.signals = 0
        self._batching = 0
        outer = self

        class _Batch:
            def __enter__(self):
                outer._batching += 1

            def __exit__(self, *args):
                outer._batching -= 1
                outer.signals += 1

        self.batchPrimChanges = _Batch()

    def clearPrims(self):
        assert self._batching, "must be batched"
        self.paths = []

    def addPrimPath(self, path):
        assert self._batching, "must be batched"
        self.paths.append(str(path))


@pytest.fixture
def usdview_scene():
    pytest.importorskip("pxr.Usdviewq", reason="usdview is not part of the usd-core wheel")
    from types import SimpleNamespace
    from usd_chunks.plugins.chunk_usdview import UsdviewScene

    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Kettle", "Xform")
    selection = _Selection()
    s = UsdviewScene.__new__(UsdviewScene)
    s.usdviewAPI = SimpleNamespace(stage=stage, dataModel=SimpleNamespace(selection=selection))
    return s, selection


def test_usdview_replaces_the_selection_in_one_change(usdview_scene):
    s, selection = usdview_scene

    assert s.select_prims([Sdf.Path("/World/Kettle")]) is True
    assert selection.paths == ["/World/Kettle"]
    assert selection.signals == 1


def test_usdview_skips_paths_with_no_prim(usdview_scene):
    """usdview's own selection raises on those."""
    s, selection = usdview_scene

    assert s.select_prims(["/World/Kettle", "/World/Gone"]) is True
    assert selection.paths == ["/World/Kettle"]


def test_usdview_with_nothing_real_to_select_keeps_the_selection(usdview_scene):
    s, selection = usdview_scene

    assert s.select_prims(["/World/Gone"]) is False
    assert selection.paths == ["/Old"]


# --- in the node graph ----------------------------------------------------------------------

def test_ctrl_click_on_the_field_in_a_graph_node_selects_the_prim_not_the_node(qapp, scene):
    """The node claims ctrl+click on its own chrome to toggle node selection. On the field
    embedded in its body, the field has to get it -- and the node selection must not move."""
    from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker

    docker = ChunkWidgetDocker()
    docker.resize(900, 700)
    docker.show()
    qapp.processEvents()
    try:
        _click_through_graph(qapp, scene, docker)
    finally:
        # close() alone leaves the docker alive and listening to the registry, building
        # graph views -- and auto-wiring views -- for whatever the next test creates.
        scene.clear_chunk_selection()
        docker.close()
        docker.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _click_through_graph(qapp, scene, docker):
    import time
    from usd_chunks.chunk_flags import ChunkFlags as _Flags
    from usd_chunks.plugins.chunk_usd import get_chunk_prim_path

    xf = _mk(scene, "transform")
    xf.get_param("primpath_location").value = "/World/Kettle"
    view = docker.tab_widget.get_current_graph_view()
    path = get_chunk_prim_path(xf)
    field = None
    end = time.monotonic() + 5.0
    while time.monotonic() < end and field is None:
        qapp.processEvents()
        node = view.scene()._nodes.get(path)
        body = getattr(node, "_chunk_widget", None) if node is not None else None
        if body is not None:
            field = next((w for w in body.findChildren(ChunkLineEditWidget)
                          if w.param.name == "primpath_location"), None)
    assert field is not None, "the node's body never got built"
    if not view.viewport().isVisible():
        pytest.skip("graph page not visible in this docker configuration")

    view.centerOn(node)
    qapp.processEvents()
    local = field.text.mapTo(node._chunk_widget, field.text.rect().center())
    at = view.mapFromScene(node._proxy.mapToScene(QtCore.QPointF(local)))
    was_selected = xf.has_state(_Flags.State.SELECTED)

    QTest.mouseClick(view.viewport(), LEFT, CTRL, at)
    qapp.processEvents()

    assert scene.selected == [["/World/Kettle"]]
    assert xf.has_state(_Flags.State.SELECTED) == was_selected, "the click must not toggle the node"
