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
from PySide6 import QtCore, QtTest, QtWidgets

from usd_chunks import chunk
from usd_chunks.frontends.qt.widgets import (
    ChunkFloatSpinboxWidget,
    ChunkIntSpinboxWidget,
    ChunkSliderWidget,
    NumberField,
    caret_step,
)


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
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    assert field.value() == pytest.approx(-2.57)


def test_a_typed_number_is_a_change_only_on_enter(qapp) -> None:
    """Typing 5000 into a chunk that takes seconds to cook cooked it for 5, 50 and 500
    first: every keystroke was a change."""
    field = _field(qapp, 4.0)
    seen = []
    field.valueChanged.connect(seen.append)
    field.selectAll()

    QtTest.QTest.keyClicks(field, "-3")
    assert seen == [] and field.value() == pytest.approx(4.0)

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    assert seen == [-3.0] and field.value() == pytest.approx(-3.0)


def test_what_does_not_read_as_a_number_goes_back_to_the_value(qapp) -> None:
    field = _field(qapp, 4.0)
    seen = []
    field.valueChanged.connect(seen.append)
    field.selectAll()

    QtTest.QTest.keyClicks(field, "-")
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)

    assert seen == [] and field.text() == "4.0"


def test_leaving_the_field_takes_the_typed_number(qapp) -> None:
    window = QtWidgets.QWidget()
    field = NumberField(parent=window)
    other = QtWidgets.QLineEdit(window)
    field.setValue(4.0)
    window.show()
    window.activateWindow()
    field.setFocus()
    qapp.processEvents()
    seen = []
    field.valueChanged.connect(seen.append)
    field.selectAll()

    QtTest.QTest.keyClicks(field, "12")
    other.setFocus()
    qapp.processEvents()

    assert seen == [12.0]
    window.close()


def test_up_steps_from_the_number_typed_so_far(qapp) -> None:
    field = _field(qapp, 4.0)
    seen = []
    field.valueChanged.connect(seen.append)
    field.selectAll()

    QtTest.QTest.keyClicks(field, "12")         # "12|", the ones
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Up)

    assert seen == [13.0], "one change, from the typed 12, not from the 4"


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


def test_typing_into_the_slider_widget_changes_its_param_once(qapp) -> None:
    param = chunk.Slider("budget", is_int=True, min_value=0, max_value=1_000_000, default_value=3000)
    widget = ChunkSliderWidget(param)
    field = widget.spin_box
    values = []
    field.valueChanged.connect(lambda _v: values.append(param.value))
    field.selectAll()

    QtTest.QTest.keyClicks(field, "5000")
    assert param.value == 3000, "nothing taken while typing"

    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
    assert param.value == 5000 and values == [5000]
    assert widget.slider.value() == 5000


@pytest.mark.parametrize("widget_type, is_int, typed, expected", [
    (ChunkIntSpinboxWidget, True, "50", 50),
    (ChunkFloatSpinboxWidget, False, "2.5", 2.5),
])
def test_a_spinbox_takes_a_typed_number_on_enter(qapp, widget_type, is_int, typed, expected) -> None:
    param = chunk.Spinbox("n", is_int=is_int, min_value=0, max_value=100, default_value=1)
    widget = widget_type(param)
    widget.lineEdit().selectAll()

    QtTest.QTest.keyClicks(widget, typed)
    assert param.value == 1, "nothing taken while typing"

    QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_Return)
    assert param.value == pytest.approx(expected)
