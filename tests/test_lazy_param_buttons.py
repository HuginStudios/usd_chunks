"""Buttons a param widget only sometimes needs are made when it needs them.

A LineEdit's pin button belongs to prim-selection fields and a container's list button to
ListGroups. Built for every field and group and then hidden, they were about a quarter of a
Scatter Points body's widgets, each constructed, styled and laid out for nothing.
"""
from __future__ import annotations

import pytest

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.widgets import ChunkLineEditWidget, ChunkWidget, ContainerWidget


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
