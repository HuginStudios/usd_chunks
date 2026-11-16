"""A TextEdit holding megabytes (an inline terrain) shows a short read-only preview, not the text.

Qt lays out a QPlainTextEdit's whole lines, and a 10 MB inline_usd terrain is 48 lines of
megabytes each: scrolling the param panel took about five seconds a step.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from usd_chunks import chunk  # noqa: E402
from usd_chunks.frontends.qt.widgets import ChunkTextEditWidget  # noqa: E402

_HUGE = "#usda 1.0\ndef Mesh \"terrain\" {\n    point3f[] points = [" + "(1.5, 2.5, 3.5), " * 200_000 + "]\n}\n"


def _widget(value):
    param = chunk.TextEdit("code", default_value="")
    param.set_flag(chunk.ChunkFlags.ParamFlags.USD_SYNTAX_HIGHLIGHT)
    widget = ChunkTextEditWidget(param)
    widget.set_value(value)
    return widget


def test_huge_text_shows_a_short_read_only_preview(qapp):
    widget = _widget(_HUGE)
    shown = widget.text.document().toPlainText()
    assert len(shown) < 20_000
    assert "read-only" in shown
    assert widget.text.isReadOnly()


def test_the_preview_never_replaces_the_value(qapp):
    widget = _widget(_HUGE)
    # A focus-out sends get_value() back to the param: the whole text, not the preview.
    assert widget.get_value() == _HUGE


def test_huge_text_draws_quickly(qapp):
    widget = _widget(_HUGE)
    widget.resize(400, 300)
    widget.show()
    start = time.perf_counter()
    widget.text.horizontalScrollBar().setValue(5000)
    widget.repaint()
    qapp.processEvents()
    widget.hide()
    assert time.perf_counter() - start < 0.5


def test_many_short_lines_stay_editable(qapp):
    # 3000 short lines, 70 KB: under both limits, though lines 2000 on add up past HUGE_LINE.
    text = "\n".join(f"    float x{i} = {i}.5" for i in range(3000))
    widget = _widget(text)
    assert not widget.text.isReadOnly()
    assert widget.text.document().toPlainText() == text


def test_ordinary_text_stays_editable(qapp):
    widget = _widget(_HUGE)
    widget.set_value("#usda 1.0\ndef Sphere \"s\" {}\n")
    assert not widget.text.isReadOnly()
    assert widget.text.document().toPlainText() == "#usda 1.0\ndef Sphere \"s\" {}\n"
    assert widget.get_value() == "#usda 1.0\ndef Sphere \"s\" {}\n"
