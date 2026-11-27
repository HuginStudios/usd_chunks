"""Drag the bar under a text field to make it taller or shorter.

Reported: a ChunkGroup's Description opened three lines tall in the Params tab and could not
be made bigger, so a longer description was read through a slot, scrolling.
"""
from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender

QtCore = pytest.importorskip("PySide6.QtCore")
QtTest = pytest.importorskip("PySide6.QtTest")

from qt_wait import pump_until  # noqa: E402 -- after the Qt skip
from usd_chunks.frontends.qt.widgets import ChunkParamWidgetBase, ChunkTextEditWidget, ChunkWidget  # noqa: E402

DESCRIPTION = "\n".join(f"Line {i} of a long description." for i in range(12))


class _Scene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture(autouse=True)
def _registry():
    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


def _drag(grip, dy: int) -> None:
    start = grip.rect().center()
    end = start + QtCore.QPoint(0, dy)
    QtTest.QTest.mousePress(grip, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, start)
    QtTest.QTest.mouseMove(grip, end)
    QtTest.QTest.mouseRelease(grip, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, end)


def _chunk(group_flags) -> chunk.Chunk:
    c = chunk.Chunk("Docs")
    grp = chunk.Group("about", "About", flags=group_flags)
    grp.add_param(chunk.TextEdit("doc", "Description", default_value=DESCRIPTION, rows=3,
                                 flags=ChunkFlags.ParamFlags.LABELED))
    grp.add_param(chunk.Slider("width", "Width", default_value=1.0))
    c.add_param(grp)
    return c


def _shown(widget, qapp) -> None:
    widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.setFixedWidth(320)
    widget.show()
    pump_until(qapp, lambda: False, seconds=0.3)


def _top(child, widget) -> int:
    return child.mapTo(widget, child.rect().topLeft()).y()


def test_dragging_the_grip_resizes_the_field(qapp):
    widget = ChunkTextEditWidget(chunk.TextEdit("doc", "Description", default_value=DESCRIPTION, rows=3))
    try:
        _shown(widget, qapp)
        start = widget.text.height()

        _drag(widget.grip, 80)
        assert widget.text.height() == start + 80

        _drag(widget.grip, -1000)
        line = widget.text.fontMetrics().lineSpacing()
        assert line <= widget.text.height() < start, "shrinks, but never below one line"
    finally:
        widget.deleteLater()


@pytest.mark.parametrize("group_flags", [
    ChunkFlags.GroupFlags.LABELED,
    ChunkFlags.GroupFlags.LABELED | ChunkFlags.GroupFlags.COLLAPSABLE,
], ids=["plain group", "collapsable group"])
def test_the_params_tab_makes_room_for_a_taller_field(qapp, group_flags):
    c = _chunk(group_flags)  # held: widgets keep only weak references to their chunk
    widget = ChunkWidget(c)
    try:
        _shown(widget, qapp)
        field = widget.findChild(ChunkTextEditWidget)
        slider = next(w for w in widget.findChildren(ChunkParamWidgetBase) if w.param.name == "width")
        before = widget.sizeHint().height()
        gap = _top(slider, widget) - _top(field, widget)

        _drag(field.grip, 120)
        pump_until(qapp, lambda: False, seconds=0.3)

        assert widget.sizeHint().height() == before + 120
        # The slider below moved down with it rather than the field running under it.
        assert _top(slider, widget) - _top(field, widget) == gap + 120
    finally:
        widget.deleteLater()


def _assert_no_grip(widget, qapp) -> None:
    try:
        _shown(widget, qapp)
        field = widget.findChild(ChunkTextEditWidget)
        assert field.isVisible()
        assert not field.grip.isVisible()
        assert field.height() == field.text.height()
    finally:
        widget.deleteLater()


def test_a_graph_node_body_has_no_grip(qapp):
    # A node keeps the size it was laid out at: a field dragged taller would spill out of it.
    c = _chunk(ChunkFlags.GroupFlags.LABELED)
    _assert_no_grip(ChunkWidget(c, for_proxy=True), qapp)


def test_a_chunkgroup_node_has_no_grip_on_its_description(qapp):
    # A group's node shows its own params through a path of its own (ChunkWidget.add_widget).
    with Scene.new() as scene:
        group = scene.create.chunkgroup(name="Docs", doc=DESCRIPTION)
        _assert_no_grip(ChunkWidget(group, for_proxy=True), qapp)
