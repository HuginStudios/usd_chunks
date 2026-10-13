"""A LineEdit row is not shown while it has no parent.

ChunkLineEditWidget applies its state from __init__, before add_widget has parented it. A
show then is a top-level window's full show -- WA_DontShowOnScreen only keeps it off screen
-- and it cost ~1.4 ms per LineEdit, about an eighth of adding a Scatter Points node. The
row's layout shows the widget once it is parented, so construction only has to apply a hide.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.widgets import ChunkLineEditWidget, ChunkWidget


class _Scene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture(autouse=True)
def _registry():
    chunk_registry.ChunkRegistry().initialize(_Scene(), "pytest")


def _chunk() -> chunk.Chunk:
    c = chunk.Chunk("Fields")
    c.add_param(chunk.LineEdit("shown", "Shown"))
    hidden = chunk.LineEdit("hidden", "Hidden")
    hidden.set_state(ChunkFlags.State.HIDDEN)
    c.add_param(hidden)
    return c


def _field(widget: ChunkWidget, name: str) -> ChunkLineEditWidget:
    return next(w for w in widget.findChildren(ChunkLineEditWidget) if w.param.name == name)


def test_construction_does_not_show_an_unparented_field(qapp) -> None:
    field = ChunkLineEditWidget(chunk.LineEdit("loose", "Loose"))
    try:
        assert field.parentWidget() is None
        assert not field.testAttribute(QtCore.Qt.WidgetAttribute.WA_WState_ExplicitShowHide), \
            "__init__ showed the field before it had a parent"
    finally:
        field.deleteLater()


def test_a_field_still_shows_once_it_is_in_the_body(qapp) -> None:
    widget = ChunkWidget(_chunk())
    widget.show()
    qapp.processEvents()
    try:
        assert _field(widget, "shown").isVisible()
    finally:
        widget.close()
        widget.deleteLater()


def test_a_hidden_field_stays_hidden(qapp) -> None:
    widget = ChunkWidget(_chunk())
    widget.show()
    qapp.processEvents()
    try:
        assert not _field(widget, "hidden").isVisible()
    finally:
        widget.close()
        widget.deleteLater()
