"""Parts a param widget only sometimes needs are made when it needs them.

A LineEdit's pin button belongs to prim-selection fields, a container's list button to
ListGroups, and a row's label to rows with label text. Built for every one and then hidden,
they were about a quarter of a Scatter Points body's widgets, each constructed, styled and
laid out for nothing.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.widgets import (
    ChunkLineEditWidget, ChunkParamWidgetBase, ChunkWidget, ContainerWidget, LabeledWidget,
)


class _Scene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture(autouse=True)
def _registry():
    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


def _field(flags=ChunkFlags.ParamFlags.LABELED) -> ChunkLineEditWidget:
    return ChunkLineEditWidget(chunk.LineEdit("path", "Path", flags=flags))


def test_a_plain_field_has_no_pin_button(qapp) -> None:
    field = _field()
    assert field.button is None
    field.deleteLater()


def test_a_prim_selection_field_has_one(qapp) -> None:
    field = _field(ChunkFlags.ParamFlags.LABELED | ChunkFlags.ParamFlags.PRIM_SELECTION)
    assert field.button is not None
    assert not field.button.isHidden()
    field.deleteLater()


def test_becoming_a_prim_selection_field_adds_it(qapp) -> None:
    field = _field()
    field._sync_pin_button(True)
    assert field.button is not None and not field.button.isHidden()
    field._sync_pin_button(False)
    assert field.button.isHidden()
    field.deleteLater()


def _containers(widget: ChunkWidget, name: str) -> list[ContainerWidget]:
    return [c for c in widget.findChildren(ContainerWidget) if c.group.name == name]


def test_only_a_list_group_gets_a_list_button(qapp) -> None:
    c = chunk.Chunk("Holder")
    plain = chunk.Group("plain", "Plain")
    plain.add_param(chunk.LineEdit("a", "A"))
    rows = chunk.ListGroup("rows", "Rows")
    rows.add_param(chunk.LineEdit("b", "B"))
    c.add_param(plain)
    c.add_param(rows)
    widget = ChunkWidget(c)
    try:
        (plain_w,) = _containers(widget, "plain")
        (rows_w,) = _containers(widget, "rows")
        assert plain_w.list_expand_button is None
        assert rows_w.list_expand_button is not None
        layout = rows_w.__layout__
        assert layout.indexOf(rows_w.list_expand_button) == layout.indexOf(rows_w.minimize_button) + 1
    finally:
        widget.deleteLater()


def _row(widget: ChunkWidget, name: str) -> LabeledWidget:
    return next(r for r in widget.findChildren(LabeledWidget)
                if isinstance(r.widget, ChunkParamWidgetBase) and r.widget.param.name == name)


def test_a_row_label_is_made_only_when_the_row_has_text(qapp) -> None:
    c = chunk.Chunk("Rows")
    c.add_param(chunk.LineEdit("plain", "Plain", flags=ChunkFlags.ParamFlags(0)))
    c.add_param(chunk.LineEdit("named", "Named", flags=ChunkFlags.ParamFlags.LABELED))
    widget = ChunkWidget(c)
    try:
        assert _row(widget, "plain").label is None
        named = _row(widget, "named")
        assert named.label is not None and named.label.text() == "Named"
        assert named.vlayout.indexOf(named.label) == 0, "the label sits above the widget"
    finally:
        widget.deleteLater()


def test_a_label_arriving_later_gets_the_filters_queued_for_it(qapp) -> None:
    c = chunk.Chunk("Rows")
    c.add_param(chunk.LineEdit("plain", "Plain", flags=ChunkFlags.ParamFlags(0)))
    widget = ChunkWidget(c)
    try:
        row = _row(widget, "plain")
        seen = []

        class _Spy(QtCore.QObject):
            def eventFilter(self, obj, event):
                if obj is row.label and event.type() == QtCore.QEvent.Type.Enter:
                    seen.append(event)
                return False

        spy = _Spy()
        row.add_label_filter(spy)
        row.update_label("Later")
        assert row.label is not None and not row.label.isHidden()
        assert row.vlayout.indexOf(row.label) == 0
        QtWidgets.QApplication.sendEvent(row.label, QtCore.QEvent(QtCore.QEvent.Type.Enter))
        assert seen, "a filter added before the label existed was not installed on it"
    finally:
        widget.deleteLater()
