from __future__ import annotations
import threading
import weakref
from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Generic, NamedTuple

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_resources, chunk_utils
import usd_chunks.chunk_flags
from usd_chunks.chunk_widget_api import ChunkParamWidgetBase
from usd_chunks import chunk_naming
from usd_chunks import chunk_widget_api
from usd_chunks import chunk_core
from usd_chunks import chunk_stage
from usd_chunks.graph import engine as chunk_graph

import logging
import re
from collections.abc import Callable
from functools import partial
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

# Re-exported under the original private name: chunk_qt imports it from here, and the
# implementation is core (frontend-agnostic), not Qt.
_clean_description = chunk_registry.clean_description

#: Called with the chunks about to be deleted, before request_delete() runs on any of them.
#: The node graph registers ChunkGraphView.mark_pending_delete_relevance here; it imports
#: this module, so this module cannot import it back.
pre_delete_hooks: list[Callable[[list], None]] = []


def announce_pending_delete(chunks: list) -> None:
    """Tell every pre-delete hook that `chunks` are about to be deleted."""
    for hook in pre_delete_hooks:
        hook(chunks)


# Background tint for a tab's Params-page header panel (prim path field, lofted own-params,
# filter bar) -- distinct from the body's #333333 (CHUNK_STYLESHEET's default QFrame color).
_PARAMS_HEADER_BG = "#242424"
# The search box on a Params page: a shade apart from the group's box above it.
_PARAMS_SEARCH_BG = "#2b2b2b"
# The edge drawn round each of the two boxes on a Params page.
_PARAMS_BOX_EDGE = "#4a4a4a"


CHUNK_STYLESHEET = """
QCheckBox {
    background: transparent;
}
QFrame{
    background-color: #333333;
    border: none;
}
QComboBox{
    background-color: #202020;
}
QLineEdit{
    background-color: #202020;
}
QToolButton{
    border: 2px solid;
    text-align: center;border: 0px;
    border-radius: 0px;
    padding: 4px;
    background-color: #333333;
    color: #EEEEEE;
    border-color: rgb(156, 156, 156);
}
QPushButton{border: 1px solid;
    border-color: rgb(115, 115, 115);
    border-radius: 3px;
    background-color: #333333;
    }
    QPushButton:hover {
    border: 1px solid;
    border-radius: 3px;
    border-color: rgb(156, 156, 156);
}
QPushButton:focus {
    outline-color: transparent;
    border: 2px solid;
    border-color: rgb(243, 243, 243);
}
QPushButton:pressed{
    background-color: rgb(80, 80, 80);
}
QPushButton:disabled {
    color: rgb(40, 40, 40);
    border-color: rgb(40, 40, 40);
    background-color: rgb(40, 40, 40);
}
QSlider {
    min-height: 8px;
    background: transparent;
    height: 20px;
}
QSlider::groove:horizontal {
    border: 0px;
    background: #202020;
    height: 5px;
    border-radius: 10px;
}
QSlider::handle {
    background: #eeeeee;
    height: 10px;
    width: 10px;
    border-radius: 5px;
    margin: -3px 0px;
}
QSlider::handle:hover {
    background: #ffffff;
    height: 10px;
    width: 10px;
    border-radius: 5px;
    margin: -3px 0px;
}
QSlider::sub-page:horizontal {
    background: #404040;
    border-top-left-radius: 10px;
    border-bottom-left-radius: 10px;
}
QRangeSlider {
    qproperty-barColor: #202020;
}
.ChunkWidget {
    border: 0px solid transparent;
    border-radius: 0px;
}
.ChunkWidget[chunkSelected="true"] {
    border: 1px solid #cab46a;
    border-radius: 1px;
}
QLabel[chunkExpression="true"] {
    color: #7fb2e5;
    font-style: italic;
}
QLabel[chunkOverride="true"] {
    color: #e0b060;
}
QLabel[chunkHeld="true"] {
    color: #d07a6a;
}
QLabel[chunkLocked="true"] {
    color: #808080;
}
"""

# Marks a widget that carries CHUNK_STYLESHEET, so nothing below it applies it again.
_SHEET_FLAG = "_chunk_sheet_applied"
# Marks a class that always styles itself, so a descendant can skip *before* the ancestor
# has actually got round to it. ChunkWidget applies the sheet at the end of its own
# __init__, which is after its children have been built and asked this question.
_SELF_STYLING = "_chunk_styles_self"


def disconnect_when_destroyed(qobject: QtCore.QObject, connections) -> None:
    """Disconnect these NoticeConnections when Qt deletes qobject.

    Qt can delete a widget while its Python object lives on, still subscribed: it then kept
    reacting to notices, and one arriving later reached a dead C++ object. The lambda holds
    the connections, not qobject, so this does not keep qobject alive. connections may be a
    list the caller refills later; whatever it holds at destroy time is disconnected.

    Args:
        qobject: The Qt object whose deletion ends the connections.
        connections: The NoticeConnections to disconnect.
    """
    qobject.destroyed.connect(lambda *_, _conns=connections: [c.disconnect() for c in _conns])


def apply_chunk_stylesheet(widget: QtWidgets.QWidget) -> bool:
    """Give widget CHUNK_STYLESHEET, unless something above it already covers it.

    What matters is that exactly *one* widget per tree carries the sheet. Qt merges every
    ancestor sheet into each descendant's style resolution, so a second copy further down
    buys nothing and makes resolving the whole subtree dearer: applying it to all ten
    ChunkWidgets of a nine-page shader instead of the outermost one measured 175ms against
    117ms for the same tree.

    Args:
        widget (QtWidgets.QWidget): The widget to style

    Returns:
        bool: True if the sheet was applied, False if it was already covered
    """
    if getattr(widget, _SHEET_FLAG, False):
        return False

    parent = widget.parentWidget()
    while parent is not None:
        if getattr(parent, _SHEET_FLAG, False) or getattr(parent, _SELF_STYLING, False):
            # Deliberately not flagged on widget itself: it may be reparented out from
            # under this ancestor later (the node graph builds under a sink, then
            # detaches), and would need a sheet of its own at that point.
            return False
        parent = parent.parentWidget()

    setattr(widget, _SHEET_FLAG, True)
    widget.setStyleSheet(CHUNK_STYLESHEET)
    return True


class SyntaxHighlighter(QtGui.QSyntaxHighlighter):
    def __init__(self, parent, chunk_highlighter: chunk_core.SyntaxHighlightMapping):
        super().__init__(parent)
        self._hl = chunk_highlighter

    def highlightBlock(self, text):
        for start, end, style in self._hl.match_text(text):
            fmt = QtGui.QTextCharFormat()
            if style.get("bold"):
                fmt.setFontWeight(QtGui.QFont.Weight.Bold)

            fmt.setFontItalic(style.get("italic", False))
            fmt.setForeground(QtGui.QColor(style.get("color")))
            self.setFormat(start, end - start, fmt)



class _FineDragSlider(QtWidgets.QSlider):
    """A QSlider whose drag speed drops to a fraction of normal while Ctrl is held, for
    fine-tuning a value that spans a wide range in a narrow widget (e.g. a translate
    slider covering meters of range when the desired precision is centimeters). Qt's
    default drag jumps the value straight to whatever range-to-pixel position is under
    the cursor -- fine at full speed, but too coarse to hit a precise value in a wide
    range squeezed into a narrow widget. Only Ctrl+drag is affected; a plain drag keeps
    Qt's normal behavior untouched (including click-to-jump on press).
    """

    _FINE_SENSITIVITY = 0.1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fine_dragging = False
        self._drag_last_x = 0.0
        # Float accumulator, not re-derived from cursor position each move: at reduced
        # sensitivity, many consecutive sub-one-unit deltas would otherwise be lost to
        # setValue()'s integer rounding instead of eventually adding up.
        self._drag_value = 0.0

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier
        ):
            self._fine_dragging = True
            self._drag_last_x = event.position().x()
            self._drag_value = float(self.value())
            # Down, as Qt's own drag is: with tracking off the value then lands on release.
            self.setSliderDown(True)
            event.accept()
            return
        self._fine_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self._fine_dragging:
            super().mouseMoveEvent(event)
            return
        x = event.position().x()
        dx = x - self._drag_last_x
        self._drag_last_x = x
        span = self.maximum() - self.minimum()
        groove_width = max(self.width(), 1)
        self._drag_value += dx * (span / groove_width) * self._FINE_SENSITIVITY
        self._drag_value = min(self.maximum(), max(self.minimum(), self._drag_value))
        # The position, not the value: tracking decides whether the value follows now.
        self.setSliderPosition(round(self._drag_value))
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._fine_dragging:
            self._fine_dragging = False
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        # Like the number beside it: the wheel scrolls the panel or zooms the node graph. Taken
        # here, scrolling past a node's Keep % changed it, and the chunk cooked again.
        event.ignore()


def caret_step(text: str, caret: int, point: str = ".") -> tuple:
    """Which digit the caret is on, and the size of a step of that digit.

    Returns (digit_index, step). The digit is the one just left of the caret, or the one
    just right of it when what is on the left is not a digit -- the start of the field, a
    sign, or the point: "12|.50" steps the 2, "12.|50" steps the 5. The step is the digit's
    place value: 1 for the last integer digit, 10 for the one before it, 0.1 for the first
    decimal. (-1, None) when there is no digit to step, which a blank field is.

    Pure so it can be tested without a widget.
    """
    digit = -1
    if 0 < caret <= len(text) and text[caret - 1].isdigit():
        digit = caret - 1
    elif caret < len(text) and text[caret].isdigit():
        digit = caret
    else:
        # A caret past a trailing non-digit, or a text with nothing at the caret: fall
        # back to the nearest digit to its left, then to its right.
        left = next((i for i in range(min(caret, len(text)) - 1, -1, -1) if text[i].isdigit()), -1)
        right = next((i for i in range(max(caret, 0), len(text)) if text[i].isdigit()), -1)
        digit = left if left >= 0 else right
    if digit < 0:
        return -1, None
    point_at = text.find(point)
    if point_at < 0:
        point_at = len(text)
    place = (point_at - 1 - digit) if digit < point_at else (point_at - digit)
    return digit, 10.0 ** place


class NumberField(QtWidgets.QLineEdit):
    """The slider's number field: a text field that only admits a number.

    A QLineEdit, not a QAbstractSpinBox. The spin box wraps a line edit in a layer of its
    own key handling and focus proxying, and inside usdview -- which reclaims native focus
    and runs an application-wide filter that turns Left/Right into frame stepping whenever
    the focused widget is not one it recognises as text -- that layer was where the caret
    keys went missing. A line edit is the thing usdview's filter is built to leave alone,
    and it needs no proxy to reach.

    What is typed is validated as a number, not clamped: digits, one point, a leading
    sign (float), digits and a sign (int). Up/Down step the digit under the caret (see
    caret_step); PageUp/PageDown step ten of them, and each step is a change at once.

    A typed number is a change on Enter, or on leaving the field -- not keystroke by
    keystroke: typing 5000 into a chunk that takes seconds to cook cooked it for 5, then 50,
    then 500. What does not read as a number ("-", "") changes nothing, and the field goes
    back to showing the value.
    """

    valueChanged = QtCore.Signal(float)

    _FLOAT = QtCore.QRegularExpression(r"^-?\d*\.?\d*$")
    _INT = QtCore.QRegularExpression(r"^-?\d*$")

    def __init__(self, is_float: bool = True, decimals: int = 3, parent=None):
        super().__init__(parent)
        self._is_float = is_float
        self._decimals = decimals
        self._value = 0.0 if is_float else 0
        self.setValidator(QtGui.QRegularExpressionValidator(self._FLOAT if is_float else self._INT, self))
        self.editingFinished.connect(self._commit)

    # -- value ---------------------------------------------------------------------------

    def value(self):
        return self._value

    def setValue(self, value) -> None:
        """Show `value`, keeping the caret on the same place value when the text changes
        shape (9.9 -> 10.0 gains a digit on the left), so holding Up keeps stepping the
        digit that was chosen. Emits valueChanged when the value actually changed.
        """
        value = float(value) if self._is_float else int(round(value))
        offset = self.cursorPosition() - self._point_at(self.text())
        changed = value != self._value
        self._value = value
        text = self._format(value)
        if text != self.text():
            self.setText(text)
            self.setCursorPosition(max(0, min(len(text), self._point_at(text) + offset)))
        if changed:
            self.valueChanged.emit(value)

    def _format(self, value) -> str:
        if not self._is_float:
            return str(int(value))
        text = f"{value:.{self._decimals}f}"
        # Trailing zeros dropped, one decimal kept: "1.250" reads "1.25", "12.000" reads
        # "12.0" so there is always a decimal digit to put the caret after.
        if "." in text:
            text = text.rstrip("0")
            if text.endswith("."):
                text += "0"
        return text

    def _point_at(self, text: str) -> int:
        at = text.find(".")
        return at if at >= 0 else len(text)

    def _parse(self, text: str):
        try:
            return float(text) if self._is_float else int(text)
        except ValueError:
            return None

    def _commit(self) -> None:
        """On Enter or on leaving the field: take what was typed as the value, and show it
        the way setValue would."""
        value = self._parse(self.text())
        if value is not None and value != self._value:
            self._value = value
            self.valueChanged.emit(value)
        text = self._format(self._value)
        if text != self.text():
            self.setText(text)

    # -- keys ----------------------------------------------------------------------------

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        steps = {
            QtCore.Qt.Key.Key_Up: 1,
            QtCore.Qt.Key.Key_Down: -1,
            QtCore.Qt.Key.Key_PageUp: 10,
            QtCore.Qt.Key.Key_PageDown: -10,
        }.get(key)
        if steps is None:
            super().keyPressEvent(event)
            return
        self.stepBy(steps)
        event.accept()

    def stepBy(self, steps: int) -> None:
        _digit, step = caret_step(self.text(), self.cursorPosition())
        if step is None:
            return
        if not self._is_float:
            step = int(step)
        # From what is typed, not yet taken: 12 typed over a 4, then Up, is 13.
        if (typed := self._parse(self.text())) is not None:
            self._value = typed
        # Rounded to the field's own precision: 0.1 + 0.2 is 0.30000000000000004 in binary,
        # and the caret would land in a text ten digits longer than the one it was in.
        new = self._value + steps * step
        if self._is_float:
            new = round(new, self._decimals)
        self.setValue(new)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        # Same as the slider beside it: the wheel scrolls the panel, it does not edit.
        event.ignore()


class SpinSlider(QtWidgets.QWidget):
    def __init__(self, parent=None, is_float=False):
        super().__init__(parent)
        self._step = 1000 if is_float else 1

        # Three decimals, the slider's own resolution (1 / _step). At two, a value of 0.125
        # read 0.13 and the thousandths could be set but never seen.
        self.spin_box = NumberField(is_float=is_float, decimals=3)
        self.spin_box.setMaximumWidth(48)

        self.slider = _FineDragSlider(QtCore.Qt.Orientation.Horizontal)

        layout = QtWidgets.QHBoxLayout(self)
        layout.addWidget(self.slider)
        layout.addWidget(self.spin_box)
        layout.setContentsMargins(0, 0, 0, 0)

        self.slider.valueChanged.connect(self._on_slider_changed)
        self.spin_box.valueChanged.connect(self._on_spin_changed)

    def _on_slider_changed(self, value):
        with QtCore.QSignalBlocker(self.spin_box):
            self.spin_box.setValue(value / self._step)

    def _on_spin_changed(self, real_value):
        with QtCore.QSignalBlocker(self.slider):
            self.slider.setValue(int(real_value * self._step))

    def setRange(self, min_int, max_int):
        self.slider.setRange(min_int, max_int)

    def setValue(self, int_value):
        self.slider.setValue(int_value)

    def value(self):
        return self.slider.value()

    def setOrientation(self, orient):
        self.slider.setOrientation(orient)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()

class ChunkSliderWidget(ChunkParamWidgetBase[chunk.Slider], SpinSlider):
    def __init__(self, param: chunk.Slider):
        is_float = ChunkFlags.ParamType.FLOAT in param.evaluate_type()
        super().__init__(param, is_float=is_float)

        self._apply_range()

        self.setup_chunk_widget()
        self.slider.valueChanged.connect(self.value_changed)
        self.spin_box.valueChanged.connect(lambda v: self.value_changed(int(v * self._step)))
        chunk_of = param.get_chunk() if hasattr(param, "get_chunk") else None
        config = chunk_of.get_config() if chunk_of is not None else None
        if config is not None and config.cook_on_release:
            # A drag sets the value once, when it ends; the number follows it meanwhile.
            self.slider.setTracking(False)
            self.slider.sliderMoved.connect(self._on_slider_changed)

    def _apply_range(self) -> None:
        min_val = self.param.min_value
        max_val = self.param.max_value
        min_int = int(min_val * self._step) if min_val is not None else 100 * (1 - self._step)
        max_int = int(max_val * self._step) if max_val is not None else 100 * self._step
        self.setRange(min_int, max_int)

    def handle_param_changed(self, notice, sender):
        # A range that moves after the widget exists -- a Wedge row's Min/Max edited once
        # its slider is lofted -- would otherwise leave the slider spanning the old one.
        if notice.data.name in ("min_value", "max_value"):
            self._apply_range()
        super().handle_param_changed(notice, sender)

    def get_value(self):
        return self.value() / self._step

    def set_value(self, value):
        if isinstance(value, str):
            value = self.param.value
        if value is None or isinstance(value, str):
            return
        int_val = int(value * self._step)
        with QtCore.QSignalBlocker(self.slider), QtCore.QSignalBlocker(self.spin_box):
            self.slider.setValue(int_val)
            self.spin_box.setValue(int_val / self._step)

    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        is_horizontal = ChunkFlags.LayoutFlags.HORIZONTAL == layout_flags
        orient = QtCore.Qt.Orientation.Horizontal if is_horizontal else QtCore.Qt.Orientation.Vertical

        self.setOrientation(orient)

    def value_changed(self, value: int | float | None = None):
        if not self.param or value is None:
            return
        result = value / float(self._step)
        # The step division makes a float of everything, so an `is_int` param would hand back
        # e.g. 3.0 -- fine until a chunk does range() or a list index with it. The param says
        # it is an int, so it delivers one.
        if getattr(self.param, "is_int", False):
            result = int(result)
        super().value_changed(result)

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkFloatSpinboxWidget(ChunkParamWidgetBase[chunk.Spinbox], QtWidgets.QDoubleSpinBox):
    def __init__(self, param: chunk.Spinbox):
        super().__init__(param)

        self._step = 1

        min_val = param.min_value
        max_val = param.max_value

        if min_val is not None:
            self.setMinimum(min_val * self._step)
        else:
            self.setMinimum(-999999999.0)

        if max_val is not None:
            self.setMaximum(max_val * self._step)
        else:
            self.setMaximum(999999999.0)

        # A typed number is a change on Enter or on leaving the box, as in the slider's
        # field (NumberField); the arrows still change it at once.
        self.setKeyboardTracking(False)
        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
        if isinstance(value, str):
            value = self.param.value
        if value is None or isinstance(value, str):
            return
        with QtCore.QSignalBlocker(self):
            self.setValue(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkIntSpinboxWidget(ChunkParamWidgetBase[chunk.Spinbox], QtWidgets.QSpinBox):
    def __init__(self, param: chunk.Spinbox):
        super().__init__(param)

        self._step = 1

        min_val = param.min_value
        max_val = param.max_value

        if min_val is not None:
            self.setMinimum(int(min_val * self._step))
        else:
            self.setMinimum(-999999999)

        if max_val is not None:
            self.setMaximum(int(max_val * self._step))
        else:
            self.setMaximum(999999999)

        # A typed number is a change on Enter or on leaving the box, as in the slider's
        # field (NumberField); the arrows still change it at once.
        self.setKeyboardTracking(False)
        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
        if isinstance(value, str):
            value = self.param.value
        if value is None or isinstance(value, str):
            return
        with QtCore.QSignalBlocker(self):
            self.setValue(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkLabelWidget(ChunkParamWidgetBase[chunk.Label], QtWidgets.QLabel):
    def __init__(self, param: chunk.Label):
        super().__init__(param)
        self._shown = ""
        # A readout can be long -- a solver's summary, or an error naming a file -- and a row
        # is as wide as the panel, so an unwrapped label was cut off at its edge. Wrapped, and
        # never read as rich text.
        self.setWordWrap(True)
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.setup_chunk_widget()

    def get_value(self):
        return self._shown

    def setSizePolicy(self, *args):  # noqa: N802 - Qt's name
        """Keep height-for-width on whatever policy the panel gives this label.

        A container sets every param widget's policy fresh (add_widget), which drops the flag
        word wrap sets -- and without it the layout never asks how tall the wrapped text is:
        the first line hid under the button above and the last was cut off.
        """
        policy = args[0] if len(args) == 1 else QtWidgets.QSizePolicy(*args)
        policy.setHeightForWidth(self.wordWrap())
        return super().setSizePolicy(policy)

    def contextMenuEvent(self, event):  # noqa: N802 - Qt's name
        """Right-click Copy: the exact text, without the break points setText adds."""
        menu = QtWidgets.QMenu(self)
        menu.addAction("Copy", lambda: QtWidgets.QApplication.clipboard().setText(self._shown))
        menu.popup(event.globalPos())

    def setText(self, text):  # noqa: N802 - Qt's name
        """Show text, wrapping a long path too: Qt only breaks a line at whitespace, so a
        zero-width space after each separator gives it somewhere to break."""
        self._shown = "" if text is None else str(text)
        result = super().setText(self._shown.replace("\\", "\\​").replace("/", "/​"))
        self._fit_height()
        return result

    def _fit_height(self):
        """Ask for the height the wrapped text needs at this width, as a minimum height.

        Height-for-width alone was not enough: a node graph's panel sits in a graphics proxy
        whose layouts go by minimum sizes, so the row stayed one height and the text spilled
        over its neighbours. A minimum height travels up every layout to the panel.
        """
        if self.wordWrap() and self.width() > 0:
            self.setMinimumHeight(self.heightForWidth(self.width()))

    def resizeEvent(self, event):  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._fit_height()

    def set_value(self, value):
        return self.setText("" if value is None else str(value))

    def set_label(self, text):
        # A Label is one piece of text doing two jobs: its label is the placeholder, its
        # value what the chunk reports. Setup writes the value and then the label, so a
        # value set before the widget existed -- a readout written by a cook during scene
        # load -- was painted over by the placeholder and stayed that way, since nothing
        # changed afterwards to repaint it. The value wins whenever there is one.
        param = self._param()
        value = getattr(param, "raw_value", None) if param is not None else None
        return self.setText(str(value) if value not in (None, "") else text)

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkLineEditWidget(ChunkParamWidgetBase[chunk.LineEdit], QtWidgets.QWidget):
    def __init__(self, param: chunk.LineEdit):
        super().__init__(param)

        # The pin button only exists on a prim-selection field (see _sync_pin_button): built
        # for every LineEdit and hidden on most, it was a widget per field for nothing.
        self.button: QtWidgets.QToolButton | None = None

        self.text = QtWidgets.QLineEdit(self)
        self.text.editingFinished.connect(self.value_changed)
        # Ctrl+click selects the prim(s) the field names -- see eventFilter.
        self.text.installEventFilter(self)
        self.main_layout = QtWidgets.QHBoxLayout()

        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.text)
        self.setLayout(self.main_layout)

        self._sync_pin_button(self.param.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION))

        self.setup_chunk_widget()
        # This widget has no parent yet, so it is a top-level window: the set_hidden(False)
        # inside update_from_state() below would be a show(), flashing a real OS window for
        # every param a chunk builds. Suppressed here rather than in create_widget, which
        # only gets the object back after __init__ has already shown it. Cleared by
        # ContainerWidget.add_widget once the widget is parented.
        self.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
        # setup_chunk_widget() doesn't call update_from_state() and the reactive
        # ATTRIBUTE_CHANGED/state path isn't reliable for state set before this widget
        # existed (e.g. a ListGroup template's DISABLED state, already set when each
        # row's proxy widget gets created) -- apply current state explicitly, once, now.
        self.update_from_state()

    def handle_param_changed(self, notice: chunk_notice.AttributeChangeNotice, sender: chunk.ChunkParamBase):
        if notice.alias == chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED and notice.data.value:
            self._sync_pin_button(ChunkFlags.ParamFlags.PRIM_SELECTION in notice.data.value)
        return super().handle_param_changed(notice, sender)

    def _sync_pin_button(self, wanted: bool) -> None:
        """Show the pin button when the field takes a prim selection, making it on first need."""
        if self.button is None:
            if not wanted:
                return
            self.button = QtWidgets.QToolButton(self)
            self.button.setText("Pin Selection")
            self.button.setIcon(QtGui.QIcon(str(chunk_resources.get_icon("pin"))))
            self.button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            self.button.clicked.connect(self.pin_prim)
            self.main_layout.addWidget(self.button)
        self.button.setHidden(not wanted)

    def pin_prim(self):
        scene = self.param.get_scene()
        if not scene:
            return
        prims = scene.get_selected_prims()
        # Nothing selected is a misfire, not an instruction to erase: the path already in the
        # field is worth more than the empty selection.
        if not prims:
            return
        # Assigned through the param rather than calling its set_prims(). Inside a ListGroup row
        # `self.param` is a ProxyParam: attribute *assignment* is proxied to that row, but a
        # method looked up on it comes back bound to the shared template, so set_prims() would
        # write the template's default and leave the row alone.
        self.param.value = ",".join(str(p.GetPath()) for p in prims)

    SELECT_HINT = "Ctrl+click to select in the viewport"

    def scene_prim_paths(self) -> list[Sdf.Path]:
        """The prims this field names, if every entry in it is one that exists on the stage.

        Any field holding prim paths qualifies, not just PRIM_SELECTION ones: a Reference's
        "Insert to location" names a prim just as much as a Transform's target does. Anything
        that is not wholly existing prims -- a name, a glob, a path not authored yet -- gives
        nothing, so ctrl+click there stays an ordinary click.

        Read from the param, not the text box: an expression field shows `@Other.param@`
        while its value is the path it resolves to.
        """
        scene = self.param.get_scene()
        stage = scene.get_stage() if scene else None
        if not stage:
            return []
        entries = [e.strip() for e in str(self.param.value or "").split(",") if e.strip()]
        paths = []
        for entry in entries:
            if not Sdf.Path.IsValidPathString(entry):
                return []
            path = Sdf.Path(entry).StripAllVariantSelections()
            if not (path.IsAbsolutePath() and path.IsPrimPath()) or not stage.GetPrimAtPath(path):
                return []
            paths.append(path)
        return paths

    def select_in_scene(self) -> bool:
        scene = self.param.get_scene()
        paths = self.scene_prim_paths()
        return bool(scene and paths and scene.select_prims(paths))

    def eventFilter(self, watched, event):
        if watched is self.text:
            if (event.type() == QtCore.QEvent.Type.MouseButtonPress
                    and event.button() == QtCore.Qt.MouseButton.LeftButton
                    and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
                # Consumed only when something was selected, so the click does not also
                # place the text cursor; otherwise it behaves as it always did.
                if self.select_in_scene():
                    # And the field gives up the keyboard: ctrl+click is a selection
                    # gesture, not an edit, so the `w` that follows it has to reach the
                    # viewport's manipulator shortcut instead of being typed into the path.
                    self.text.clearFocus()
                    return True
            elif event.type() == QtCore.QEvent.Type.ToolTip and self.scene_prim_paths():
                # Only on fields that currently name a prim -- the hint on a name field
                # would be noise. Everything else falls through to the param's own tooltip.
                tip = f"{self.param.tooltip}\n\n{self.SELECT_HINT}" if self.param.tooltip else self.SELECT_HINT
                QtWidgets.QToolTip.showText(event.globalPos(), tip, self.text)
                return True
        return super().eventFilter(watched, event)

    def get_value(self):
        return self.text.text()

    def set_value(self, value):
        return self.text.setText(str(value))

    def set_hidden(self, val: bool):
        # __init__ applies state before add_widget has parented this, and a show then is a
        # top-level window's full show -- WA_DontShowOnScreen only keeps it off screen. That
        # was ~1.4 ms per LineEdit for nothing: the row's layout shows it once it is
        # parented. A hide is still applied, so a HIDDEN param stays hidden when added.
        if val or self.parentWidget() is not None:
            self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))

    def update_from_state(self):
        # Base ChunkParamWidgetBase.update_from_state is a no-op stub with no default
        # implementation -- without this override, DISABLED never actually renders as
        # disabled for a LineEdit (only reactively, and only if this were overridden).
        self.set_hidden(self.param.has_state(ChunkFlags.State.HIDDEN))
        self.set_enabled(not self.param.has_state(ChunkFlags.State.DISABLED))


class ChunkTextEditWidget(ChunkParamWidgetBase[chunk.TextEdit], QtWidgets.QWidget):
    def __init__(self, param: chunk.TextEdit):
        super().__init__(param)

        self.text = QtWidgets.QPlainTextEdit(parent=self)
        self.text.setSizePolicy(QtWidgets.QSizePolicy.Policy.MinimumExpanding, QtWidgets.QSizePolicy.Policy.Maximum)
        self.text.textChanged.connect(self.value_changed)
        self.text.setTabStopDistance(QtGui.QFontMetricsF(self.text.font()).horizontalAdvance(" ") * 4)
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)

        hlm = None
        self.hl = None
        if param.has_flag(ChunkFlags.ParamFlags.PY_SYNTAX_HIGHLIGHT):
            hlm = chunk_core.PySyntaxHighlightMapping()
        elif param.has_flag(ChunkFlags.ParamFlags.USD_SYNTAX_HIGHLIGHT):
            hlm = chunk_core.UsdSyntaxHighlightMapping()
        if hlm:
            self.hl = SyntaxHighlighter(self.text.document(), hlm)

        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        self.text.setFont(font)

        if param.rows is not None:
            line_h = QtGui.QFontMetrics(self.text.font()).lineSpacing()
            self.text.setFixedHeight(param.rows * line_h + 4)

        self.main_layout = QtWidgets.QHBoxLayout()

        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.text)
        self.setLayout(self.main_layout)
        self.setup_chunk_widget()

    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        self.value_changed(self.get_value())
        return super().focusOutEvent(event)

    def get_value(self):
        return self.text.document().toPlainText()

    def set_value(self, value):
        if value != self.get_value():
            with QtCore.QSignalBlocker(self.text):
                self.text.setPlainText(str(value))

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkFileWidget(ChunkParamWidgetBase[chunk.FileBrowser], QtWidgets.QWidget):
    ValueChanged = QtCore.Signal(str)

    def __init__(self, param: chunk.FileBrowser):
        super().__init__(param)

        self.button = QtWidgets.QToolButton(self)
        self.button.setText("Browse")

        self.button.setIcon(QtGui.QIcon(str(chunk_resources.get_icon("folder_open"))))

        self.button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.button.clicked.connect(self.open_file_dial)
        self.text = QtWidgets.QLineEdit(self)

        self.setup_chunk_widget()
        self.text.editingFinished.connect(self.value_changed)
        self.ValueChanged.connect(self.value_changed)

    def open_file_dial(self):
        if not self.param:
            return

        # What to ask for is chunk_widget_api.file_dialog_spec, shared with the Flet and
        # ImGui frontends so all three honour the same pattern/extensions and the same
        # DIRECTORY and FILE_EXISTING flags. Only turning it into a QFileDialog is local.
        #
        # FILE_EXISTING unset (the default) means the file need not exist yet -- an
        # output/destination picker like Export ChunkGroup's Output File. file_dial()
        # ignores create_new for directory=True anyway (always getExistingDirectory).
        spec = chunk_widget_api.file_dialog_spec(self.param)
        pattern = " ".join(spec.patterns)
        directory = spec.directory
        create_new = not spec.must_exist

        if file := file_dial(pattern, create_new, directory):
            self.set_value(file)
            self.ValueChanged.emit(file)

    def get_value(self):
        return self.text.text()

    def set_value(self, value):
        return self.text.setText(str(value))

    def set_icon(self, icon: str):
        self.button.setIcon(QtGui.QIcon(icon))

    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        if ChunkFlags.LayoutFlags.VERTICAL == layout_flags:
            self.main_layout = QtWidgets.QVBoxLayout()
        else:
            self.main_layout = QtWidgets.QHBoxLayout()

        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.text)
        self.main_layout.addWidget(self.button)

        self.setLayout(self.main_layout)

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


def _screen_pos(widget: QtWidgets.QWidget, pos: QtCore.QPoint) -> QtCore.QPoint:
    """The screen position of a point in widget, also when it is embedded in a graph.

    A widget inside a QGraphicsProxyWidget sits in a hidden window of its own, so its
    mapToGlobal() is not where it shows. Map through the proxy and the view instead.

    Args:
        widget (QtWidgets.QWidget): The widget pos is in
        pos (QtCore.QPoint): A point in widget's coordinates

    Returns:
        QtCore.QPoint: Where that point is on screen.
    """
    top = widget.window()
    proxy = top.graphicsProxyWidget()
    views = [v for v in proxy.scene().views() if v.isVisible()] if proxy and proxy.scene() else []
    if not views:
        return widget.mapToGlobal(pos)
    scene_pos = proxy.mapToScene(QtCore.QPointF(widget.mapTo(top, pos)))
    return views[0].viewport().mapToGlobal(views[0].mapFromScene(scene_pos))


class ChunkComboBoxWidget(ChunkParamWidgetBase[chunk.Menu], QtWidgets.QComboBox):
    def __init__(self, param: chunk.Menu):
        super().__init__(param)

        self.add_callback(self.menu_items_changed, param.get_attribute_sender("menu_items"))
        self.update_menu_items(param)

        self.setup_chunk_widget()
        self.currentTextChanged.connect(self.value_changed)

    def menu_items_changed(self, notice, sender):
        self.update_menu_items(self.param)

    def update_menu_items(self, param: chunk.Menu):
        menu_items = self.param.menu_items
        # clear()/addItems() make Qt auto-select (and emit currentTextChanged for) the
        # first item as the list goes from empty to populated. That signal is wired to
        # value_changed, which would write that transient first item straight into the
        # param model -- clobbering the real value -- before set_value() below even runs.
        with QtCore.QSignalBlocker(self):
            self.clear()
            if menu_items:
                self.addItems(menu_items)

        if menu_items:
            val = self.param.value if self.param.value is not None else self.param.default_value
            if val:
                self.set_value(val)

    def get_value(self):
        return self.currentText()

    def set_value(self, value):
        if isinstance(value, str) and chunk_utils.has_expression(value):
            # setup_chunk_widget()/handle_param_changed() (ChunkParamWidgetBase) pass
            # param.raw_value -- for a node-graph-connected Menu that's the literal
            # "@Node.param@" expression text, not a menu_items entry. Resolve it for
            # display; menu_items themselves are never expression strings.
            value = self.param.value
        with QtCore.QSignalBlocker(self):
            index = self.findText(value)
            if index < 0 and value:
                # A resolved connection value (e.g. a Layer chunk's Layer
                # Name) is a plain name, not one of the raw identifiers/sentinels in
                # menu_items -- setCurrentText() would silently no-op on a
                # non-editable combo box, leaving the old selection on screen. Show
                # it via a transient extra item instead; update_menu_items() clears
                # and rebuilds the list from scratch on every refresh anyway.
                index = self.count()
                self.addItem(value)
            self.setCurrentIndex(index)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # QComboBox.showPopup() places its popup wrong for a combo embedded in the node
        # graph. Show our own menu instead, placed by _screen_pos.
        QtCore.QTimer.singleShot(0, self, self._show_as_menu)

    def _show_as_menu(self) -> None:
        # A menu of what is on the stage is filled as it opens (Menu.refresh_items); the
        # menu_items notice rebuilds this combo's entries before they are read below.
        self.param.refresh_items()
        items = [self.itemText(i) for i in range(self.count())]
        current = self.currentText()
        # No parent: a popup whose parent is embedded in the node graph is embedded in the
        # graph scene too, where it gets no typing and the graph takes its wheel.
        if len(items) > _SearchableListMenu.MAX_PLAIN_ITEMS:
            # A plain QMenu taller than the screen spreads into columns wider than it.
            menu = _SearchableListMenu(items, current)
            menu.picked.connect(self.setCurrentText)
        else:
            menu = QtWidgets.QMenu()
            for text in items:
                act = menu.addAction(text)
                if text == current:
                    act.setCheckable(True)
                    act.setChecked(True)
            menu.triggered.connect(self._menu_item_chosen)
        # Use popup() (non-modal), not exec_(). exec_ runs a nested event loop in which a
        # deferred node-graph rebuild can destroy this proxied combobox mid-selection; exec_'s
        # result would then be applied to a freed C++ widget → silent crash. With popup(), the
        # menu closes when this widget is destroyed, before it can fire its signals.
        self.destroyed.connect(menu.close)
        menu.aboutToHide.connect(menu.deleteLater)
        self._popup = menu  # unparented, so Python must keep it alive while it is open
        menu.popup(_screen_pos(self, QtCore.QPoint(0, self.height())))

    def _menu_item_chosen(self, action) -> None:
        self.setCurrentText(action.text())

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkCheckBoxWidget(ChunkParamWidgetBase[chunk.Checkbox], QtWidgets.QCheckBox):
    def __init__(self, param: chunk.Checkbox):
        super().__init__(param)
        self.setup_chunk_widget()

        self.toggled.connect(self.value_changed)

    def get_value(self):
        return self.isChecked()

    def set_value(self, value: bool):
        if isinstance(value, str):
            value = self.param.value
        if value is None or isinstance(value, str):
            return
        with QtCore.QSignalBlocker(self):
            self.setChecked(value)

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkPushButtonWidget(ChunkParamWidgetBase[chunk.Button], QtWidgets.QPushButton):
    def __init__(self, param: chunk.Button):
        super().__init__(param)
        self.setup_chunk_widget()
        self.clicked.connect(self.value_changed)

    def set_label(self, text):
        self.setText(text)

    def set_icon(self, icon: str):
        qicon = QtGui.QIcon(icon)
        self.setIcon(qicon)

    def value_changed(self, value: Any = None):
        """Override for buttons as the param do not have a value. Better to just send the param notice directly"""
        if self.param:
            self.param.click()

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkSeparatorWidget(ChunkParamWidgetBase, QtWidgets.QFrame):
    def __init__(self, param: chunk.Separator):
        super().__init__(param)

        is_horizontal = usd_chunks.chunk_flags.ChunkFlags.LayoutFlags.HORIZONTAL == param.layout
        shape = QtWidgets.QFrame.Shape.HLine if is_horizontal else QtWidgets.QFrame.Shape.VLine
        self.setFrameShape(shape)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)

        self.setup_chunk_widget()

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))


class ChunkColorPickerWidget(ChunkParamWidgetBase[chunk.ColorPicker], QtWidgets.QPushButton):
    def __init__(self, param: chunk.ColorPicker):
        super().__init__(param)
        self.setFixedHeight(24)
        self.setFlat(True)
        self.setup_chunk_widget()
        self.clicked.connect(self._open_dialog)

    def _qcolor(self) -> QtGui.QColor:
        val = self.param.value
        if not val or len(val) < 3:
            return QtGui.QColor(0, 0, 0)
        r, g, b = [max(0.0, min(1.0, float(v))) for v in val[:3]]
        return QtGui.QColor.fromRgbF(r, g, b)

    def _apply_swatch(self, color: QtGui.QColor):
        self.setStyleSheet(
            f"background-color: {color.name()};"
            f"border: 1px solid #555555;"
            f"border-radius: 3px;"
        )

    def get_value(self):
        return self.param.value

    def set_value(self, value):
        if value is None:
            return
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            self._apply_swatch(QtGui.QColor.fromRgbF(
                max(0.0, min(1.0, float(value[0]))),
                max(0.0, min(1.0, float(value[1]))),
                max(0.0, min(1.0, float(value[2]))),
            ))

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val and not chunk_utils.is_wire_locked(self.param))

    def update_from_state(self):
        self.set_hidden(self.param.has_state(ChunkFlags.State.HIDDEN))
        is_output = self.param.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        self.set_enabled(not is_output and not self.param.has_state(ChunkFlags.State.DISABLED))

    def _dialog_parent(self):
        """A real top-level window to hang the colour dialog off.

        On the Params panel window() is the usdview window and everything is normal. On
        the graph this widget is embedded in a QGraphicsProxyWidget -- reparented to None
        and flagged WA_DontShowOnScreen -- so window() returns the swatch itself, and a
        dialog parented to a widget that is never shown on screen comes up blank and
        mis-sized. Fall back to whatever window is actually active.
        """
        win = self.window()
        if (
            win is not None
            and win is not self
            and not win.testAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        ):
            return win
        return QtWidgets.QApplication.activeWindow()

    def _open_dialog(self):
        color = QtWidgets.QColorDialog.getColor(
            self._qcolor(), self._dialog_parent(), "Pick Color",
            QtWidgets.QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if color.isValid():
            self._apply_swatch(color)
            self.value_changed((color.redF(), color.greenF(), color.blueF()))


class _FlowLayout(QtWidgets.QLayout):
    """Left to right, wrapping: as many tiles per row as the width holds."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items: list[QtWidgets.QLayoutItem] = []
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def clear(self):
        while self._items:
            item = self.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QtCore.QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, dry_run=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def expandingDirections(self):
        return QtCore.Qt.Orientations(QtCore.Qt.Orientation(0))

    def _layout(self, rect: QtCore.QRect, dry_run: bool) -> int:
        sp = self.spacing()
        x, y, row_h = rect.x(), rect.y(), 0
        for item in self._items:
            w, h = item.sizeHint().width(), item.sizeHint().height()
            if x + w > rect.right() and row_h:
                x, y, row_h = rect.x(), y + row_h + sp, 0
            if not dry_run:
                item.setGeometry(QtCore.QRect(x, y, w, h))
            x += w + sp
            row_h = max(row_h, h)
        return y + row_h - rect.y()


class _ImageTile(QtWidgets.QFrame):
    """One item: its picture (when it has one) over its name."""

    clicked = QtCore.Signal(str)

    def __init__(self, item: str, size: int):
        super().__init__()
        self.item = item
        self._path = ""
        self.setFixedWidth(size + 8)
        self.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self._picture = QtWidgets.QLabel()
        self._picture.setFixedSize(size, size)
        self._picture.setScaledContents(True)
        self._picture.setStyleSheet("background:#333;")
        layout.addWidget(self._picture)
        name = QtWidgets.QLabel(item)
        name.setWordWrap(True)
        name.setMaximumWidth(size + 4)
        name.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(name)
        self.set_selected(False)

    def set_image(self, path: str) -> None:
        if path == self._path:
            return
        pixmap = QtGui.QPixmap(path) if path else QtGui.QPixmap()
        if not pixmap.isNull():
            self._path = path
            self._picture.setPixmap(pixmap)

    def set_selected(self, selected: bool) -> None:
        color = "#4a90d9" if selected else "#2a2a2a"
        self.setStyleSheet(f"QFrame {{ border: 2px solid {color}; border-radius: 3px; }}")

    def mousePressEvent(self, event):
        self.clicked.emit(self.item)
        super().mousePressEvent(event)


class ChunkImageMenuWidget(ChunkParamWidgetBase[chunk.ImageMenu], QtWidgets.QWidget):
    """An ImageMenu as a searchable grid of tiles.

    Tiles are built a page at a time as the grid scrolls: a catalog of hundreds of items
    would otherwise build every tile, for every node showing one, on every scene open. The
    current value is pinned to the front of the first page, so it is visible without
    scrolling to wherever it sits in the list.
    """

    PAGE_SIZE = 40

    def __init__(self, param: chunk.ImageMenu):
        super().__init__(param)
        self._tiles: dict[str, _ImageTile] = {}
        self._pending: list[str] = []

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search...")
        outer.addWidget(self._search)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        container = QtWidgets.QWidget()
        self._flow = _FlowLayout(container, spacing=4)
        self._scroll.setWidget(container)
        outer.addWidget(self._scroll)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._rebuild)
        self._search.textChanged.connect(self._search_timer.start)

        self.add_callback(self._items_changed, param.get_attribute_sender("menu_items"))
        self.add_callback(self._images_changed, param.get_attribute_sender("item_images"))
        self.setFixedHeight(360)
        self.setup_chunk_widget()
        self._rebuild()

    # ChunkParamWidgetBase

    def get_value(self):
        return self.param.value

    def set_value(self, value):
        if isinstance(value, str) and chunk_utils.has_expression(value):
            value = self.param.value
        for item, tile in self._tiles.items():
            tile.set_selected(item == value)

    def set_label(self, _text):
        pass

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val)

    def showEvent(self, event):
        super().showEvent(event)
        # A menu of runtime data is filled as it is about to be looked at (Menu.refresh_items).
        self.param.refresh_items()

    # Private

    def _items_changed(self, _notice, _sender):
        self._rebuild()

    def _images_changed(self, _notice, _sender):
        images = self.param.item_images or {}
        for item, tile in self._tiles.items():
            tile.set_image(images.get(item, ""))

    def _rebuild(self):
        term = self._search.text().lower()
        items = [i for i in (self.param.menu_items or []) if i and term in i.lower()]
        current = self.param.value
        if current in items:
            items.remove(current)
            items.insert(0, current)
        self._flow.clear()
        self._tiles.clear()
        self._pending = items
        self._load_next_page()

    def _load_next_page(self):
        page, self._pending = self._pending[: self.PAGE_SIZE], self._pending[self.PAGE_SIZE:]
        images = self.param.item_images or {}
        size = self.param.tile_size or 96
        for item in page:
            tile = _ImageTile(item, size)
            tile.set_image(images.get(item, ""))
            tile.set_selected(item == self.param.value)
            tile.clicked.connect(self._on_clicked)
            self._tiles[item] = tile
            self._flow.addWidget(tile)

    def _on_scroll(self, value: int):
        if self._pending and value >= self._scroll.verticalScrollBar().maximum() - 50:
            self._load_next_page()

    def _on_clicked(self, item: str):
        self.set_value(item)
        self.value_changed(item)


class ChunkQTWidgetFactory(chunk_widget_api.ChunkWidgetFactoryBase):
    widget_map = {
        ChunkFlags.ParamType.FLOAT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.INT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.FLOAT_SPINBOX: ChunkFloatSpinboxWidget,
        ChunkFlags.ParamType.INT_SPINBOX: ChunkIntSpinboxWidget,
        ChunkFlags.ParamType.LABEL: ChunkLabelWidget,
        ChunkFlags.ParamType.LINEEDIT: ChunkLineEditWidget,
        ChunkFlags.ParamType.TEXTEDIT: ChunkTextEditWidget,
        ChunkFlags.ParamType.FILE: ChunkFileWidget,
        ChunkFlags.ParamType.STRING_MENU: ChunkComboBoxWidget,
        ChunkFlags.ParamType.CHECKBOX: ChunkCheckBoxWidget,
        ChunkFlags.ParamType.BUTTON: ChunkPushButtonWidget,
        ChunkFlags.ParamType.SEPARATOR: ChunkSeparatorWidget,
        ChunkFlags.ParamType.COLOR_PICKER: ChunkColorPickerWidget,
    }

    def create_widget(self, param: chunk.ChunkParamBase) -> ChunkParamWidgetBase | None:
        """Create a Qt widget based on the ChunkParam object. The param.get_type() should determine the type of Qt Widget to create

        Args:
            param (chunk.ChunkParamBase): The parameter. The widget will update param.value when widget value is changed

        Returns:
            QtWidgets.QWidget: The QtWidget created
            None: If failed to create a widget
        """
        key = param.evaluate_type()
        # An ImageMenu is a Menu by type, so frontends without a grid show a dropdown;
        # this one has a grid.
        if isinstance(param, chunk.ImageMenu):
            widget_class = ChunkImageMenuWidget
        else:
            widget_class = self.get_map().get(key, None)

        if not widget_class:
            logger.warning("Could not create widget for param %s: missing mapping in Qt factory", param.name)
            return None

        widget: ChunkParamWidgetBase = widget_class(param)

        # A widget with no parent is a top-level window, so anything that makes it visible
        # before it reaches a layout shows a real window on screen -- which is what a chunk
        # built at runtime does, one flash per param. Suppressed here and cleared in
        # ContainerWidget.add_widget once the LabeledWidget has parented it, the same
        # guard the node graph and delete_self already use.
        if isinstance(widget, QtWidgets.QWidget):
            widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)

        if param.tooltip and isinstance(widget, QtWidgets.QWidget):
            widget.setToolTip(param.tooltip)

        return widget


class LabeledWidget(QtWidgets.QWidget):
    def __init__(self, widget, parent=None):
        super().__init__(parent)

        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
        self.vlayout = QtWidgets.QVBoxLayout()
        self.vlayout.setContentsMargins(0, 0, 0, 0)
        self.vlayout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.widget: ChunkParamWidgetBase = widget
        # Made by update_label when the row first gets text (see _ensure_label). Most rows
        # never do, and an eager label was a hidden widget per row, constructed, styled and
        # laid out for nothing. Filters others ask for wait in _label_filters; this row's own
        # is installed by _ensure_label and never listed -- a row holding itself would be a
        # reference cycle, keeping its widget's Python side, and so that widget's notice
        # callbacks, alive after Qt has deleted it.
        self.label: QtWidgets.QLabel | None = None
        self._label_filters: list[QtCore.QObject] = []

        self.vlayout.addWidget(widget)
        self.setLayout(self.vlayout)

        # Built on first hover, not here. A Qt.ToolTip label is a top-level window, and
        # asking for one costs ~0.2ms even offscreen -- trivial per row, but a graph of
        # nodes builds one LabeledWidget per param, so a 49-node network paid for ~1300
        # tooltip windows at load, nearly all of which are never hovered.
        self._value_tooltip: QtWidgets.QLabel | None = None
        self._tooltip_timer: QtCore.QTimer | None = None
        self._show_timer: QtCore.QTimer | None = None

        if isinstance(self.widget, ChunkParamWidgetBase):
            self._listener = chunk_notice.NoticeConnection(
                self.update_value, alias=self.widget.get_notice_alias(), sender=self.widget
            )

        # Hiding a param only hides its inner widget (see ChunkParamWidgetBase.set_hidden /
        # ChunkContainerWidgetBase.set_hidden) -- this row (label + widget) must react to the
        # same state directly, driven by the model's own state, not Qt's Hide/Show events:
        # a hidden child that un-hides while its parent (this row) is still hidden never
        # actually becomes visible on screen, so Qt never fires a QEvent.Show for it,
        # which would leave the row stuck hidden forever.
        self._model = getattr(widget, 'param', None) or getattr(widget, 'group', None)
        if self._model is not None:
            self._state_listener = chunk_notice.NoticeConnection(
                self._sync_hidden,
                alias=chunk_notice.ChunkNoticeAlias.STATE_CHANGED,
                sender=self._model.get_attribute_sender("state"),
            )
            self._sync_hidden()

        self._name_hint = ""
        self._label_hovered = False
        # A param built holding an expression is marked from the start, not only once it
        # next changes.
        self._sync_expression_mark()

        self.setMouseTracking(True)

    def _sync_hidden(self, notice=None, sender=None):
        if self._model is None:
            return
        hidden = self._model.has_state(ChunkFlags.State.HIDDEN)
        # QWidget::setVisible only early-returns once WA_WState_ExplicitShowHide is set,
        # which it is not on a widget nobody has shown or hidden yet -- so the call this
        # makes at construction does the full attribute-and-event work to arrive at the
        # state the widget was already in. Every one of these rows starts visible, so on
        # a load that was one wasted call per param.
        if hidden != self.isHidden():
            self.setHidden(hidden)

    def should_show_tooltip(self):
        if not isinstance(self.widget, ChunkParamWidgetBase):
            return False
        if isinstance(self.widget, ChunkSliderWidget):
            return False

        t = self.widget.param.evaluate_type()
        if (ChunkFlags.ParamType.EMPTY | ChunkFlags.ParamType.STRING) & t != 0:
            return False
        return True

    def tooltip_pos(self):
        return QtGui.QCursor.pos() + QtCore.QPoint(16, 8)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is self.label:
            if event.type() == QtCore.QEvent.Type.Enter:
                self._label_hovered = True
                if self._name_hint:
                    self._hover_timer().start(600)
            elif event.type() == QtCore.QEvent.Type.Leave:
                self._label_hovered = False
                if self._show_timer is not None:
                    self._show_timer.stop()
                self.hide_tooltip()
        return False

    def show_tooltip(self):
        if self._label_hovered and self._name_hint:
            text = self._name_hint
        elif self.should_show_tooltip():
            text = str(self.widget.param.value)
        else:
            return
        self._ensure_tooltip()
        self._value_tooltip.setText(text)
        self._value_tooltip.adjustSize()
        self._value_tooltip.move(self.tooltip_pos())
        self._value_tooltip.setHidden(False)
        self._tooltip_timer.start(3000)

    def _ensure_tooltip(self):
        """Create the tooltip window and its hide timer, once, on first use."""
        if self._value_tooltip is not None:
            return
        self._value_tooltip = QtWidgets.QLabel(parent=self)
        self._value_tooltip.setHidden(True)
        self._value_tooltip.setWindowFlags(QtCore.Qt.WindowType.ToolTip)
        self._tooltip_timer = QtCore.QTimer(self)
        self._tooltip_timer.timeout.connect(self.hide_tooltip)

    def _hover_timer(self) -> QtCore.QTimer:
        """The hover-delay timer, created on the first hover this row ever gets."""
        if self._show_timer is None:
            self._show_timer = QtCore.QTimer(self)
            self._show_timer.setSingleShot(True)
            self._show_timer.timeout.connect(self.show_tooltip)
        return self._show_timer

    def hide_tooltip(self):
        if self._value_tooltip is not None:
            self._value_tooltip.hide()

    def update_value(self, notice: chunk_notice.ChunkNotice, sender):
        name = notice.data.name
        if name in ["label", "name"]:
            self.update_label(notice.data.value)
        elif name == "value":
            self._sync_expression_mark()
            self._sync_override_mark()
            if self.should_show_tooltip():
                self.show_tooltip()

    def add_label_filter(self, obj: QtCore.QObject) -> None:
        """Install obj as an event filter on the label, now or once there is one."""
        self._label_filters.append(obj)
        if self.label is not None:
            self.label.installEventFilter(obj)

    def _ensure_label(self) -> QtWidgets.QLabel:
        """The row's label, made on first need above the widget, where it always sat."""
        if self.label is None:
            self.label = QtWidgets.QLabel()
            self.vlayout.insertWidget(0, self.label)
            self.label.installEventFilter(self)
            for obj in self._label_filters:
                self.label.installEventFilter(obj)
        return self.label

    def update_label(self, text):
        if text or self.label is not None:
            label = self._ensure_label()
            label.setHidden(not bool(text))
            label.setText(text)
        self._sync_expression_mark()
        self._sync_override_mark()

    def _sync_override_mark(self):
        """Show on the label that this param's value overrides one composed in from a file
        (chunk_stage.param_override_source): a sequence's chunk nudged in the shot. The
        question every shot artist asks of a referenced network is "what did we change
        here", and the field itself cannot say -- the value it shows is just a value.
        """
        # Marks only the label. A row without one shows nothing to mark, and update_label
        # runs this again when it gets one.
        if not isinstance(self.widget, ChunkParamWidgetBase) or self.label is None:
            return
        try:
            source = chunk_stage.param_override_source(self.widget.param)
            # The other way round, and the one that matters more: a stronger layer holds
            # this value (chunk_stage.param_held_by), so an edit here is kept but does not
            # show -- the field springs back, and without this nothing says why.
            holder = chunk_stage.param_held_by(self.widget.param)
            # An edit would land in a locked file (read-only, or edit permission off), so it
            # is refused -- said up front rather than discovered by the field springing back.
            lock = chunk_stage.param_locked_by(self.widget.param)
        except ReferenceError:
            return
        overridden = source is not None and holder is None
        held = holder is not None
        locked = lock is not None
        if (self.label.property("chunkOverride") != overridden
                or self.label.property("chunkHeld") != held
                or self.label.property("chunkLocked") != locked):
            self.label.setProperty("chunkOverride", overridden)
            self.label.setProperty("chunkHeld", held)
            self.label.setProperty("chunkLocked", locked)
            self.label.style().unpolish(self.label)
            self.label.style().polish(self.label)
        if self.label.property("chunkExpression"):
            return
        if locked:
            self.label.setToolTip(
                f"Locked: {lock.GetDisplayName() or lock.identifier} can't be edited. Set the "
                "edit target to a layer you can edit to override it there"
            )
        elif held:
            self.label.setToolTip(
                f"Overridden by {chunk_stage.layer_label(holder)}: an edit here is kept "
                "beneath it and shows once it goes"
            )
        elif overridden:
            self.label.setToolTip(f"Overrides {source.GetDisplayName() or source.identifier}")

    def _sync_expression_mark(self):
        """Show on the label that this param's value comes from an expression.

        Otherwise a field driven by `@Node.param@` or `${SHOT}` is indistinguishable from
        one somebody typed a number into: the widget shows the resolved value, because that
        is what the value IS, and the text behind it is nowhere on screen.
        """
        if not isinstance(self.widget, ChunkParamWidgetBase):
            return
        try:
            raw = self.widget.param.raw_value
        except ReferenceError:
            return
        # Either kind of driven value: a @Node.param@ wire, or a ${VAR} in an asset path
        # that USD substitutes during composition. Both show a value nobody typed here.
        expr = chunk_utils.is_expression_text(raw) or chunk_utils.has_stage_expression(raw)
        # The mark is on the label; a row without one has nothing to mark until update_label
        # makes it and runs this again. The wire lock below applies either way.
        if self.label is not None:
            if self.label.property("chunkExpression") != expr:
                self.label.setProperty("chunkExpression", expr)
                # A property the stylesheet selects on only takes effect after a re-polish.
                self.label.style().unpolish(self.label)
                self.label.style().polish(self.label)
            self.label.setToolTip(str(raw) if expr else "")
        # A wire locks the field (chunk_utils.is_wire_locked); set_enabled folds that in, so
        # re-applying the state locks it when a wire lands and frees it when one is pulled.
        locked = chunk_utils.is_wire_locked(self.widget.param)
        if getattr(self, "_wire_locked", False) != locked:
            self._wire_locked = locked
            self.widget.update_from_state()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.should_show_tooltip():
            event.accept()
        return super().mouseMoveEvent(event)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        if self.should_show_tooltip() and not self._label_hovered:
            self._hover_timer().start(600)
            event.accept()
        return super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        if self._show_timer is not None:
            self._show_timer.stop()
        self.hide_tooltip()
        return super().leaveEvent(event)

    @staticmethod
    def _add_clear_param(menu: QtWidgets.QMenu, model) -> QtGui.QAction | None:
        """Add a Clear Param item, greyed out when no layer sets the param.

        Args:
            menu: The menu to add the item to.
            model: The param or vector the row shows.

        Returns:
            The new action, or None when the row has no value to clear (a button, a label,
            an output) or when its edits would land in a locked file.
        """
        t = model.evaluate_type()
        # EMPTY covers buttons and separators. `in`, not `&`: the low bits are reused per
        # type, so a label and a slider share one.
        if ChunkFlags.ParamType.EMPTY in t or ChunkFlags.ParamType.LABEL in t:
            return None
        if isinstance(model, chunk_core.ChunkVectorBase):
            is_output = model.is_output
        else:
            is_output = model.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        if is_output or model.default_value is None or chunk_stage.param_locked_by(model) is not None:
            return None
        action = menu.addAction("Clear Param")
        action.setEnabled(chunk_stage.can_clear_param(model))
        return action

    @staticmethod
    def _clear_param(model) -> None:
        """Clear model in the layer its edits go to, and say so when other layers still set it.

        Where the cleared layer is the strongest, the user is offered to write the default
        there, over the others.

        Args:
            model: The param or vector to clear.
        """
        cleared = chunk_stage.clear_param(model)
        if not cleared.others:
            return
        names = ", ".join(chunk_stage.layer_label(layer) for layer in cleared.others)
        msg = f"{model.get_label()} is still set in {names}, so it shows {_value_text(model.value)}."
        if not cleared.default_can_win:
            QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Information, "Clear Param", msg).exec()
            return
        here = chunk_stage.layer_label(cleared.layer)
        detail = f"Set it to its default, {_value_text(model.default_value)}, in {here}?"
        if confirm_dial(msg, title="Clear Param", detail=detail):
            chunk_stage.set_param_default_in(model, cleared.layer)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        if not isinstance(self.widget, ChunkParamWidgetBase):
            # A vector (Translate, Scale) is a group of fields, so it gets just the one
            # item, from its header; a field inside it has its own menu.
            vector = self._model if isinstance(self._model, chunk_core.ChunkVectorBase) else None
            menu = QtWidgets.QMenu()
            clear_action = self._add_clear_param(menu, vector) if vector is not None else None
            if clear_action is None:
                event.ignore()
                return
            if menu.exec_(event.globalPos()) == clear_action:
                self._clear_param(vector)
            return

        param = self.widget.param
        chunk_obj = param.get_chunk()
        context = chunk_obj.get_context() if chunk_obj else None
        # If the param sits directly on a group, get_context() returns that group itself, so
        # "Loft up" would loft to self. Go one level up to the parent context instead, so a
        # param on a nested group lofts out to its enclosing group.
        if chunk_obj is not None and context is chunk_obj:
            pc = getattr(chunk_obj, "_parent_context", None)
            context = pc() if pc else None

        t = param.evaluate_type()
        is_expr_capable = (
            ChunkFlags.ParamType.FLOAT in t
            or ChunkFlags.ParamType.INT in t
            or ChunkFlags.ParamType.BOOL in t
        )
        raw = param.raw_value
        # is_expression_text, not has_expression: a ${VAR} stage expression is just as much
        # an expression to clear or edit, so the menu must not offer "Set Expression..." on
        # a field that already has one.
        has_expr = is_expr_capable and chunk_utils.is_expression_text(raw)

        menu = QtWidgets.QMenu()
        value_rect = QtCore.QRect(self.widget.pos(), self.widget.size())
        # A button has no label to right-click, so a click anywhere on it counts.
        on_label = not value_rect.contains(event.pos()) or ChunkFlags.ParamType.BUTTON in t
        # Lofted already: the menu takes it back down instead of lofting a second copy.
        lofted_to = chunk_stage.lofted_copy(param, context) if context and on_label else None
        loft_action = menu.addAction("Loft up") if context and on_label and lofted_to is None else None
        # Lofted params only, via the _lofted marker (set once by loft_param_to_context /
        # _restore_lofted_params, alongside the generated default label itself) -- not
        # every param generally. Lofting gives a param a qualified default label
        # (Reference1_primpath_location -> "Reference1: Prim Path") precisely so several
        # same-named lofts stay tellable apart, but that default has to be just a default,
        # not the only option, or "rename the lofted param" would not mean anything.
        rename_action = menu.addAction("Rename…") if on_label and getattr(param, "_lofted", False) else None
        unloft_target = param if getattr(param, "_lofted", False) else lofted_to
        unloft_action = menu.addAction("Unloft") if on_label and unloft_target is not None else None
        clear_action = self._add_clear_param(menu, param)
        override_source = chunk_stage.param_override_source(param)
        revert_action = None
        if override_source is not None:
            revert_action = menu.addAction(
                f"Revert to {override_source.GetDisplayName() or override_source.identifier}"
            )
        expr_action = None
        clear_expr_action = None

        if is_expr_capable:
            if has_expr:
                clear_expr_action = menu.addAction("Clear Expression")
            else:
                expr_action = menu.addAction("Set Expression...")

        if not menu.actions():
            event.ignore()
            return

        if action := menu.exec_(event.globalPos()):
            if loft_action and action == loft_action:
                chunk_stage.loft_param_to_context(param, context)
            elif unloft_action and action == unloft_action:
                # Next tick: unlofting deletes this widget, which is still inside its own event.
                QtCore.QTimer.singleShot(0, lambda: chunk_stage.unloft_param(unloft_target))
            elif clear_action and action == clear_action:
                self._clear_param(param)
            elif revert_action and action == revert_action:
                chunk_stage.revert_param_override(param)
            elif rename_action and action == rename_action:
                old = param.get_label()
                # See loft_param_to_context's Rename action: same reasoning, param scope.
                parent = QtWidgets.QApplication.activeWindow() or self
                new, ok = QtWidgets.QInputDialog.getText(parent, "Rename Param", "Label:", text=old)
                new = new.strip() if new else ""
                if ok and new and new != old:
                    param.label = new
            elif expr_action and action == expr_action:
                current = str(raw) if isinstance(raw, str) else ""
                # Parent the dialog to a real top-level window, not self: in the node graph this
                # widget is embedded in a QGraphicsProxyWidget, which can't host a modal dialog
                # (the popup never shows). activeWindow() is the usdview main window here.
                parent = QtWidgets.QApplication.activeWindow() or self
                text, ok = QtWidgets.QInputDialog.getText(
                    parent, "Set Expression", "Expression:", QtWidgets.QLineEdit.EchoMode.Normal, current
                )
                if ok and text:
                    param.value = text
            elif clear_expr_action and action == clear_expr_action:
                param.value = param.default_value


class ContainerWidget(
    Generic[chunk_core.GroupT], chunk_core.ChunkContainerWidgetBase[chunk_core.GroupT], QtWidgets.QFrame
):
    """Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    group_height_changed = QtCore.Signal(float)

    # A folded group builds its contents on first expand: set_expanded builds before it
    # measures the height it animates to.
    defer_collapsed_children = True

    def __init__(
        self,
        group,
        parent=None,
    ):
        self.animation_duration = 25
        self._animation_value = 0
        self._anim_target_h: int | None = None  # content height, measured per animation
        self._depth = 0
        super().__init__(group, parent=parent)
        # When Qt destroys this widget -- e.g. the node graph rebuild calls deleteLater on the
        # embedded ChunkWidget without going through delete_self -- revoke our notice
        # subscriptions. Otherwise a later notice (a rename firing _name_changed → update_label,
        # a state change, …) invokes a method on the deleted C++ object and crashes. Safe post-
        # delete: _disconnect_callbacks only touches Python NoticeConnections.
        self.destroyed.connect(lambda *_: self._disconnect_callbacks())

    def _is_context_tab(self) -> bool:
        """True when this widget is the scrollable tab context (not a compact node)."""
        if not self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            return False
        if getattr(self, '_is_tab_context_widget', False):
            return True  # explicitly opened as a tab -- always use scroll area
        cfg = getattr(self.group, 'get_config', lambda: None)()
        return not (cfg and getattr(cfg, 'module', None))

    def build_collapsable(self):
        if self._is_context_tab():
            # Plain tab contexts are scrollable containers for all child chunks.
            self.content = QtWidgets.QScrollArea(self)
            self.content_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom)
            self.content_layout.setContentsMargins(2, 2, 2, 10)
            self.content_layout.setSpacing(20)
            self.content_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            wid = QtWidgets.QWidget(self)
            wid.setLayout(self.content_layout)
            self.content.setWidget(wid)
            self.content.setWidgetResizable(True)

        else:
            self.content = QtWidgets.QFrame(self)
            self.content_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom)
            self.content_layout.setContentsMargins(2, 2, 2, 10)
            self.content_layout.setSpacing(1)
            self.content.setLayout(self.content_layout)

        # Setup minimize/maximize button
        self.minimize_button = QtWidgets.QToolButton()
        self.minimize_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.minimize_button.setIconSize(QtCore.QSize(6, 6))
        self.minimize_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)
        self.minimize_button.installEventFilter(self)

        # Only a ListGroup has one, so it is made when update_from_layout finds the flag. Built
        # here for every group, it was a hidden button in nearly every container of a body,
        # each still constructed, styled and laid out.
        self.list_expand_button: QtWidgets.QToolButton | None = None

        # Used when collapsable
        self.toggle_anim = QtCore.QPropertyAnimation(self, b"animation_value")
        self.toggle_anim.setStartValue(0)
        self.toggle_anim.setEndValue(self.animation_duration)
        # Kept: without it the animation runs at QPropertyAnimation's 250ms default and
        # animation_duration is only the value range the height scales against, so setting
        # that number has never changed how long a group takes to open.
        self.toggle_anim.setDuration(self.animation_duration)

        self.toggle_anim_curve = QtCore.QEasingCurve(QtCore.QEasingCurve.Type.OutExpo)
        self.toggle_anim.setEasingCurve(self.toggle_anim_curve)

        self.__layout__ = QtWidgets.QVBoxLayout()
        self.__layout__.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        self.__layout__.setContentsMargins(2, 1, 2, 1)
        self.__layout__.setSpacing(0)

        self.__layout__.addWidget(self.minimize_button)
        self.__layout__.addWidget(self.content)

        self.setLayout(self.__layout__)
        self._update_group_depth()

    _GROUP_COLORS = ("#303030", "#333333")

    def _update_group_depth(self):
        depth = len(self.group.get_identifier().split("."))
        bg = self._GROUP_COLORS[depth % len(self._GROUP_COLORS)]
        # Two setStyleSheet calls per container, each re-polishing its whole subtree --
        # collectively the single most expensive thing in building a chunk. The colour only
        # depends on nesting depth, so re-applying the same one repaints for nothing.
        if bg == getattr(self, "_group_bg", None):
            return
        self._group_bg = bg

        self.minimize_button.setStyleSheet(f"QToolButton {{ background-color: {bg}; border-radius: 4px; }}")
        self.content.setStyleSheet(
            f"QFrame {{ background-color: {bg};}}border-bottom-left-radius: 4px; border-bottom-right-radius: 4px;"
            f"QScrollArea {{ background-color: {bg};}}border-bottom-left-radius: 4px; border-bottom-right-radius: 4px;"
        )

    def supports_widget(self, widget: Any) -> bool:
        return isinstance(widget, QtWidgets.QWidget)

    @QtCore.Property(float)
    def animation_value(self):  # type: ignore
        return self._animation_value

    @animation_value.setter
    def animation_value(self, val):
        self._animation_value = val
        scale = max(self._animation_value / self.animation_duration, 0)

        # The height is fixed for the duration of a collapse or expand -- the content is
        # not changing, only how much of it shows. Recomputing sizeHint() on every one of
        # ~18 frames re-measures the whole child layout each time, which is what makes
        # opening a section with many params feel slow. Measured once at animation start
        # by set_expanded; falls back to measuring for any other driver of this property.
        h = self._anim_target_h
        if h is None:
            h = self.content_layout.sizeHint().height()

        self.content.setFixedHeight(int(h * scale))

    def animation_finished(self):
        """Modify the layout sizeConstraint when collapsed. Otherwise the child groups of collapsed ContainerWidgets will scale the widget when addWidget is called on the cild"""
        # Drop the measurement so a later animation re-measures content that may have
        # changed size in the meantime (params added, a nested group expanded).
        self._anim_target_h = None
        if self.is_collapsed():
            self.content_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        else:
            self.content_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)

    def get_widget_factory(self) -> chunk_widget_api.ChunkWidgetFactoryBase:
        if factory := self.group.widget_factory:
            return factory
        else:
            return ChunkQTWidgetFactory()

    def update_from_layout(self):
        if ChunkFlags.LayoutFlags.HORIZONTAL == self.group.layout:
            self.content_layout.setDirection(QtWidgets.QBoxLayout.Direction.LeftToRight)
        elif ChunkFlags.LayoutFlags.VERTICAL == self.group.layout:
            self.content_layout.setDirection(QtWidgets.QBoxLayout.Direction.TopToBottom)
        return super().update_from_layout()

    def update_from_flags(self):
        if self.group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE):
            # Enables animatiable collapsing of the group
            self.minimize_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
            self.minimize_button.setCheckable(True)
            self.toggle_anim.finished.connect(self.animation_finished)
            self.minimize_button.toggled.connect(self.set_expanded)
        else:
            # The layout should update min and max size. When Collapsable, this is handled in animation_finished, as collapsed groups should set NoConstraint
            self.content_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)

        if usd_chunks.chunk_flags.ChunkFlags.GroupFlags.LABELED in self.group.flags:
            self.minimize_button.setText(self.group.get_label())
            self.minimize_button.setHidden(False)
        else:
            self.minimize_button.setText("")
            if not self.group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE):
                self.minimize_button.setHidden(True)

        if usd_chunks.chunk_flags.ChunkFlags.GroupFlags.FRAMED in self.group.flags:
            self.content.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
            self.content.setLineWidth(2)
        else:
            self.content.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        if (usd_chunks.chunk_flags.ChunkFlags.GroupFlags.LIST in self.group.flags
                and self.list_expand_button is None):
            self.list_expand_button = QtWidgets.QToolButton()
            self.list_expand_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.list_expand_button.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum
            )
            icon = QtGui.QIcon(str(chunk_resources.get_icon("add")))
            self.list_expand_button.setIcon(icon)
            self.list_expand_button.setIconSize(QtCore.QSize(20, 20))
            self.list_expand_button.clicked.connect(self.list_expand)
            # Where it always sat: between the header button and the content.
            self.__layout__.insertWidget(self.__layout__.indexOf(self.minimize_button) + 1,
                                         self.list_expand_button)

    def update_label(self, text: str):
        if usd_chunks.chunk_flags.ChunkFlags.GroupFlags.LABELED in self.group.flags:
            self.minimize_button.setText(text)

    def list_expand(self):
        if not isinstance(self.group, chunk_core.ChunkListGroupBase):
            return

        self.group.size += 1

    def set_hidden(self, val: bool):
        # See LabeledWidget._sync_hidden: asking for the state it is already in is not free
        # during construction, and update_from_state asks for it once per container.
        if val != self.isHidden():
            self.setHidden(val)

    def set_enabled(self, val: bool):
        self.content.setEnabled(val)

    def set_selected(self, val: bool):
        pass

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        event.accept()

    def update_from_state(self):
        super().update_from_state()

        if self.group.has_flag(ChunkFlags.GroupFlags.COLLAPSABLE):
            self.minimize_button.setChecked(not self.group.has_state(ChunkFlags.State.COLLAPSED))

            if self.is_collapsed():
                self.content.setFixedHeight(0)

    def set_expanded(self, checked):
        arrow_type = QtCore.Qt.ArrowType.DownArrow if checked else QtCore.Qt.ArrowType.RightArrow
        direction = (
            QtCore.QAbstractAnimation.Direction.Forward if checked else QtCore.QAbstractAnimation.Direction.Backward
        )
        # Folding a section open is a UI preference and does not dirty the chunk: COLLAPSED
        # is a presentation bit, so the state change stops at the ownership chain instead of
        # being propagated as a cook (AttributeSender.is_presentation_change).
        if checked:
            # Before the measurement below: a group folded since it was built has no
            # contents yet, and would open onto a height of nothing.
            self.ensure_children_built()
            self.group.unset_state(ChunkFlags.State.COLLAPSED)
        else:
            self.group.set_state(ChunkFlags.State.COLLAPSED)

        self.minimize_button.setArrowType(arrow_type)
        # Measure the content once, here, rather than on every animation frame.
        self._anim_target_h = self.content_layout.sizeHint().height()
        self.toggle_anim.setDirection(direction)
        self.toggle_anim.start()

        if hasattr(self, "_group_bg"):
            bg = self._group_bg
            if checked:
                sheet = (
                    f"QToolButton {{ background-color: {bg}; "
                    f"border-top-left-radius: 4px; border-top-right-radius: 4px; "
                    f"border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; }}"
                )
            else:
                sheet = f"QToolButton {{ background-color: {bg}; border-radius: 4px; }}"

            # Groups that start collapsed reach this during construction, and re-applying
            # the sheet the button already has re-parses and re-polishes for nothing.
            if sheet != getattr(self, "_button_sheet", None):
                self._button_sheet = sheet
                self.minimize_button.setStyleSheet(sheet)

    def delete_self(self):
        try:
            self.deleteLater()
            # A visible widget reparented to no parent becomes a real top-level window and
            # Qt shows it immediately, before deleteLater() actually runs -- set this first
            # or a still-visible widget flashes a native window on its way out.
            self.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
            self.setParent(None)
        except RuntimeError:
            # C++ object already destroyed (e.g. by QGraphicsProxyWidget teardown)
            pass

    def remove_widget(self, widget: QtWidgets.QWidget):
        self._remove_row(self.content_layout, widget)

    def _remove_row(self, layout: QtWidgets.QLayout, widget: QtWidgets.QWidget) -> bool:
        """Remove and delete the row in layout that wraps widget.

        Args:
            layout: The layout holding the rows.
            widget: The param or group widget the row wraps.

        Returns:
            True when the row was found.
        """
        try:
            count = layout.count()
            for i in reversed(range(count)):
                item = layout.itemAt(i)
                if not item:
                    continue
                iw = item.widget()

                if isinstance(iw, LabeledWidget) and iw.widget == widget:
                    layout.removeItem(item)
                    # Tear the wrapper down, not just its layout entry. Otherwise the
                    # LabeledWidget stays a child of this container, keeping a strong ref
                    # to the param widget -> chunk, so the chunk never GCs and its layer's
                    # weakref.finalize (which removes the layer) never fires.
                    # removeItem() only drops layout management -- iw is still a visible
                    # child of self, so reparenting it to none would flash a real top-level
                    # window unless suppressed first.
                    iw.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
                    iw.setParent(None)
                    iw.deleteLater()
                    return True
        except RuntimeError:
            # This container's own C++ layout/widget was already torn down (e.g. the whole
            # ChunkWidget was deleted via _group_deleted while a sibling's PARAM_REMOVED was
            # still propagating here during a tab-delete cascade). Nothing to remove -- the
            # parent teardown handles the children. Swallowing this lets the cascade finish
            # and reach super().request_delete()/CHUNK_DELETED, so the tab actually closes.
            pass
        return False

    def add_widget(self, widget: QtWidgets.QWidget, label=None):  # type: ignore
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)

        labeled = LabeledWidget(widget, self)
        # Now parented, so showing it can no longer produce a top-level window -- lift the
        # suppression create_widget put on it.
        widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen, False)
        widget.installEventFilter(self)
        labeled.add_label_filter(self)

        if label is not None:
            labeled.update_label(label)
        if isinstance(widget, ChunkParamWidgetBase):
            labeled._name_hint = widget.param.name

        if self.group.has_flag(ChunkFlags.GroupFlags.LIST):
            del_icon = QtGui.QIcon(str(chunk_resources.get_icon("remove")))

            del_btn = QtWidgets.QToolButton()
            del_btn.setIcon(del_icon)
            del_btn.setIconSize(QtCore.QSize(15, 15))
            del_btn.clicked.connect(partial(self.remove_from_list_group, widget))
            labeled.vlayout.insertWidget(0, del_btn)

        self.content_layout.addWidget(labeled)

    def remove_from_list_group(self, widget: QtWidgets.QWidget):
        if isinstance(self.group, chunk_core.ChunkListGroupBase):
            for i in range(self.content_layout.count()):
                item = self.content_layout.itemAt(i)
                if not item:
                    continue

                iw = item.widget()
                if isinstance(iw, LabeledWidget) and iw.widget == widget:
                    self.group.remove_index(i)

        elif isinstance(widget, chunk_core.ChunkContainerWidgetBase):
            widget.group.request_delete()
        elif isinstance(widget, chunk_widget_api.ChunkParamWidgetBase):
            widget.param.request_delete()
        self.remove_widget(widget)


_VIEW_COL_HEADER = "#c87830"


def slot_for_key(key) -> int | None:
    """The view slot a number key picks: 0 (the Output) for 1, a view slot for 2 and 3.

    Args:
        key (QtCore.Qt.Key): The key pressed.

    Returns:
        The slot, or None for any other key.
    """
    number = key - QtCore.Qt.Key.Key_0
    if number == chunk_graph.OUTPUT_NUMBER:
        return 0
    return next((slot for slot in chunk_graph.VIEW_SLOTS if chunk_graph.view_number(slot) == number), None)


class ViewSlotState(QtCore.QObject):
    """Shared view-slot state for a context tab.

    Holds the active view slot (0 = none, so the graph cooks to its Output; 1–2 = a view)
    and notifies connected widgets when it changes.  The actual blocking/muting logic lives
    in ChunkGraphView; this object is the lightweight communication bus between
    the nodegraph and the params panel.
    """

    slot_changed = QtCore.Signal(int)   # 0 = the Output, 1–2 = active view slot
    create_view_for = QtCore.Signal(str, int)  # (prim path, slot) -- handled by ChunkGraphView
    set_output_for = QtCore.Signal(str)  # prim path -- "Set Output", handled by ChunkGraphView
    loft_changed = QtCore.Signal(object, bool)  # (chunk, lofted) -- Loft up / Unloft chunk

    def __init__(self, parent=None):
        super().__init__(parent)
        self._slot: int = 0

    @property
    def active_slot(self) -> int:
        return self._slot

    def set_slot(self, slot: int) -> None:
        if slot != self._slot:
            self._slot = slot
            self.slot_changed.emit(slot)

    def _exec_menu(self, menu: QtWidgets.QMenu, pos: QtCore.QPoint):
        """Thin, overridable seam around QMenu.exec() -- PySide6 doesn't allow monkeypatching
        QMenu.exec itself (it's a bound virtual slot, not a plain Python attribute), so tests
        stub this plain instance method instead of driving a real modal popup.
        """
        return menu.exec(pos)

    def _run_menu_action_on(self, conn, meth, targets) -> None:
        """Run the menu action the user picked once per selected chunk.

        Each chunk gets its OWN callback, looked up by name on that chunk -- `conn` belongs
        to the one that was right-clicked, and firing it repeatedly would run that chunk's
        action N times instead of each chunk's own.

        Deletes are special twice over. Every chunk registers `remove` as a menu action, and
        ChunkBase.remove calls request_delete() directly -- bypassing the Delete-key path
        that normally captures recook relevance -- so relevance is captured for the whole
        set BEFORE any of them goes, which is the only moment it can still be judged (see
        mark_pending_delete_relevance). And deleting one chunk can take another with it: a
        group deletes its contents, so a target selected alongside its own parent is already
        gone by its turn. Skipped rather than reported, since that is the requested outcome.
        """
        name = getattr(meth, "__name__", None)
        is_delete = name == "remove"
        if is_delete:
            announce_pending_delete(list(targets))

        for target in targets:
            if is_delete and getattr(target, "_deleting", False):
                continue
            own = next(
                (
                    c for c in target.find_callbacks(
                        notice_type=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE
                    )
                    if getattr(c.get_method(), "__name__", None) == name
                ),
                None,
            )
            if own is None:
                continue  # this chunk type does not offer the action; leave it alone
            own.handle_callback(
                chunk_notice.MenuActionNotice(target.get_notice_data()), target, False
            )

    def show_chunk_menu(
        self,
        grp: chunk.ChunkBase,
        prim_path: str,
        global_pos: QtCore.QPoint,
        graph_view=None,
    ) -> None:
        """Build and exec the shared right-click menu for a chunk.

        Handles custom callback actions, Inspect Layer, and Set View / Activate
        View N.  The actual view-node creation is delegated to ChunkGraphView
        via the create_view_for signal.

        graph_view: the owning ChunkGraphView, passed only by the graph node right-click
        (ChunkGraphView._show_chunk_menu) -- None from the Params-tab widget's own right-click
        (ChunkWidget.contextMenuEvent), which has no graph selection to act on. Gates
        Copy/Cut/Duplicate, which operate on the graph's current (possibly multi-node)
        selection via graph_view, not just this one right-clicked chunk.
        """
        # What this menu acts on. Right-clicking a node inside the selection acts on the
        # whole selection; right-clicking one outside it acts on that node alone, the same
        # as every other app -- see ChunkGraphView.selected_chunks.
        targets = graph_view.selected_chunks(grp) if graph_view is not None else [grp]
        many = len(targets) > 1
        # Said out loud in the label. An action that is about to hit six chunks should not
        # look identical to one that hits the one under the cursor.
        def _label(text: str) -> str:
            return f"{text} ({len(targets)} chunks)" if many else text

        menu = QtWidgets.QMenu()
        cb_actions: dict = {}
        for conn in grp.find_callbacks(notice_type=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE):
            meth = conn.get_method()
            if meth:
                cb_actions[menu.addAction(_label(meth.__name__))] = conn

        # Inspect, Rename and Set View stay singular on purpose: one dialog, one name (six
        # chunks cannot share it), and a view targets one chunk. Everything else scales.
        inspect_action = menu.addAction("Inspect Layer")
        rename_action = menu.addAction("Rename…")
        stage_for_rename = grp.get_stage()
        prim_for_rename = stage_for_rename.GetPrimAtPath(prim_path) if stage_for_rename else None
        if prim_for_rename and prim_for_rename.IsValid() and chunk_utils.is_composed_in(prim_for_rename):
            # USD has no rename override; the name belongs to the file this chunk came
            # from. Localise the network to change its structure.
            rename_action.setEnabled(False)
            rename_action.setToolTip("Composed in from another file -- localise the network to rename")
        loft_action = menu.addAction(
            _label("Unloft chunk" if chunk_stage.is_chunk_lofted(grp) else "Loft up chunk")
        )
        # A group's network can go out to a file and come back. Publish writes the group
        # and makes it a host of that file (edits from then on are overrides); Localise
        # takes a hosted network back in as a plain chunkgroup of this file. Singular:
        # each is a conversation about one file.
        publish_action = menu.addAction("Publish network…") if chunk_stage.is_chunkgroup(grp) else None
        localise_action = menu.addAction("Localise network") if chunk_stage.is_localisable(grp) else None
        copy_action = cut_action = duplicate_action = None
        if graph_view is not None:
            menu.addSeparator()
            copy_action = menu.addAction("Copy")
            cut_action = menu.addAction("Cut")
            duplicate_action = menu.addAction("Duplicate")
        menu.addSeparator()

        stage: Usd.Stage | None = grp.get_stage()
        chunk_type = None
        existing_view_slot: int | None = None
        is_output_source = False
        if stage:
            prim = stage.GetPrimAtPath(prim_path)
            if prim.IsValid():
                chunk_type = prim.GetAttribute("info:chunkType").Get()
            if chunk_type not in ("view", "output"):
                for p in stage.Traverse():
                    if p.GetTypeName() != chunk_naming.CHUNK_PRIM_TYPENAME:
                        continue
                    p_type = p.GetAttribute("info:chunkType").Get()
                    if p_type not in ("view", "output"):
                        continue
                    exec_in = p.GetAttribute(chunk_naming.EXEC_INPUT)
                    if exec_in.IsValid():
                        for conn_path in exec_in.GetConnections():
                            if str(conn_path.GetPrimPath()) != prim_path:
                                continue
                            if p_type == "output":
                                is_output_source = True
                            else:
                                existing_view_slot = p.GetAttribute("view:slot").Get()

        # "Set Output" over one entry per view slot: picking a slot that already has a view
        # re-points that view here. What already ends at this chunk is shown checked.
        output_action = None
        view_actions: dict = {}
        if chunk_type not in ("view", "output"):
            output_action = menu.addAction("Set Output")
            output_action.setCheckable(True)
            output_action.setChecked(is_output_source)
        if chunk_type != "view":
            for slot in chunk_graph.VIEW_SLOTS:
                act = menu.addAction(f"Set View {chunk_graph.view_number(slot)}")
                act.setCheckable(True)
                act.setChecked(slot == existing_view_slot)
                view_actions[act] = slot

        res = self._exec_menu(menu, global_pos)
        if not res:
            return

        if res == inspect_action:
            layer = grp.get_edit_context()
            if layer:
                source_code_dial(layer.ExportToString(), chunk_core.UsdSyntaxHighlightMapping())
        elif res == rename_action:
            old = grp.name
            new, ok = QtWidgets.QInputDialog.getText(
                QtWidgets.QApplication.activeWindow(), "Rename chunk", "New name:", text=old
            )
            new = new.strip() if new else ""
            # Setting .name drives the framework rename (prim rename + expression patching);
            # convert_attr sanitises it to a valid identifier, so no path validation needed here.
            if ok and new and new != old:
                grp.name = new
        elif publish_action and res == publish_action:
            out, _ = QtWidgets.QFileDialog.getSaveFileName(
                QtWidgets.QApplication.activeWindow(), "Publish network", f"{grp.name}.usda",
                "USD (*.usda *.usd *.usdc)",
            )
            if out:
                chunk_stage.publish_network(grp, out)
        elif localise_action and res == localise_action:
            chunk_stage.localise_network(grp)
        elif res == loft_action:
            # The clicked chunk decides the direction, so a mixed selection ends up in one
            # state rather than each chunk flipping to the opposite of whatever it was.
            new_lofted = not chunk_stage.is_chunk_lofted(grp)
            for target in targets:
                chunk_stage.set_chunk_lofted(target, new_lofted)  # tells the pages (chunk_lofted_hook)
                self.loft_changed.emit(target, new_lofted)
        elif output_action and res == output_action:
            self.set_output_for.emit(prim_path)
        elif res in view_actions:
            slot = view_actions[res]
            if slot == existing_view_slot:
                self.set_slot(slot)
            else:
                self.create_view_for.emit(prim_path, slot)
        elif copy_action and res == copy_action:
            graph_view._copy_selected_to_clipboard()
        elif cut_action and res == cut_action:
            graph_view._cut_selected_to_clipboard()
        elif duplicate_action and res == duplicate_action:
            graph_view._duplicate_selected()
        elif conn := cb_actions.get(res):
            meth = conn.get_method()
            if many:
                self._run_menu_action_on(conn, meth, targets)
                return
            if meth is not None and meth == getattr(grp, "remove", None):
                # ChunkBase.remove (every chunk registers it as a default MENU_ACTION_NOTICE
                # callback) calls request_delete() directly, bypassing
                # ChunkGraphView._delete_selected() entirely (that's the Delete-key path).
                # Capture relevance the same way, right before it runs -- see
                # mark_pending_delete_relevance's docstring for why it can't be judged after.
                announce_pending_delete([grp])
            conn.handle_callback(
                chunk_notice.MenuActionNotice(grp.get_notice_data()), grp, False
            )


class ParamsFilterBar(QtWidgets.QWidget):
    """The search bar in a Params page's search box: a fuzzy search box with a Name/Type/Both
    scope combobox (see chunk_stage.chunk_matches_search and ChunkWidget._refresh_page).
    """

    search_changed = QtCore.Signal(str, str)  # (query, scope)

    _SCOPE_ITEMS = (("Both", "both"), ("Name", "name"), ("Type", "type"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self.search_box = QtWidgets.QLineEdit(placeholderText="Search this graph's chunks...")
        self.search_box.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_box, 1)

        self.scope_combo = QtWidgets.QComboBox()
        for label, value in self._SCOPE_ITEMS:
            self.scope_combo.addItem(label, value)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        layout.addWidget(self.scope_combo)

    def set_search_state(self, query: str, scope: str) -> None:
        self.search_box.blockSignals(True)
        self.search_box.setText(query)
        self.search_box.blockSignals(False)

        self.scope_combo.blockSignals(True)
        idx = self.scope_combo.findData(scope)
        self.scope_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.scope_combo.blockSignals(False)

    def current_scope(self) -> str:
        return self.scope_combo.currentData()

    def _on_scope_changed(self, _index: int) -> None:
        self.search_changed.emit(self.search_box.text(), self.current_scope())

    def _on_search_changed(self, text: str) -> None:
        self.search_changed.emit(text, self.current_scope())


class ChunkPanelList(QtWidgets.QWidget):
    """A column of chunk panels, kept to a list of chunks in that list's order.

    set_chunks makes panels for chunks that are new, drops the ones for chunks that left and
    puts the rest in order, so a caller hands over the whole list each time and never works
    out the change itself. Hidden while it has no panels, or while folded.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)
        self._panels: dict = {}  # chunk hash -> its panel
        self._folded = False
        self.setVisible(False)

    def panels(self) -> dict:
        """The panels shown, by chunk hash."""
        return {h: p for h, p in self._panels.items() if self._owns(p)}

    def set_chunks(self, chunks: list) -> None:
        """Show a panel for each of chunks, in their order, and nothing else.

        Args:
            chunks: The chunks to show.
        """
        wanted = {c.get_hash() for c in chunks}
        for h, panel in list(self._panels.items()):
            if h not in wanted or not self._owns(panel):
                self._drop(h)
        for i, c in enumerate(chunks):
            h = c.get_hash()
            panel = self._panels.get(h)
            if panel is None:
                panel = ChunkWidget(c, self)
                panel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
                # A chunk deleted from anywhere takes its panel with it (CHUNK_DELETED).
                panel.destroyed.connect(partial(self._forget, h))
                self._panels[h] = panel
            if self._layout.indexOf(panel) != i:
                self._layout.insertWidget(i, panel)
        self._sync_visible()

    def set_folded(self, folded: bool) -> None:
        """Hide the panels while folded, whatever they are."""
        self._folded = folded
        self._sync_visible()

    def _owns(self, panel) -> bool:
        """Whether panel is still alive and still in this list."""
        try:
            return panel.parentWidget() is self
        except RuntimeError:
            return False

    def _drop(self, h) -> None:
        panel = self._panels.pop(h, None)
        if panel is None:
            return
        try:
            self._layout.removeWidget(panel)
            panel.delete_self()
        except RuntimeError:
            pass  # already gone

    def _forget(self, h, *_args) -> None:
        self._panels.pop(h, None)
        try:
            self._sync_visible()
        except RuntimeError:
            pass  # the list is going too

    def _sync_visible(self) -> None:
        self.setVisible(bool(self._panels) and not self._folded)


class ParamsSearchPanel(QtWidgets.QFrame):
    """The search box of a Params page, apart from the group's box above it.

    A search bar, a line saying what it found, and the chunks it found. Searching changes
    only this box: what the group shows stays as it is.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ParamsSearch")
        self.setStyleSheet(
            f"QFrame#ParamsSearch {{ background-color: {_PARAMS_SEARCH_BG};"
            f" border: 1px solid {_PARAMS_BOX_EDGE}; border-radius: 6px; }}"
        )
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(2)
        self.filter_bar = ParamsFilterBar(self)
        layout.addWidget(self.filter_bar)
        self.status = QtWidgets.QLabel(self)
        self.status.setStyleSheet("color: #8a8a8a; padding: 0px 6px;")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.results = ChunkPanelList(self)
        layout.addWidget(self.results)
        self.show_results("", [], 0)

    def show_results(self, query: str, found: list, lofted_matches: int) -> None:
        """Show what a search found.

        Args:
            query: The search text.
            found: The chunks to show here: the matches that aren't lofted.
            lofted_matches: How many matches are lofted, so already in the group's box.
        """
        self.results.set_chunks(found)
        self.status.setText(self.status_text(query, len(found), lofted_matches))

    @staticmethod
    def status_text(query: str, found: int, lofted_matches: int) -> str:
        """The line under the search bar.

        Args:
            query: The search text.
            found: How many matches are shown in this box.
            lofted_matches: How many matches are in the group's box instead.

        Returns:
            The text.
        """
        if not query:
            return "Type to find any chunk in this graph."
        above = f"{lofted_matches} lofted above" if lofted_matches else ""
        if found:
            return f"{found} found" + (f", plus {above}" if above else "")
        return f"Nothing else found: {above}" if above else "Nothing found"


class ChunkWidget(ContainerWidget[chunk.ChunkBase]):
    """Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    SELECTED_COLOR = "#cab46a"

    # Read by apply_chunk_stylesheet: a nested ChunkWidget asks whether it is covered while
    # its outer one is still mid-__init__ and has not applied the sheet yet. Answering from
    # the type rather than the flag is what keeps a nine-page shader at one sheet, not ten.
    _chunk_styles_self = True

    # Overridden per-instance in __init__; here so any path that reads it before
    # construction finishes finds a value rather than an AttributeError.
    _defer_stylesheet = False
    _node_body = False

    # Params pages and the ChunkGroup sections on them, told when a chunk is lofted or
    # unlofted (chunk_loft_changed): the group's own page may be another tab than the one
    # they are on.
    _group_sections: weakref.WeakSet = weakref.WeakSet()
    # The panels of the chunks lofted to this group (see _sync_lofted_chunks). None on a
    # graph node, which would grow a whole panel per chunk, and on anything not a group.
    _lofted_chunks: ChunkPanelList | None = None

    @property
    def defer_collapsed_children(self) -> bool:  # type: ignore[override]
        """Defer for the groups inside a chunk, never for a chunk itself.

        Asked of the group rather than the class, because _add_group_layout builds every
        nested container as this same class. A chunk's own body is always built: a
        collapsed node already hides it whole, and the node graph measures ports off it.
        """
        return not isinstance(self.group, chunk.ChunkBase)

    def __init__(
        self,
        group: chunk.ChunkBase,
        parent=None,
        is_tab_context: bool = False,
        for_proxy: bool = False,
    ):
        # Must be set before super().__init__ so build_collapsable sees the correct role.
        self._is_tab_context_widget = is_tab_context
        # Node bodies style themselves after their reparent, not during construction.
        self._defer_stylesheet = for_proxy
        # A node's body in the graph, rather than a Params page. Set before super().__init__,
        # which builds the contents.
        self._node_body = for_proxy
        self._view_state: ViewSlotState | None = None
        self._view_overlay: QtWidgets.QLabel | None = None
        # The chunk hashes _track_panels last put into group_widgets.
        self._page_hashes: set = set()
        self._page_built = False
        super().__init__(group, parent)

        self.installEventFilter(self)
        if is_tab_context:
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def set_view_state(self, vs: ViewSlotState) -> None:
        if self._view_state is not None:
            self._view_state.slot_changed.disconnect(self._update_view_overlay)
            self._view_state.loft_changed.disconnect(self._on_loft_changed)
        self._view_state = vs
        vs.slot_changed.connect(self._update_view_overlay)
        vs.loft_changed.connect(self._on_loft_changed)
        if self._is_tab_context_widget:
            self._ensure_view_overlay()
            self._update_view_overlay(vs.active_slot)

    def _on_search_changed(self, query: str, scope: str) -> None:
        """The search text or scope changed: live, on every keystroke."""
        chunk_stage.set_search_query(self.group, query)
        chunk_stage.set_search_scope(self.group, scope)
        self._refresh_page()

    def _on_loft_changed(self, target_chunk: chunk.ChunkBase, lofted: bool) -> None:
        """A chunk was lofted or unlofted from the shared menu (graph or Params surface)."""
        self._refresh_page()

    def _child_chunks(self, gone=None) -> list[chunk.ChunkBase]:
        """The chunks in this group's graph, in order: its own, then the ChunkGroups in it.

        A ChunkGroup is not among its parent's params -- add_chunk puts every group at the top
        of the scene -- so those are found by the group they name as their parent.

        Args:
            gone: A chunk to leave out, as it is being removed.

        Returns:
            The chunks.
        """
        group = self.group
        out = [p for p in group.get_my_params() if isinstance(p, chunk.ChunkBase) and p is not gone]
        if scene := chunk.get_scene():
            seen = {id(c) for c in out}
            for ctx in scene.get_contexts():
                if (id(ctx) not in seen and ctx is not group and ctx is not gone
                        and chunk_stage.owning_group(ctx) is group):
                    out.append(ctx)
                    seen.add(id(ctx))
        return out

    def _sync_lofted_chunks(self, gone=None) -> list[chunk.ChunkBase]:
        """Show the chunks lofted to this group in its box, in graph order.

        Args:
            gone: A chunk to leave out, as it is being removed.

        Returns:
            The group's chunks, with gone left out.
        """
        children = self._child_chunks(gone)
        if self._lofted_chunks is not None:
            self._lofted_chunks.set_chunks([c for c in children if chunk_stage.is_chunk_lofted(c)])
            if not self._is_tab_context_widget:
                self._track_panels(self._lofted_chunks.panels())
        return children

    def _track_panels(self, panels: dict) -> None:
        """Keep group_widgets, how the rest of the code finds a chunk's panel, to panels.

        Args:
            panels: The chunk panels shown now, by chunk hash.
        """
        for h in self._page_hashes - panels.keys():
            self.group_widgets.pop(h, None)
        for h, panel in panels.items():
            self.group_widgets[h] = panel
        self._page_hashes = set(panels)

    def _refresh_page(self, gone=None) -> None:
        """Bring a Params page up to date: the group's lofted chunks, and what the search finds.

        The one place either is worked out. Everything that can change them -- a chunk added,
        removed, lofted or unlofted, a new search -- calls this, and it holds whatever the page
        showed before, so the two boxes can't drift apart. The search never changes the
        group's box: it shows the matches that aren't lofted in its own box.

        Args:
            gone: A chunk to leave out, as it is being removed.
        """
        if not (self._is_tab_context_widget and self._page_built):
            return
        children = self._sync_lofted_chunks(gone)
        query = chunk_stage.get_search_query(self.group)
        scope = chunk_stage.get_search_scope(self.group)
        matches = [c for c in children if query and chunk_stage.chunk_matches_search(c, query, scope)]
        found = [c for c in matches if not chunk_stage.is_chunk_lofted(c)]
        self._search_panel.show_results(query, found, len(matches) - len(found))

        self._track_panels({**self._lofted_chunks.panels(), **self._search_panel.results.panels()})

    def _ensure_view_overlay(self) -> None:
        if self._view_overlay is not None:
            return
        ol = QtWidgets.QLabel(self)
        ol.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        ol.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        ol.hide()
        self._view_overlay = ol
        self._position_view_overlay()

    def _update_view_overlay(self, slot: int) -> None:
        if not self._is_tab_context_widget or not self._view_overlay:
            return
        if slot == 0:
            text = "Output"
            self._view_overlay.setStyleSheet(
                "background: rgba(58,111,143,210); color: white;"
                "border-radius: 4px; padding: 3px 8px;"
            )
        else:
            text = f"View: {chunk_graph.view_number(slot)}"
            self._view_overlay.setStyleSheet(
                "background: rgba(200,120,48,210); color: white;"
                "border-radius: 4px; padding: 3px 8px;"
            )
        self._view_overlay.setText(text)
        self._view_overlay.adjustSize()
        self._position_view_overlay()
        self._view_overlay.show()
        self._view_overlay.raise_()

    def _position_view_overlay(self) -> None:
        if not self._view_overlay:
            return
        sz = self._view_overlay.sizeHint()
        self._view_overlay.setGeometry(
            self.width() - sz.width() - 8,
            self.height() - sz.height() - 8,
            sz.width(),
            sz.height(),
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._view_overlay:
            self._position_view_overlay()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if self._view_state and self._is_tab_context_widget:
            if (slot := slot_for_key(event.key())) is not None:
                self._view_state.set_slot(slot)
                event.accept()
                return
        super().keyPressEvent(event)

    def _is_group_chunk(self) -> bool:
        """True when this is a user-created ChunkGroup (CONTEXT with a module), not a plain tab context."""
        cfg = self.group.get_config() if isinstance(self.group, chunk.ChunkBase) else None
        return bool(self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module)

    def _is_group_chunk_node(self) -> bool:
        """True only when shown as a node in a parent context (not as the tab context widget itself)."""
        return self._is_group_chunk() and not getattr(self, '_is_tab_context_widget', False)

    def build_collapsable(self):
        super().build_collapsable()

        # Here rather than in set_selected, which runs from update_from_state at the end of
        # the base __init__ -- by which point _build_param_widgets has filled the subtree
        # that setStyleSheet then has to re-polish. At this point the tree is still empty.
        #
        # Skipped for a node body: that one is reparented to None on its way into a
        # QGraphicsProxyWidget, and reparenting a *styled* tree costs ~22ms against ~2.5ms
        # unstyled -- more than the full-tree sheet it was meant to save. create_proxy
        # applies it once the reparent is done instead. Measured, both orderings.
        if not self._defer_stylesheet:
            apply_chunk_stylesheet(self)

        if not isinstance(self.group, chunk.ChunkBase):
            return

        self.__layout__.setContentsMargins(2, 1, 2, 0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )

        if self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            self._prim_path_field = self._build_prim_path_field()

            self._lofted_container = QtWidgets.QWidget(self)
            self._lofted_container.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Maximum,
            )
            self._lofted_layout = QtWidgets.QVBoxLayout(self._lofted_container)
            self._lofted_layout.setContentsMargins(4, 2, 4, 4)
            self._lofted_layout.setSpacing(2)
            self._lofted_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            self._lofted_container.setVisible(False)

            if not self._node_body:  # a graph node would grow a whole panel per chunk
                self._lofted_chunks = ChunkPanelList(self)

            if self._is_tab_context_widget:
                self._build_params_page()
            else:
                above_content = self.__layout__.indexOf(self.content)
                self.__layout__.insertWidget(above_content, self._prim_path_field)
                self.__layout__.insertWidget(above_content + 1, self._lofted_container)
                if self._lofted_chunks is not None:
                    self.__layout__.insertWidget(above_content + 2, self._lofted_chunks)

        if self._is_group_chunk():
            self.minimize_button.installEventFilter(self)
            if self._is_group_chunk_node():
                # Node view: content has no sub-chunks. Zero out its height so the
                # layout never allocates space for it regardless of frame style or
                # animation side-effects.
                self.content.hide()
                self.content.setMaximumHeight(0)

    def _build_params_page(self) -> None:
        """Lay out a Params page: the group's box, then the search box, both scrolling.

        The group's box holds this widget's header button (moved in, so it titles the box),
        the prim path, the lofted params and the lofted chunks; the header folds this box
        only. The search box below it is separate. Both are items of the scrolling page, so
        a tall lofted chunk can't push the page past the dock.
        """
        self._group_box = QtWidgets.QFrame(self)
        self._group_box.setObjectName("ParamsGroupBox")
        self._group_box.setStyleSheet(
            f"QFrame#ParamsGroupBox {{ background-color: {_PARAMS_HEADER_BG};"
            f" border: 1px solid {_PARAMS_BOX_EDGE}; border-radius: 6px; }}"
        )
        self._group_box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        box_layout = QtWidgets.QVBoxLayout(self._group_box)
        box_layout.setContentsMargins(1, 1, 1, 4)
        box_layout.setSpacing(0)
        self.__layout__.removeWidget(self.minimize_button)
        box_layout.addWidget(self.minimize_button)

        self._group_box_body = QtWidgets.QWidget(self._group_box)
        body_layout = QtWidgets.QVBoxLayout(self._group_box_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        # Plain QWidgets paint no background of their own without WA_StyledBackground.
        for w in (self._prim_path_field, self._lofted_container):
            w.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
            w.setStyleSheet(f"background-color: {_PARAMS_HEADER_BG};")
            body_layout.addWidget(w)
        body_layout.addWidget(self._lofted_chunks)
        box_layout.addWidget(self._group_box_body)

        self._search_panel = ParamsSearchPanel(self)
        self._filter_bar = self._search_panel.filter_bar
        self._filter_bar.set_search_state(
            chunk_stage.get_search_query(self.group), chunk_stage.get_search_scope(self.group)
        )
        self._filter_bar.search_changed.connect(self._on_search_changed)

        self.content_layout.insertWidget(0, self._group_box)
        self.content_layout.insertWidget(1, self._search_panel)

    def add_widget(self, widget: QtWidgets.QWidget, label=None):
        if self._is_group_chunk_node() and not isinstance(widget, ChunkParamWidgetBase):
            # Shown as a node: suppress all non-lofted widgets.
            return
        # A Params page shows its chunks in its two boxes (_refresh_page), so whatever else it
        # is given is the group's own interface: it goes in the group's box.
        if (self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                and (isinstance(widget, ChunkParamWidgetBase) or self._is_tab_context_widget)):
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)
            labeled = LabeledWidget(widget, self)
            if label is not None:
                labeled.update_label(label)
            if isinstance(widget, ChunkParamWidgetBase):
                labeled._name_hint = widget.param.name
            self._lofted_layout.addWidget(labeled)
            self._lofted_container.setVisible(True)
        else:
            super().add_widget(widget, label)

    def remove_widget(self, widget: QtWidgets.QWidget):
        # A lofted param's row sits in the header, not in the chunk list.
        if not self._remove_row(self.content_layout, widget) and hasattr(self, "_lofted_layout"):
            if self._remove_row(self._lofted_layout, widget) and not self._lofted_layout.count():
                self._lofted_container.setVisible(False)

    def _add_group_layout(self, group):
        if isinstance(group, chunk.ChunkBase) and self._lofted_chunks is not None:
            # A chunk added to a group that shows its chunks itself: on a Params page, or in a
            # group's section on the page above.
            if self._is_tab_context_widget:
                self._refresh_page()
            else:
                self._sync_lofted_chunks()
            return None
        if self._is_group_chunk_node():
            # Don't create orphan Qt child widgets -- they render at (0,0) as overlays.
            return None
        if (isinstance(group, chunk.ChunkBase)
                and group.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                and (cfg := group.get_config()) and cfg.module):
            # ChunkGroup nodes are rendered via _add_group_widget (triggered by CHUNK_LOADED).
            # Suppress the normal nested-layout path to avoid a duplicate widget.
            return None
        return super()._add_group_layout(group)

    def _param_removed_callback(self, notice, sender):
        super()._param_removed_callback(notice, sender)
        if isinstance(notice.data, chunk.ChunkBase) and self._lofted_chunks is not None:
            if self._is_tab_context_widget:
                self._refresh_page(gone=notice.data)
            else:
                self._sync_lofted_chunks(gone=notice.data)

    def _build_param_widgets(self, group):
        if self._is_group_chunk_node():
            # Shown as a node: the group's interface only -- its lofted params, and on a Params
            # page the chunks lofted to it, not every chunk inside.
            for param in group.get_my_params():
                if isinstance(param, chunk_core.ChunkParamBase):
                    self._add_param_widget(param)
            if self._lofted_chunks is not None:
                ChunkWidget._group_sections.add(self)
                self._sync_lofted_chunks()
            return
        super()._build_param_widgets(group)
        if self._is_tab_context_widget:
            ChunkWidget._group_sections.add(self)
            self._page_built = True
            self._refresh_page()

    @classmethod
    def chunk_loft_changed(cls, target: chunk.ChunkBase, lofted: bool) -> None:
        """Show or take away target's panel in the sections and Params pages of its group.

        Args:
            target: The chunk lofted or unlofted.
            lofted: Its new state.
        """
        owner = chunk_stage.owning_group(target)
        for section in list(cls._group_sections):
            try:
                if section.group is not owner:
                    continue
                if section._is_tab_context_widget:
                    section._refresh_page()
                else:
                    section._sync_lofted_chunks()
            except (RuntimeError, ReferenceError):
                continue  # a section whose Qt side or group has gone

    def animation_finished(self):
        super().animation_finished()
        if self._is_context_tab() and not self.is_collapsed():
            # After expansion, clear the fixed height the animation pinned on the
            # QScrollArea so it can expand to fill __layout__ naturally.
            self.content.setMinimumHeight(0)
            self.content.setMaximumHeight(16777215)

    def set_expanded(self, checked):
        if self._is_tab_context_widget and hasattr(self, "_group_box_body"):
            # On a Params page the header folds the group's box; the search box stays. Kept on
            # the page rather than in the group's COLLAPSED state, which folds the group's
            # node in the graph above: one shouldn't fold the other.
            self._show_group_box_folded(not checked)
            return
        super().set_expanded(checked)

    def _show_group_box_folded(self, folded: bool) -> None:
        self._group_box_body.setHidden(folded)
        self.minimize_button.blockSignals(True)
        self.minimize_button.setChecked(not folded)
        self.minimize_button.blockSignals(False)
        self.minimize_button.setArrowType(
            QtCore.Qt.ArrowType.RightArrow if folded else QtCore.Qt.ArrowType.DownArrow
        )

    def update_from_state(self):
        if self._is_tab_context_widget:
            # Not ContainerWidget's: the group's COLLAPSED state is its node's in the graph
            # above. Applied here it folded the whole page to nothing, leaving one stretched
            # field showing (see set_expanded).
            chunk_core.ChunkContainerWidgetBase.update_from_state(self)
            if hasattr(self, "_group_box_body"):
                # update_from_flags, run just before, resets the header's arrow.
                self._show_group_box_folded(self._group_box_body.isHidden())
            return
        if self._is_group_chunk_node():
            # Skip super() -- it would start the collapse animation which calls
            # content.setFixedHeight and inflates the node's reported size.
            # Drive everything directly with blocked signals so toggled() never fires.
            expanded = not self.group.has_state(ChunkFlags.State.COLLAPSED)
            self.minimize_button.blockSignals(True)
            self.minimize_button.setChecked(expanded)
            self.minimize_button.blockSignals(False)
            if hasattr(self, '_prim_path_field'):
                self._prim_path_field.setVisible(expanded)
            if hasattr(self, '_lofted_container'):
                has_content = bool(self._lofted_layout.count()) if hasattr(self, '_lofted_layout') else False
                self._lofted_container.setVisible(expanded and has_content)
            if self._lofted_chunks is not None:
                self._lofted_chunks.set_folded(not expanded)
            self.set_selected(self.group.has_state(ChunkFlags.State.SELECTED))
            return
        super().update_from_state()

    def _add_group_widget(self, group_chunk):
        """A ChunkGroup was made in this page's graph: show it if it belongs on the page.

        Called on CHUNK_LOADED rather than through add_param, so the group's own PARAM_ADDED
        notices never reach this context's ownership chain.
        """
        self._refresh_page()

    def _build_prim_path_field(self) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        container.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Maximum,
        )
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        label = QtWidgets.QLabel("Prim Path")
        label.setFixedWidth(64)

        edit = QtWidgets.QLineEdit(self.group.prim_path or "")
        edit.setPlaceholderText("/chunks  (default)")

        def _commit():
            val = edit.text().strip() or None
            if val == self.group.prim_path:
                return
            if val is not None:
                p = Sdf.Path(val)
                if not p.IsPrimPath() or not p.IsAbsolutePath():
                    edit.setText(self.group.prim_path or "")
                    return
            chunk_stage.rename_context_prim_path(self.group, val)

        edit.editingFinished.connect(_commit)

        layout.addWidget(label)
        layout.addWidget(edit)
        return container

    def _reorder_chunk_widget_after(
        self,
        chunk_to_move: chunk.ChunkBase,
        after_chunk: chunk.ChunkBase,
    ) -> None:
        """Move chunk_to_move's widget to appear immediately after after_chunk's widget."""
        after_idx: int | None = None
        move_widget: QtWidgets.QWidget | None = None
        for i in range(self.content_layout.count()):
            item = self.content_layout.itemAt(i)
            if not item:
                continue
            w = item.widget()
            if not isinstance(w, LabeledWidget):
                continue
            inner = w.widget
            if isinstance(inner, ChunkWidget):
                if inner.group is after_chunk:
                    after_idx = i
                elif inner.group is chunk_to_move:
                    move_widget = w
        if after_idx is not None and move_widget is not None:
            self.content_layout.insertWidget(after_idx + 1, move_widget)

    def _find_view_state(self) -> ViewSlotState | None:
        if self._view_state:
            return self._view_state
        p = self.parent()
        while p is not None:
            if isinstance(p, ChunkWidget) and p._view_state:
                return p._view_state
            p = p.parent()
        return None

    def contextMenuEvent(self, event):
        if self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            event.ignore()
            return
        vs = self._find_view_state()
        if vs is None:
            event.ignore()
            return
        ctx = self.group.get_context()
        if ctx and getattr(ctx, "prim_path", None):
            prim_path = f"{ctx.prim_path}/chunks/{self.group.name}"
        else:
            prim_path = f"/chunks/{self.group.name}"
        vs.show_chunk_menu(self.group, prim_path, event.globalPos())

    def set_selected(self, val: bool):
        selected = bool(
            val and (not self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT) or self._is_group_chunk())
        )

        # setStyleSheet re-polishes the whole subtree beneath the widget and cost ~10ms a
        # call -- 112 of them, 1.7s, easily the most expensive thing in building a chunk.
        # Every nested group is its own ChunkWidget, but Qt stylesheets cascade, so only
        # the outermost needs to apply CHUNK_STYLESHEET, and
        # the selection border rides a dynamic property resolved by that one sheet.
        # build_collapsable now does this while the subtree is still empty, so this is
        # normally its no-op guard path; it still matters for a widget reparented out from
        # under whatever was covering it. Deferred for a proxy-bound body -- see
        # build_collapsable.
        if not self._defer_stylesheet:
            apply_chunk_stylesheet(self)

        if self.property("chunkSelected") != selected:
            self.setProperty("chunkSelected", selected)
            # A property the stylesheet selects on only takes effect after a re-polish,
            # and this one is scoped to this widget rather than its whole subtree.
            self.style().unpolish(self)
            self.style().polish(self)

        return super().set_selected(val)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if (watched is getattr(self, 'minimize_button', None)
                and event.type() == QtCore.QEvent.Type.MouseButtonDblClick
                and self._is_group_chunk()):
            # Double-click on the header of a ChunkGroup -- dive into its tab.
            parent = self.parent()
            while parent is not None:
                if callable(getattr(parent, '_on_dive_into', None)):
                    parent._on_dive_into(self.group)
                    return True
                parent = parent.parent()
            return True

        if isinstance(event, QtGui.QMouseEvent) and event.type() is event.Type.MouseButtonPress and event.isAccepted():
            if not (c := self.group.get_chunk()):
                event.ignore()
            elif (
                (QtCore.Qt.MouseButton.LeftButton | QtCore.Qt.MouseButton.RightButton) & event.buttons()
                and (scene := chunk.get_scene())
                and c not in scene.get_selected_chunks()
            ):
                if QtCore.Qt.KeyboardModifier.ShiftModifier in event.modifiers():
                    c.set_selected(True)
                else:
                    scene.set_selected_chunks([c])

        return super().eventFilter(watched, event)


# set_chunk_lofted tells every Params page and group section, whoever lofted the chunk.
chunk_stage.chunk_lofted_hook = ChunkWidget.chunk_loft_changed


def _value_text(value) -> str:
    """A param value as a short text for a message: floats to 6 digits, vectors as (x, y)."""
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (tuple, list)):
        return "(" + ", ".join(_value_text(v) for v in value) + ")"
    return str(value)


def confirm_dial(msg, title: str | None = None, detail: str | None = None) -> bool:
    dial = QtWidgets.QMessageBox(text=msg)
    if title:
        dial.setWindowTitle(title)
    if detail:
        dial.setInformativeText(detail)
    dial.setStandardButtons(QtWidgets.QMessageBox.StandardButton.No | QtWidgets.QMessageBox.StandardButton.Yes)
    return dial.exec() == QtWidgets.QMessageBox.StandardButton.Yes


def progress_dial(title: str, label: str, run):
    """Run `run(on_line)` on a worker thread behind a modal progress dialog; return its result.

    The bar is a busy indicator -- pip does not say how much is left until it is done -- and
    the line under the label is the last thing `run` reported, so a long download is
    visibly moving. Modal, and the call does not return until the work has, so the caller
    can use the result as if it had run inline; the UI repaints meanwhile.

    `run` must not touch Qt: on_line only queues the text, which a timer on the main
    thread puts on screen.
    """
    dial = QtWidgets.QProgressDialog(label, "", 0, 0)
    dial.setWindowTitle(title)
    dial.setCancelButton(None)  # pip cannot be stopped halfway cleanly
    dial.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
    dial.setMinimumDuration(0)
    dial.setMinimumWidth(480)

    lines: list[str] = []
    result: list = []
    errors: list[BaseException] = []

    def work():
        try:
            result.append(run(lines.append))
        except BaseException as exc:  # handed back to the main thread below
            errors.append(exc)

    loop = QtCore.QEventLoop()
    worker = threading.Thread(target=work, name="usd_chunks-pip", daemon=True)

    def poll():
        if lines:
            dial.setLabelText(f"{label}\n{lines[-1][:120]}")
        if not worker.is_alive():
            timer.stop()
            loop.quit()

    timer = QtCore.QTimer()
    timer.timeout.connect(poll)
    dial.show()
    worker.start()
    timer.start(50)
    loop.exec()
    dial.close()
    dial.deleteLater()

    if errors:
        raise errors[0]
    return result[0]


def source_code_dial(src: str, highlighter: chunk_core.SyntaxHighlightMapping):
    _editor = QtWidgets.QPlainTextEdit(src, readOnly=True)

    SyntaxHighlighter(_editor.document(), highlighter)

    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
    _editor.setFont(font)

    dial = QtWidgets.QDialog()
    dial_layout = QtWidgets.QHBoxLayout()
    dial.setLayout(dial_layout)
    dial_layout.addWidget(_editor)
    return dial.exec()


#: Starts a search result's category column, so a divider runs down the list.
_RESULT_DIVIDER = "│  "


_MENU_SELECTED_RULE = re.compile(r"QMenu::item:selected\s*\{([^}]*)\}")


def _host_menu_highlight() -> tuple:
    """(background, color) a highlighted menu row has in this host, as stylesheet values.

    Hosts colour menus in their application stylesheet -- usdview's is a tan
    rgb(190, 156, 85) -- not in the palette, so a list inside the menu would highlight in
    the palette's unrelated colour. Read the host's rule; fall back to the palette.
    """
    background, color = "palette(highlight)", "palette(highlighted-text)"
    app = QtWidgets.QApplication.instance()
    if app is not None and (rule := _MENU_SELECTED_RULE.search(app.styleSheet() or "")):
        for prop in rule.group(1).split(";"):
            name, _, value = prop.partition(":")
            name, value = name.strip(), value.strip()
            if name in ("background", "background-color") and value:
                background = value
            elif name == "color" and value:
                color = value
    return background, color


class _SearchResults(QtWidgets.QTreeWidget):
    """The ranked search results inside the chunk menu: name | category, one row each.

    A list widget rather than menu rows: it scrolls, the category column starts at the same
    x on every row, and refilling it is cheap. Refilling a QMenu meant re-inserting every
    action on every keystroke (13s to type "materialx" with 200 results), and a QMenu capped
    in height clips instead of scrolling under usdview's style.
    """

    #: Emitted with the MenuEntry that was clicked.
    picked = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setIndentation(0)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)  # typing stays in the search field
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        # Sized to its columns, never the other way round: a stretched last section reports
        # the stretched width, and sizing the widget from it widened the menu every search.
        self.header().setStretchLastSection(False)
        # The list never has focus (typing stays in the search field), so the highlight is
        # stated for every state rather than left to the unfocused palette, which draws none.
        background, color = _host_menu_highlight()
        self.setStyleSheet(
            "QTreeWidget { background: transparent; border: none; }"
            "QTreeWidget::item { padding: 1px 4px; }"
            "QTreeWidget::item:selected, QTreeWidget::item:selected:!active {"
            f" background: {background}; color: {color}; }}"
        )
        # A menu highlights what the mouse is over; so does this.
        self.itemEntered.connect(self.setCurrentItem)
        self.itemClicked.connect(lambda item, _col: self.picked.emit(item.data(0, QtCore.Qt.ItemDataRole.UserRole)))

    def show_entries(self, entries, visible_rows: int) -> None:
        """Refill with entries, best first, and size to at most visible_rows rows."""
        self.clear()
        dim = self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText)
        for entry in entries:
            item = QtWidgets.QTreeWidgetItem(
                [entry.label, _RESULT_DIVIDER + " › ".join(entry.namespace[:-1])]
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, entry)
            item.setForeground(1, dim)
            if entry.tooltip:
                item.setToolTip(0, entry.tooltip)
            self.addTopLevelItem(item)
        if entries:
            self.setCurrentItem(self.topLevelItem(0))
        for column in (0, 1):
            self.setColumnWidth(column, self.sizeHintForColumn(column) if entries else 0)

        row = self.sizeHintForRow(0) if entries else 0
        self.setFixedHeight(row * min(len(entries), visible_rows) + 2)
        scroll = self.verticalScrollBar().sizeHint().width() if len(entries) > visible_rows else 0
        self.setFixedWidth(self.columnWidth(0) + self.columnWidth(1) + scroll + 4)

    def current_entry(self):
        item = self.currentItem()
        return item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item is not None else None

    def step(self, key) -> None:
        """Move the highlight the way a menu's arrow keys would."""
        count = self.topLevelItemCount()
        if not count:
            return
        row = self.indexOfTopLevelItem(self.currentItem()) if self.currentItem() else -1
        page = max(1, self.viewport().height() // max(1, self.sizeHintForRow(0)) - 1)
        moves = {
            QtCore.Qt.Key.Key_Down: row + 1,
            QtCore.Qt.Key.Key_Up: row - 1,
            QtCore.Qt.Key.Key_PageDown: row + page,
            QtCore.Qt.Key.Key_PageUp: row - page,
            QtCore.Qt.Key.Key_Home: 0,
            QtCore.Qt.Key.Key_End: count - 1,
        }
        target = max(0, min(count - 1, moves.get(key, row)))
        self.setCurrentItem(self.topLevelItem(target))


class _ListItem(NamedTuple):
    """A plain text row for _SearchResults: a label with no category."""

    label: str
    tooltip: str = ""

    @property
    def namespace(self) -> tuple:
        return (self.label,)


class _SearchableListMenu(QtWidgets.QMenu):
    """A Menu param's popup when it has too many items for a plain menu.

    A search field over one scrolling list, so hundreds of items (Apply Schema lists every
    API schema a plugin registers) stay one column instead of a screen-wide grid. Typing
    keeps the items that contain every typed word, in their menu order.
    """

    #: Items a plain QMenu shows; more get this menu.
    MAX_PLAIN_ITEMS = 25

    #: Rows on screen at once; more scroll.
    VISIBLE_ROWS = 20

    _NAV_KEYS = (
        QtCore.Qt.Key.Key_Down,
        QtCore.Qt.Key.Key_Up,
        QtCore.Qt.Key.Key_PageDown,
        QtCore.Qt.Key.Key_PageUp,
    )

    #: Emitted with the text of the item that was picked.
    picked = QtCore.Signal(str)

    def __init__(self, items: list[str], current: str, parent=None):
        super().__init__(parent)
        self._items = [_ListItem(text) for text in items]
        self._width = None
        self.search = QtWidgets.QLineEdit(placeholderText="Search...")
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        search_action = QtWidgets.QWidgetAction(self)
        search_action.setDefaultWidget(self.search)
        self.addAction(search_action)

        self.results = _SearchResults()
        self.results.setColumnHidden(1, True)  # no categories here
        self.results.picked.connect(self._pick)
        self._results_action = QtWidgets.QWidgetAction(self)
        self._results_action.setDefaultWidget(self.results)
        self.addAction(self._results_action)

        self._filter("")
        # Sized once for the full list, so the menu keeps its width while filtering, and
        # the name fills the row so the highlight does too.
        self._width = self.results.width()
        self.results.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        if current in items:
            item = self.results.topLevelItem(items.index(current))
            self.results.setCurrentItem(item)
            self.results.scrollToItem(item, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        shown = [item for item in self._items if all(w in item.label.lower() for w in words)]
        self.results.show_entries(shown, self.VISIBLE_ROWS)
        if self._width is not None:
            self.results.setFixedWidth(self._width)
        # A QMenu measures a widget action once and caches it; changing the action's
        # text is what makes it measure again after the list changes size.
        self._results_action.setText(f"{len(shown)}:{self.results.height()}")

    def _pick(self, entry) -> None:
        if entry is None:
            return
        self.close()
        self.picked.emit(entry.label)

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QtCore.QEvent.Type.KeyPress:
            key = event.key()
            if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                self._pick(self.results.current_entry())
                return True
            if key in self._NAV_KEYS:
                self.results.step(key)
                return True
        return super().eventFilter(obj, event)


class FilterableChunkMenu(QtWidgets.QMenu):
    """The chunk-creation menu: a category tree, and a ranked list while searching.

    Typing swaps the tree for one scrollable list ordered by
    chunk_registry.search_menu_entries, closest match first and already highlighted, so
    "trans" + Enter creates Transform.
    """

    #: Results kept; the rest of a broad search (a category name matches every chunk in it)
    #: is a scroll away rather than cut off.
    MAX_RESULTS = 200

    #: Result rows on screen at once; more scroll.
    VISIBLE_RESULTS = 12

    _NAV_KEYS = (
        QtCore.Qt.Key.Key_Down,
        QtCore.Qt.Key.Key_Up,
        QtCore.Qt.Key.Key_PageDown,
        QtCore.Qt.Key.Key_PageUp,
    )

    def __init__(self):
        super().__init__("Chunks")
        self.filter_widget = QtWidgets.QLineEdit(placeholderText="Search...")
        self.filter_widget.textChanged.connect(self._filter_actions)
        self.filter_widget.installEventFilter(self)
        self.results = _SearchResults()
        self.results.picked.connect(self._pick)
        self._entries: list = []
        self._tree_actions: list = []
        self._results_action = None
        self._no_results = None
        self._picked = None

    def _pick(self, entry) -> None:
        """Take entry as the answer and close; exec_ then returns no action."""
        if entry is None:
            return
        self._picked = entry
        self.close()

    def _filter_actions(self, text: str):
        ranked = (
            chunk_registry.search_menu_entries(self._entries, text)[: self.MAX_RESULTS]
            if text.strip() else None
        )
        searching = ranked is not None
        for action in self._tree_actions:
            action.setVisible(not searching)
        if searching:
            self.results.show_entries(ranked, self.VISIBLE_RESULTS)
        if self._no_results is not None:
            self._no_results.setVisible(searching and not ranked)
        if self._results_action is not None:
            self._results_action.setVisible(bool(ranked))
            # An open QMenu shows a widget action's widget when the action comes back, but
            # does not hide it when the action goes: the old list stayed drawn under the
            # category tree after the search was deleted.
            self.results.setVisible(bool(ranked))
            # A QMenu measures a widget action once and caches it; changing the action's
            # text is what makes it measure again after the list changes size.
            self._results_action.setText(f"{len(ranked or ())}:{self.results.height()}")

    def eventFilter(self, obj, event):
        if obj is self.filter_widget:
            if event.type() == event.Type.KeyPress:
                key = event.key()
                if key == QtCore.Qt.Key.Key_Escape:
                    return True
                searching = bool(self.filter_widget.text().strip())
                if searching and key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                    self._pick(self.results.current_entry())
                    return True
                if searching and key in self._NAV_KEYS:
                    self.results.step(key)
                    return True
                if key in (
                    QtCore.Qt.Key.Key_Return,
                    QtCore.Qt.Key.Key_Down,
                    QtCore.Qt.Key.Key_Up,
                ):
                    QtWidgets.QApplication.sendEvent(self, event)
                    return True
            return super().eventFilter(obj, event)

        # App-level filter: redirect typing from any open submenu back to the search box
        if isinstance(obj, QtWidgets.QMenu) and event.type() == QtCore.QEvent.Type.KeyPress:
            key = event.key()
            text = event.text()
            if text and text.isprintable() and not text.isspace():
                self.filter_widget.insert(text)
                return True
            if key == QtCore.Qt.Key.Key_Backspace:
                current = self.filter_widget.text()
                if current:
                    self.filter_widget.setText(current[:-1])
                return True

        return False

    def select_module(self, pos):
        """Pop the chunk menu at pos and return what the user picked.

        Renders chunk_registry.get_menu_entries() and ranks searches with
        chunk_registry.search_menu_entries -- the entries, their nesting and the ranking are
        all core, so this only turns them into QMenus and QActions.

        Args:
            pos (QtCore.QPoint): Global position to pop the menu at

        Returns:
            Optional[chunk_registry.MenuEntry]: The chosen entry, or None if dismissed or
            if the user picked "import" (which is handled here).
        """
        items = dict()
        items["menu"] = self
        self.setToolTipsVisible(True)

        filter_action = QtWidgets.QWidgetAction(items["menu"])
        filter_action.setDefaultWidget(self.filter_widget)

        items["menu"].addAction(filter_action)
        items["menu"].addSeparator()

        # Search results, hidden until something is typed.
        self._results_action = QtWidgets.QWidgetAction(self)
        self._results_action.setDefaultWidget(self.results)
        self._results_action.setVisible(False)
        self.addAction(self._results_action)
        self._no_results = self.addAction("No matching chunks")
        self._no_results.setEnabled(False)
        self._no_results.setVisible(False)

        self._entries = chunk_registry.ChunkRegistry().get_menu_entries(sort_result=True)
        for entry in self._entries:
            namespace = list(entry.namespace)
            tooltip = entry.tooltip

            d = items
            last = len(namespace) - 1
            for i, ns in enumerate(namespace):
                if i == last:
                    if m := d.get("menu"):
                        action = m.addAction(ns)
                        action.setData(entry)
                        action.setToolTip(tooltip)
                        if m is self:
                            self._tree_actions.append(action)
                    break
                elif ns not in d:
                    submenu = d.get("menu").addMenu(ns)
                    submenu.setToolTipsVisible(True)
                    if d is items:
                        self._tree_actions.append(submenu.menuAction())
                    d[ns] = {"menu": submenu}
                d = d[ns]

        self.addSeparator()

        import_action = QtGui.QAction("import")
        import_action.setData("import")
        self.addAction(import_action)

        self.filter_widget.setFocus()
        QtWidgets.QApplication.instance().installEventFilter(self)
        exc = self.exec_(pos)
        QtWidgets.QApplication.instance().removeEventFilter(self)

        if self._picked is not None:
            return self._picked

        if not exc:
            return

        if exc.data() == "import":
            if file := file_dial("*" + chunk_registry.PY_MODULE_EXT):
                chunk_registry.ChunkRegistry().import_chunk(file)
            return

        if not (entry := exc.data()) or not isinstance(entry, chunk_registry.MenuEntry):
            return

        return entry


def file_dial(name_filter: str | None = None, create_new=False, directory=False):
    if directory:
        return QtWidgets.QFileDialog.getExistingDirectory(None, "Open Directory", "")

    _filter = ""
    if name_filter:
        _filter = ";;".join(name_filter.split())
    if create_new:
        res = QtWidgets.QFileDialog.getSaveFileName(None, "Save File", "", _filter, "")
    else:
        res = QtWidgets.QFileDialog.getOpenFileName(None, "Open File", "", _filter, "")

    return res[0]


class ChunkContextBreadcrumb(QtWidgets.QWidget):
    """The path from the scene's root group down to the one being looked at.

    Tabs here are the groups you have OPENED, in the order you opened them -- browser tabs,
    not a tree. That is what makes them fast to hit and also what makes them silent about
    hierarchy: dive straight into a nested group from the graph and its parent may not be
    open at all, so no amount of arranging the tab row can show where you are. A path can,
    because it is not a set: it has direction, it is as long as the nesting is deep rather
    than as long as the session, and it leaves the tabs where muscle memory left them.

    Every entry is clickable, including one whose group has no tab open -- clicking it dives
    there and opens one. A path you can only partly click is the thing that makes context
    navigation irritating elsewhere.
    """

    #: Emitted with the context chunk the user clicked.
    navigate = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(6, 2, 6, 2)
        self._layout.setSpacing(3)
        # Sized by what it holds. It floats over the graph canvas rather than living in a
        # strip, so nothing stretches it and nothing clips it -- a strip has to be given a
        # height, and a height that suits one font cuts the descenders off another.
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        # Nothing but the text. A plate under it read as a panel sitting on the graph --
        # one more piece of furniture -- where the path is meant to be a label on the canvas
        # itself. The buttons are auto-raise, so they stay flat until hovered, and the
        # explicit transparency keeps a host stylesheet from painting one back in.
        self.setObjectName("chunkContextPath")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(
            "QWidget#chunkContextPath { background: transparent; }"
            "QWidget#chunkContextPath QToolButton { background: transparent; border: none; }"
            "QWidget#chunkContextPath QLabel { background: transparent; }"
        )

    def showEvent(self, event):
        """Take the size the path needs, once there is something laid out to measure.

        A layout skips hidden widgets, and a child created while its parent is not yet
        visible is hidden -- so sizing at set_context time measured the margins alone and
        left a 12x4 plate holding four buttons nobody could see. Being shown is the first
        moment the hint is real.
        """
        super().showEvent(event)
        self.resize(self.sizeHint())

    def set_context(self, ctx) -> None:
        """Rebuild the row for ctx, root first."""
        while (item := self._layout.takeAt(0)) is not None:
            if (w := item.widget()) is not None:
                # Hidden, never unparented. deleteLater() waits for the event loop it was
                # called from, so a popup opened straight after (the empty-panel Tab and
                # file-drop paths) runs its nested loop with the old button still alive --
                # and unparented it would be a top-level window, which Windows shows and
                # activates, closing the popup the instant it appears.
                w.hide()
                w.deleteLater()

        entries = context_ancestry(ctx)
        # Nothing to say at the root of an empty scene, and an empty plate floating in the
        # corner is worse than no plate.
        self.setVisible(bool(entries))
        for i, node in enumerate(entries):
            if i:
                sep = QtWidgets.QLabel("›", self)  # a single ›, not a > that reads as markup
                sep.setEnabled(False)
                # A touch larger than the names it divides: at the same size the glyph is
                # thin enough to read as punctuation inside a word rather than as a step
                # between two places.
                sep_font = sep.font()
                sep_font.setPointSizeF(sep_font.pointSizeF() + 2)
                sep.setFont(sep_font)
                sep.show()
                self._layout.addWidget(sep)
            entry = self._entry(node, is_current=node is ctx)
            # Shown explicitly. These are built into an already-visible parent, and a child
            # that is never shown is skipped by the layout -- which is how the plate ended
            # up 12x4 with a full path inside it.
            entry.show()
            self._layout.addWidget(entry)
        # No trailing stretch: that belonged to a full-width row. A floating plate is as
        # wide as its path, and a stretch would make it as wide as the graph.
        #
        # Activated before measuring, or adjustSize() asks a layout that has not laid
        # anything out yet and gets the margins alone -- a 12x4 plate with four invisible
        # buttons in it.
        self._layout.activate()
        # resize(sizeHint()), not adjustSize(): adjustSize leaves a child of a plain
        # (layout-less) parent at whatever size it already had, so the plate stayed at its
        # margins -- 12x4 -- while reporting a 669x26 hint and holding four buttons nobody
        # could see.
        self.resize(self.sizeHint())

    def _entry(self, node, is_current: bool) -> QtWidgets.QWidget:
        button = QtWidgets.QToolButton(self)
        button.setText(node.name)
        button.setAutoRaise(True)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        font = button.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() - 1))
        # The one you are in is stated, not offered: bold and inert, so the row reads as a
        # position rather than as four things to press.
        font.setBold(is_current)
        button.setFont(font)
        if is_current:
            button.setEnabled(False)
        else:
            button.clicked.connect(lambda _=False, n=node: self.navigate.emit(n))
        return button


def context_ancestry(ctx) -> list:
    """Ctx and everything it is nested inside, outermost first.

    Walks _parent_context, which is what actually records the nesting -- get_context()
    returns self for a context chunk and so says nothing about where it sits. Guarded
    against a cycle: prim nesting should prevent one, but a path widget is a poor place to
    discover otherwise.
    """
    chain: list = []
    seen: set = set()
    node = ctx
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        # A context can be walked while it is still being built, before it has been named.
        # There is nothing to label such an entry with and nothing useful to click, and the
        # row is refreshed again when the page is shown -- so leave it out rather than draw
        # a blank button that navigates somewhere unnameable.
        if getattr(node, "name", ""):
            chain.append(node)
        parent_ref = getattr(node, "_parent_context", None)
        node = parent_ref() if parent_ref else None
    chain.reverse()
    return chain
