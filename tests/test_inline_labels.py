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
from usd_chunks.frontends.qt.widgets import (
    INLINE_LABEL_WIDTH, ChunkParamWidgetBase, ChunkWidget, LabeledWidget,
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


def _shown(widget: ChunkWidget, qapp) -> None:
    widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.setFixedWidth(220)
    widget.show()
    qapp.processEvents()


def test_a_node_body_puts_the_label_beside_the_field(qapp) -> None:
    widget = ChunkWidget(_chunk(), for_proxy=True)
    try:
        _shown(widget, qapp)
        for name in ("length", "width"):
            row = _row(widget, name)
            label, field = row.label.geometry(), row.widget.geometry()
            assert label.width() == INLINE_LABEL_WIDTH
            assert field.left() > label.right(), f"{name}: the field slides under its label"
            assert abs(label.center().y() - field.center().y()) <= 2, f"{name}: not one row"
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
