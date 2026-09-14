"""The slider's number field: a text field that only admits a number, and steps the digit
under the caret.

It was a QDoubleSpinBox with a singleStep of 0.001 (the slider's resolution) shown at two
decimals: Up/Down changed the value by less than the field could show, so the arrows looked
dead, and there was no way to step the tenths or the tens at all. And inside usdview the
Left/Right keys never reached the caret -- usdview's application-wide filter turns them
into frame stepping unless the focused widget is one it recognises as text, and a spin box
reached through a proxy is not reliably that.

Now it is a QLineEdit: digits, one point, a sign. Left/Right move the caret like any text
field. Up/Down step the digit the caret is on, the way Houdini's and Nuke's fields work:
after the tenths, by 0.1; after the hundreds, by 100.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtTest

from usd_chunks import chunk
from usd_chunks.plugins.chunk_qt_widgets import ChunkSliderWidget, NumberField, caret_step


# ---------------------------------------------------------------------------
# the rule, without a widget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, caret, step", [
    ("12.500", 6, 0.001),   # after the last decimal
    ("12.500", 4, 0.1),     # after the tenths
    ("12.500", 3, 0.1),     # just after the point: the tenths, the digit to its right
    ("12.500", 2, 1.0),     # after the 2
    ("12.500", 1, 10.0),    # after the 1
    ("12.500", 0, 10.0),    # at the start: the digit to the right
    ("-3.25", 0, 1.0),      # at the start of a negative: the 3, not the sign
    ("-3.25", 1, 1.0),      # after the sign: still the 3
    ("1200", 4, 1.0),       # an int field has no point; the end is the units
    ("1200", 2, 100.0),
])
def test_the_caret_picks_the_place_value(text, caret, step) -> None:
    assert caret_step(text, caret)[1] == pytest.approx(step)


def test_no_digit_means_no_step() -> None:
    assert caret_step("", 0) == (-1, None)
    assert caret_step("-", 1) == (-1, None)


# ---------------------------------------------------------------------------
# the field
# ---------------------------------------------------------------------------

def _field(qapp, value, is_float=True):
    field = NumberField(is_float=is_float)
    field.setValue(value)
    return field


def test_left_and_right_move_the_caret(qapp) -> None:
    """The reported gap: the arrow keys could not reach the caret at all."""
    field = _field(qapp, 12.5)
    field.setCursorPosition(4)                  # "12.5|"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Left)
    assert field.cursorPosition() == 3
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Left)
    assert field.cursorPosition() == 2
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Right)
    assert field.cursorPosition() == 3


def test_up_after_the_tenths_adds_a_tenth(qapp) -> None:
    field = _field(qapp, 12.5)
    field.setCursorPosition(4)                  # "12.5|"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)

    assert field.value() == pytest.approx(12.6)
    assert field.text() == "12.6"


def test_down_after_the_tens_takes_ten(qapp) -> None:
    field = _field(qapp, 12.5)
    field.setCursorPosition(1)                  # "1|2.5"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Down)

    assert field.value() == pytest.approx(2.5)


def test_page_keys_step_ten_of_the_digit(qapp) -> None:
    field = _field(qapp, 0.25)
    field.setCursorPosition(4)                  # "0.25|"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_PageUp)

    assert field.value() == pytest.approx(0.35)


def test_the_caret_keeps_its_place_when_the_text_grows(qapp) -> None:
    """9.9 -> 10.0 gains a digit on the left. Holding the key must keep stepping the tenths,
    so the caret follows the place value, not the character index."""
    field = _field(qapp, 9.9)
    field.setCursorPosition(3)                  # "9.9|"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)
    assert field.text() == "10.0"
    assert field.cursorPosition() == 4          # still after the tenths

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)
    assert field.value() == pytest.approx(10.1)


def test_stepping_stays_at_the_fields_precision(qapp) -> None:
    """0.1 + 0.2 is 0.30000000000000004 in binary; the text must not grow ten digits."""
    field = _field(qapp, 0.2)
    field.setCursorPosition(3)

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)

    assert field.text() == "0.3"


def test_only_a_number_can_be_typed(qapp) -> None:
    field = _field(qapp, 1.0)
    field.selectAll()

    QtTest.QTest.keyClicks(field, "abc-2.x5.7")

    assert field.text() == "-2.57", "letters and a second point are refused"
    assert field.value() == pytest.approx(-2.57)


def test_a_half_typed_number_changes_nothing_yet(qapp) -> None:
    field = _field(qapp, 4.0)
    seen = []
    field.valueChanged.connect(seen.append)
    field.selectAll()

    QtTest.QTest.keyClicks(field, "-")
    assert seen == [] and field.value() == pytest.approx(4.0)

    QtTest.QTest.keyClicks(field, "3")
    assert seen == [-3.0]


def test_an_int_field_steps_its_digits_and_refuses_a_point(qapp) -> None:
    field = _field(qapp, 1200, is_float=False)
    field.setCursorPosition(2)                  # "12|00"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)
    assert field.value() == 1300
    assert isinstance(field.value(), int)

    field.setCursorPosition(4)
    QtTest.QTest.keyClicks(field, ".5")
    assert field.text() == "13005"


def test_the_shown_precision_matches_the_slider(qapp) -> None:
    """The original symptom: at two decimals a value of 0.125 read 0.13."""
    assert _field(qapp, 0.125).text() == "0.125"
    assert _field(qapp, 12.0).text() == "12.0", "one decimal digit always shown to put the caret after"


def test_the_slider_widget_follows_the_field(qapp) -> None:
    """Through ChunkSliderWidget: a keystroke in the field moves the slider and the param."""
    param = chunk.Slider("s", min_value=0.0, max_value=10.0, default_value=1.25)
    widget = ChunkSliderWidget(param)
    field = widget.spin_box
    field.setCursorPosition(2)                  # "1.|25" -> the tenths

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)

    assert param.value == pytest.approx(1.35)
    assert widget.slider.value() == 1350
