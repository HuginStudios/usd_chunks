"""A node body puts each one-line param's label beside its field, not above it.

Labels on their own line made every slider two rows tall, so nodes in the graph came out
long and thin. The params panel keeps labels above; only node bodies (ChunkWidget built
for_proxy, the way the node graph builds them) go inline.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from qt_wait import pump_until
from usd_chunks.frontends.qt.widgets import (
    INLINE_LABEL_MAX_SHARE, ChunkParamWidgetBase, ChunkWidget, LabeledWidget,
)


class _Scene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture(autouse=True)
def _registry():
    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


def _chunk() -> chunk.Chunk:
    c = chunk.Chunk("Rows")
    c.add_param(chunk.Slider("length", "Length", default_value=1.0))
    c.add_param(chunk.Slider("ground_clearance_height", "Ground Clearance Height", default_value=1.0))
    c.add_param(chunk.TextEdit("notes", "Notes"))
    grp = chunk.Group("posts", "Posts")
    grp.add_param(chunk.Slider("width", "Width", default_value=1.0))
    c.add_param(grp)
    return c


def _row(widget: ChunkWidget, name: str) -> LabeledWidget:
    return next(r for r in widget.findChildren(LabeledWidget)
                if isinstance(r.widget, ChunkParamWidgetBase) and r.widget.param.name == name)


def _shown(widget: ChunkWidget, qapp, width: int = 220) -> None:
    widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.setFixedWidth(width)
    widget.show()
    # Rows fit their label column on a queued call, after the body has its width.
    pump_until(qapp, lambda: False, seconds=0.2)


def test_a_node_body_puts_the_label_beside_the_field(qapp) -> None:
    widget = ChunkWidget(_chunk(), for_proxy=True)
    try:
        _shown(widget, qapp)
        for name in ("length", "width"):
            row = _row(widget, name)
            label, field = row.label.geometry(), row.widget.geometry()
            assert field.left() > label.right(), f"{name}: the field slides under its label"
            assert abs(label.center().y() - field.center().y()) <= 2, f"{name}: not one row"
    finally:
        widget.deleteLater()


def test_the_label_column_fits_the_longest_label_up_to_a_share_of_the_width(qapp) -> None:
    c = chunk.Chunk("Short")
    c.add_param(chunk.Slider("x", "X", default_value=1.0))
    c.add_param(chunk.Slider("y", "Y", default_value=1.0))
    short = ChunkWidget(c, for_proxy=True)
    long = ChunkWidget(_chunk(), for_proxy=True)
    try:
        _shown(short, qapp)
        _shown(long, qapp)
        x = _row(short, "x").label
        assert x.width() < 40, "a node of one-letter labels keeps its column narrow"
        assert x.alignment() & QtCore.Qt.AlignmentFlag.AlignLeft
        top = {_row(long, n).label.width() for n in ("length", "ground_clearance_height")}
        assert len(top) == 1, "the labels of one group share a column"
        assert top.pop() == int(220 * INLINE_LABEL_MAX_SHARE)
        # "Width" sits in the Posts group, and its column fits Posts' own labels: the long
        # name above it does not push its field away.
        assert _row(long, "width").label.width() < int(220 * INLINE_LABEL_MAX_SHARE)
    finally:
        short.deleteLater()
        long.deleteLater()


def test_a_wider_body_shows_a_label_it_cut_short(qapp) -> None:
    widget = ChunkWidget(_chunk(), for_proxy=True)
    try:
        _shown(widget, qapp)
        label = _row(widget, "ground_clearance_height").label
        assert QtWidgets.QLabel.text(label).endswith("…")
        # Wide enough that the whole label fits under the column's share of the width.
        text_width = label.fontMetrics().horizontalAdvance(label.text())
        widget.setFixedWidth(int((text_width + 40) / INLINE_LABEL_MAX_SHARE))
        assert pump_until(qapp, lambda: QtWidgets.QLabel.text(label) == "Ground Clearance Height",
                          seconds=2), QtWidgets.QLabel.text(label)
    finally:
        widget.deleteLater()


def test_a_long_label_is_cut_short_but_keeps_its_text(qapp) -> None:
    widget = ChunkWidget(_chunk(), for_proxy=True)
    try:
        _shown(widget, qapp)
        label = _row(widget, "ground_clearance_height").label
        assert label.text() == "Ground Clearance Height"
        assert QtWidgets.QLabel.text(label).endswith("…")
    finally:
        widget.deleteLater()


def test_a_multi_line_field_keeps_its_label_above(qapp) -> None:
    widget = ChunkWidget(_chunk(), for_proxy=True)
    try:
        row = _row(widget, "notes")
        assert row.vlayout.indexOf(row.label) == 0
    finally:
        widget.deleteLater()


def test_the_params_panel_keeps_labels_above(qapp) -> None:
    widget = ChunkWidget(_chunk())
    try:
        row = _row(widget, "length")
        assert row.vlayout.indexOf(row.label) == 0
    finally:
        widget.deleteLater()
