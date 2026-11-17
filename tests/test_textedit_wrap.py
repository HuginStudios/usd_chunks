"""A TextEdit with wrap=True is a prose field: it wraps at its edge, in the UI's own font.

Reported: Text Overlay's text field showed a paragraph as one long line, scrolled sideways,
since every TextEdit was a code field (fixed-width font, no wrapping). Code fields stay so.
"""
from __future__ import annotations

import pytest

from usd_chunks import chunk

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtGui = pytest.importorskip("PySide6.QtGui")

from usd_chunks.frontends.qt.widgets import ChunkTextEditWidget  # noqa: E402 -- after the Qt skip

PARAGRAPH = "Cold-pressed pine resin and cedar oil in a glass bottle made to be refilled, not replaced."


def _fixed_font():
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)


def test_a_prose_field_wraps_a_long_line_in_the_ui_font(qapp):
    widget = ChunkTextEditWidget(chunk.TextEdit("text", "Text", default_value=PARAGRAPH, rows=4, wrap=True))
    widget.resize(200, 120)
    widget.show()
    qapp.processEvents()

    assert widget.text.lineWrapMode() == QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth
    assert widget.text.font().family() != _fixed_font().family()
    # One paragraph, laid out on more than one line in a narrow field.
    block = widget.text.document().firstBlock()
    assert block.layout().lineCount() > 1
    widget.close()


def test_a_code_field_keeps_its_lines_whole_in_a_fixed_width_font(qapp):
    widget = ChunkTextEditWidget(chunk.TextEdit("code", "Code", default_value=PARAGRAPH, rows=4))

    assert widget.text.lineWrapMode() == QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
    assert widget.text.font().family() == _fixed_font().family()
    widget.close()


def test_wrap_is_off_unless_asked_for():
    assert chunk.TextEdit("t").wrap is False
