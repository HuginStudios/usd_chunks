from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Optional, Generic

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_resources, chunk_utils
import usd_chunks.chunk_flags
from usd_chunks.chunk_core import ChunkParamWidgetBase
from usd_chunks import chunk_core
from usd_chunks.plugins import chunk_usd

from functools import partial
from usd_chunks.chunk_flags import ChunkFlags

# Re-exported under the original private name: chunk_qt imports it from here, and the
# implementation is core (frontend-agnostic), not Qt.
_clean_description = chunk_registry.clean_description

# Background tint for a tab's Params-page header panel (prim path field, lofted own-params,
# filter bar) — distinct from the body's #333333 (CHUNK_STYLESHEET's default QFrame color).
_PARAMS_HEADER_BG = "#242424"


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
"""

# Marks a widget that carries CHUNK_STYLESHEET, so nothing below it applies it again.
_SHEET_FLAG = "_chunk_sheet_applied"
# Marks a class that always styles itself, so a descendant can skip *before* the ancestor
# has actually got round to it. ChunkWidget applies the sheet at the end of its own
# __init__, which is after its children have been built and asked this question.
_SELF_STYLING = "_chunk_styles_self"


def apply_chunk_stylesheet(widget: QtWidgets.QWidget) -> bool:
    """
    Give widget CHUNK_STYLESHEET, unless something above it already covers it.

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
    Qt's normal behavior untouched (including click-to-jump on press)."""

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
        self.setValue(round(self._drag_value))
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._fine_dragging:
            self._fine_dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SpinSlider(QtWidgets.QWidget):
    def __init__(self, parent=None, is_float=False):
        super().__init__(parent)
        self._step = 1000 if is_float else 1

        if is_float:
            self.spin_box = QtWidgets.QDoubleSpinBox()
            self.spin_box.setDecimals(2)
            self.spin_box.setSingleStep(1.0 / self._step)
            self.spin_box.setLocale(QtCore.QLocale.c())
            self.spin_box.setRange(-1e300, 1e300)
        else:
            self.spin_box = QtWidgets.QSpinBox()
            self.spin_box.setRange(-2147483648, 2147483647)
        self.spin_box.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
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

        min_val = param.min_value
        max_val = param.max_value
        min_int = int(min_val * self._step) if min_val is not None else 100 * (1 - self._step)
        max_int = int(max_val * self._step) if max_val is not None else 100 * self._step
        self.setRange(min_int, max_int)

        self.setup_chunk_widget()
        self.slider.valueChanged.connect(self.value_changed)
        self.spin_box.valueChanged.connect(lambda v: self.value_changed(int(v * self._step)))

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

    def value_changed(self, value: Optional[int | float] = None):
        if not self.param or value is None:
            return
        result = value / float(self._step)
        # The step division makes a float of everything, so an `is_int` param was handing back
        # e.g. 3.0 -- fine until a chunk does range() or a list index with it. Reported as
        # Scatter's Levels crashing with "'float' object cannot be interpreted as an integer"
        # the moment the spinbox was touched; the param says it is an int, so it delivers one.
        if getattr(self.param, "is_int", False):
            result = int(result)
        super().value_changed(result)

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val)


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
        self.setEnabled(val)


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
        self.setEnabled(val)


class ChunkLabelWidget(ChunkParamWidgetBase[chunk.Label], QtWidgets.QLabel):
    def __init__(self, param: chunk.Label):
        super().__init__(param)
        self.setup_chunk_widget()

    def get_value(self):
        return self.text()

    def set_value(self, value):
        return self.set_label(value)

    def set_label(self, text):
        return self.setText(text)

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val)


class ChunkLineEditWidget(ChunkParamWidgetBase[chunk.LineEdit], QtWidgets.QWidget):
    def __init__(self, param: chunk.LineEdit):
        super().__init__(param)

        self.button = QtWidgets.QToolButton(self)
        self.button.setText("Pin Selection")
        self.button.setIcon(QtGui.QIcon(str(chunk_resources.get_icon("pin"))))
        self.button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)

        self.button.clicked.connect(self.pin_prim)

        self.text = QtWidgets.QLineEdit(self)
        self.text.editingFinished.connect(self.value_changed)
        self.main_layout = QtWidgets.QHBoxLayout()

        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.text)
        self.main_layout.addWidget(self.button)
        self.setLayout(self.main_layout)

        self.button.setHidden(not self.param.has_flag(ChunkFlags.ParamFlags.PRIM_SELECTION))

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
        # row's proxy widget gets created) — apply current state explicitly, once, now.
        self.update_from_state()

    def handle_param_changed(self, notice: chunk_notice.AttributeChangeNotice, sender: chunk.ChunkParamBase):
        if notice.alias == chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED and notice.data.value:
            self.button.setHidden(ChunkFlags.ParamFlags.PRIM_SELECTION not in notice.data.value)
        return super().handle_param_changed(notice, sender)

    def pin_prim(self):
        scene = self.param.get_scene()
        if not scene:
            return
        prims = scene.get_selected_prims()
        # Nothing selected is a misfire, not an instruction to erase. Pinning used to blank the
        # field, which is a bad trade: the path you had was worth more than the empty selection.
        if not prims:
            return
        # Assigned through the param rather than calling its set_prims(). Inside a ListGroup row
        # `self.param` is a ProxyParam: attribute *assignment* is proxied to that row, but a
        # method looked up on it comes back bound to the shared template, so set_prims() wrote
        # the template's default and left the row alone. Reported as the pin icon doing nothing
        # on a prototype row while typing into the same field worked.
        self.param.value = ",".join(str(p.GetPath()) for p in prims)

    def get_value(self):
        return self.text.text()

    def set_value(self, value):
        return self.text.setText(str(value))

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val)

    def update_from_state(self):
        # Base ChunkParamWidgetBase.update_from_state is a no-op stub with no default
        # implementation — without this override, DISABLED never actually renders as
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
        self.setEnabled(val)


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

        pattern = ""
        if self.param.pattern:
            pattern = self.param.pattern

        if not pattern:
            pattern = "*"

        if self.param.extensions:
            pattern = " ".join([pattern + x for x in self.param.extensions])

        directory = self.param.has_flag(ChunkFlags.ParamFlags.DIRECTORY)
        # FILE_EXISTING unset (the default) means the file need not exist yet — an
        # output/destination picker like Export ChunkGroup's Output File. Previously
        # this only applied to DIRECTORY params; a plain FILE param always got
        # create_new=False (an Open dialog requiring the file to already exist),
        # regardless of the flag. file_dial() ignores create_new for directory=True
        # anyway (always getExistingDirectory), so computing it unconditionally here
        # only changes behavior for the FILE case.
        create_new = not self.param.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING)

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
        self.setEnabled(val)


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
        # param model — clobbering the real value — before set_value() below even runs.
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
            # param.raw_value — for a node-graph-connected Menu that's the literal
            # "@Node.param@" expression text, not a menu_items entry. Resolve it for
            # display; menu_items themselves are never expression strings.
            value = self.param.value
        with QtCore.QSignalBlocker(self):
            index = self.findText(value)
            if index < 0 and value:
                # A resolved connection value (e.g. a Create Layer chunk's Layer
                # Name) is a plain name, not one of the raw identifiers/sentinels in
                # menu_items — setCurrentText() would silently no-op on a
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
        # QComboBox.showPopup() can't use the proxy-aware popup path for widgets
        # that are deep descendants of a QGraphicsProxyWidget (graphicsProxyWidget()
        # returns None for non-direct embeds).  Replace with a QMenu so the popup
        # always appears at the correct screen position.
        QtCore.QTimer.singleShot(0, self, self._show_as_menu)

    def _show_as_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        current = self.currentText()
        for i in range(self.count()):
            text = self.itemText(i)
            act = menu.addAction(text)
            if text == current:
                act.setCheckable(True)
                act.setChecked(True)
        # Use popup() (non-modal), not exec_(). exec_ runs a nested event loop in which a
        # deferred node-graph rebuild can destroy this proxied combobox mid-selection; exec_'s
        # result would then be applied to a freed C++ widget → silent crash. With popup(), if
        # the widget is destroyed the menu (its child) goes with it and never fires triggered.
        menu.triggered.connect(self._menu_item_chosen)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.mapToGlobal(QtCore.QPoint(0, self.height())))

    def _menu_item_chosen(self, action) -> None:
        self.setCurrentText(action.text())

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val)


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
        self.setEnabled(val)


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
        self.setEnabled(val)


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
        self.setEnabled(val)


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
        self.setEnabled(val)

    def update_from_state(self):
        self.set_hidden(self.param.has_state(ChunkFlags.State.HIDDEN))
        is_output = self.param.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        self.set_enabled(not is_output and not self.param.has_state(ChunkFlags.State.DISABLED))

    def _dialog_parent(self):
        """A real top-level window to hang the colour dialog off.

        On the Params panel window() is the usdview window and everything is normal. On
        the graph this widget is embedded in a QGraphicsProxyWidget — reparented to None
        and flagged WA_DontShowOnScreen — so window() returns the swatch itself, and a
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


class ChunkQTWidgetFactory(chunk_core.ChunkWidgetFactoryBase):
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
        widget_class = self.get_map().get(key, None)

        if not widget_class:
            print("Could not create widget for param: ", param.name, "Missing mapping in Qt factory")
            return None

        widget: ChunkParamWidgetBase = widget_class(param)

        # A widget with no parent is a top-level window, so anything that makes it visible
        # before it reaches a layout shows a real window on screen — which is what a chunk
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
        self.label = QtWidgets.QLabel()
        self.label.setHidden(True)

        self.vlayout.addWidget(self.label)
        self.vlayout.addWidget(widget)
        self.setLayout(self.vlayout)

        # Built on first hover, not here. A Qt.ToolTip label is a top-level window, and
        # asking for one costs ~0.2ms even offscreen -- trivial per row, but a graph of
        # nodes builds one LabeledWidget per param, so a 49-node network paid for ~1300
        # tooltip windows at load, nearly all of which are never hovered.
        self._value_tooltip: Optional[QtWidgets.QLabel] = None
        self._tooltip_timer: Optional[QtCore.QTimer] = None
        self._show_timer: Optional[QtCore.QTimer] = None

        if isinstance(self.widget, ChunkParamWidgetBase):
            self._listener = chunk_notice.NoticeConnection(
                self.update_value, alias=self.widget.get_notice_alias(), sender=self.widget
            )

        # Hiding a param only hides its inner widget (see ChunkParamWidgetBase.set_hidden /
        # ChunkContainerWidgetBase.set_hidden) — this row (label + widget) must react to the
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
        self.label.installEventFilter(self)

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
        elif name == "value" and self.should_show_tooltip():
            self.show_tooltip()

    def update_label(self, text):
        self.label.setHidden(not bool(text))
        self.label.setText(text)

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

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        if not isinstance(self.widget, ChunkParamWidgetBase):
            event.ignore()
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
        has_expr = is_expr_capable and isinstance(raw, str) and chunk_utils.has_expression(raw)

        menu = QtWidgets.QMenu()
        value_rect = QtCore.QRect(self.widget.pos(), self.widget.size())
        loft_action = menu.addAction("Loft up") if context and not value_rect.contains(event.pos()) else None
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
                chunk_usd.loft_param_to_context(param, context)
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


class ChunkArea(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.flow = QtWidgets.QVBoxLayout()
        self.flow.setContentsMargins(6, 0, 10, 0)
        self.flow.setSpacing(40)
        self.flow.setContentsMargins(6, 0, 10, 0)
        self.flow.setSpacing(40)
        self.flow.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.flow.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)

        self.flow.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)

        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.widget_holder = QtWidgets.QWidget(self)
        self.widget_holder.setLayout(self.flow)

        self.setWidget(self.widget_holder)
        self.setWidgetResizable(True)

    def add_widget(self, w):
        self.flow.addWidget(w)

    def delete_chunks(self):
        # Deletes all chunks in this ScrollArea. Currently undoable: TODO: add undo/redo
        for i in range(self.flow.count()):
            if item := self.flow.itemAt(i):
                w = item.widget()
                if isinstance(w, ChunkWidget):
                    w.group.request_delete()


class ContainerWidget(
    Generic[chunk_core.GroupT], chunk_core.ChunkContainerWidgetBase[chunk_core.GroupT], QtWidgets.QFrame
):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    group_height_changed = QtCore.Signal(float)

    def __init__(
        self,
        group,
        parent=None,
    ):
        self.animation_duration = 25
        self._animation_value = 0
        self._anim_target_h: Optional[int] = None  # content height, measured per animation
        self._depth = 0
        super().__init__(group, parent=parent)
        # When Qt destroys this widget — e.g. the node graph rebuild calls deleteLater on the
        # embedded ChunkWidget without going through delete_self — revoke our notice
        # subscriptions. Otherwise a later notice (a rename firing _name_changed → update_label,
        # a state change, …) invokes a method on the deleted C++ object and crashes. Safe post-
        # delete: _disconnect_callbacks only touches Python NoticeConnections.
        self.destroyed.connect(lambda *_: self._disconnect_callbacks())

    def _is_context_tab(self) -> bool:
        """True when this widget is the scrollable tab context (not a compact node)."""
        if not self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            return False
        if getattr(self, '_is_tab_context_widget', False):
            return True  # explicitly opened as a tab — always use scroll area
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

        self.list_expand_button = QtWidgets.QToolButton()
        self.list_expand_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.list_expand_button.setIconSize(QtCore.QSize(6, 6))
        self.list_expand_button.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum
        )
        self.list_expand_button.setHidden(True)

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
        self.__layout__.addWidget(self.list_expand_button)
        self.__layout__.addWidget(self.content)

        self.setLayout(self.__layout__)
        self._update_group_depth()

    _GROUP_COLORS = ("#303030", "#333333")

    def _update_group_depth(self):
        depth = len(self.group.get_identifier().split("."))
        bg = self._GROUP_COLORS[depth % len(self._GROUP_COLORS)]
        # Two setStyleSheet calls per container, each re-polishing its whole subtree —
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

    def get_widget_factory(self) -> chunk_core.ChunkWidgetFactoryBase:
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

        if usd_chunks.chunk_flags.ChunkFlags.GroupFlags.LIST in self.group.flags:
            self.list_expand_button.setHidden(False)

            icon = QtGui.QIcon(str(chunk_resources.get_icon("add")))
            self.list_expand_button.setIcon(icon)
            self.list_expand_button.setIconSize(QtCore.QSize(20, 20))
            self.list_expand_button.clicked.connect(self.list_expand)

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
            # Qt shows it immediately, before deleteLater() actually runs — set this first
            # or a still-visible widget flashes a native window on its way out.
            self.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
            self.setParent(None)
        except RuntimeError:
            # C++ object already destroyed (e.g. by QGraphicsProxyWidget teardown)
            pass

    def remove_widget(self, widget: QtWidgets.QWidget):
        try:
            count = self.content_layout.count()
            for i in reversed(range(count)):
                item = self.content_layout.itemAt(i)
                if not item:
                    continue
                iw = item.widget()

                if isinstance(iw, LabeledWidget) and iw.widget == widget:
                    self.content_layout.removeItem(item)
                    # Tear the wrapper down, not just its layout entry. Otherwise the
                    # LabeledWidget stays a child of this container, keeping a strong ref
                    # to the param widget -> chunk, so the chunk never GCs and its layer's
                    # weakref.finalize (which removes the layer) never fires.
                    # removeItem() only drops layout management — iw is still a visible
                    # child of self, so reparenting it to none would flash a real top-level
                    # window unless suppressed first.
                    iw.setAttribute(QtCore.Qt.WA_DontShowOnScreen)
                    iw.setParent(None)
                    iw.deleteLater()
                    break
        except RuntimeError:
            # This container's own C++ layout/widget was already torn down (e.g. the whole
            # ChunkWidget was deleted via _group_deleted while a sibling's PARAM_REMOVED was
            # still propagating here during a tab-delete cascade). Nothing to remove — the
            # parent teardown handles the children. Swallowing this lets the cascade finish
            # and reach super().request_delete()/CHUNK_DELETED, so the tab actually closes.
            pass

    def add_widget(self, widget: QtWidgets.QWidget, label=None):  # type: ignore
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)

        labeled = LabeledWidget(widget, self)
        # Now parented, so showing it can no longer produce a top-level window — lift the
        # suppression create_widget put on it.
        widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen, False)
        widget.installEventFilter(self)
        labeled.label.installEventFilter(self)

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
        elif isinstance(widget, chunk_core.ChunkParamWidgetBase):
            widget.param.request_delete()
        self.remove_widget(widget)


_VIEW_COL_HEADER = "#c87830"


class ViewSlotState(QtCore.QObject):
    """Shared view-slot state for a context tab.

    Holds the active view slot (0 = none, 1–3 = active) and notifies connected
    widgets when it changes.  The actual blocking/muting logic lives in
    ChunkGraphView; this object is the lightweight communication bus between
    the nodegraph and the params panel.
    """

    slot_changed = QtCore.Signal(int)   # 0 = off, 1–3 = active slot
    create_view_for = QtCore.Signal(str)  # prim path — handled by ChunkGraphView
    pin_changed = QtCore.Signal(object, bool)  # (chunk, pinned) — a Pin/Unpin menu toggle

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
        stub this plain instance method instead of driving a real modal popup."""
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
            from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView
            ChunkGraphView.mark_pending_delete_relevance(list(targets))

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
        grp: "chunk.ChunkBase",
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
        pin_action = menu.addAction(
            _label("Unpin from Params" if chunk_usd.is_chunk_pinned(grp) else "Pin to Params")
        )
        copy_action = cut_action = duplicate_action = None
        if graph_view is not None:
            menu.addSeparator()
            copy_action = menu.addAction("Copy")
            cut_action = menu.addAction("Cut")
            duplicate_action = menu.addAction("Duplicate")
        menu.addSeparator()

        stage: Optional[Usd.Stage] = grp.get_stage()
        is_view_chunk = False
        existing_view_slot: Optional[int] = None
        if stage:
            prim = stage.GetPrimAtPath(prim_path)
            if prim.IsValid():
                is_view_chunk = prim.GetAttribute("info:chunkType").Get() == "view"
            if not is_view_chunk:
                for p in stage.Traverse():
                    if p.GetAttribute("info:chunkType").Get() != "view":
                        continue
                    exec_in = p.GetAttribute("inputs:chunk")
                    if exec_in.IsValid():
                        for conn_path in exec_in.GetConnections():
                            if str(conn_path.GetPrimPath()) == prim_path:
                                existing_view_slot = p.GetAttribute("view:slot").Get()

        if existing_view_slot is not None:
            view_action = menu.addAction(f"Activate View {existing_view_slot}")
        elif is_view_chunk:
            view_action = None
        else:
            view_action = menu.addAction("Set View")

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
        elif res == pin_action:
            # The clicked chunk decides the direction, so a mixed selection ends up in one
            # state rather than each chunk flipping to the opposite of whatever it was.
            new_pinned = not chunk_usd.is_chunk_pinned(grp)
            for target in targets:
                chunk_usd.set_chunk_pinned(target, new_pinned)
                self.pin_changed.emit(target, new_pinned)
        elif view_action and res == view_action:
            if existing_view_slot is not None:
                self.set_slot(existing_view_slot)
            else:
                self.create_view_for.emit(prim_path)
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
                from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView
                ChunkGraphView.mark_pending_delete_relevance([grp])
            conn.handle_callback(
                chunk_notice.MenuActionNotice(grp.get_notice_data()), grp, False
            )


class ParamsFilterBar(QtWidgets.QWidget):
    """Per-tab filter bar shown at the top of a context's Params page: a pin-filter toggle plus
    a fuzzy search box with a Name/Type/Both scope combobox — two independent, AND'd filters
    over the same Params page (see chunk_usd.chunk_matches_search /
    ChunkWidget._is_hidden_by_params_filter).
    """

    pin_filter_toggled = QtCore.Signal(bool)
    search_changed = QtCore.Signal(str, str)  # (query, scope)

    _SCOPE_ITEMS = (("Both", "both"), ("Name", "name"), ("Type", "type"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self.pin_filter_checkbox = QtWidgets.QCheckBox("Pinned only")
        self.pin_filter_checkbox.toggled.connect(self.pin_filter_toggled)
        layout.addWidget(self.pin_filter_checkbox)

        self.search_box = QtWidgets.QLineEdit(placeholderText="Search...")
        self.search_box.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_box, 1)

        self.scope_combo = QtWidgets.QComboBox()
        for label, value in self._SCOPE_ITEMS:
            self.scope_combo.addItem(label, value)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        layout.addWidget(self.scope_combo)

    def set_pin_filter_checked(self, checked: bool) -> None:
        self.pin_filter_checkbox.blockSignals(True)
        self.pin_filter_checkbox.setChecked(checked)
        self.pin_filter_checkbox.blockSignals(False)

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


class ChunkWidget(ContainerWidget[chunk.ChunkBase]):
    """
    Chunk is a base class for creating UI components.
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
        self._view_state: Optional[ViewSlotState] = None
        self._view_overlay: Optional[QtWidgets.QLabel] = None
        super().__init__(group, parent)

        self.installEventFilter(self)
        if is_tab_context:
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def set_view_state(self, vs: "ViewSlotState") -> None:
        if self._view_state is not None:
            self._view_state.slot_changed.disconnect(self._update_view_overlay)
            self._view_state.pin_changed.disconnect(self._on_pin_changed)
        self._view_state = vs
        vs.slot_changed.connect(self._update_view_overlay)
        vs.pin_changed.connect(self._on_pin_changed)
        if self._is_tab_context_widget:
            self._ensure_view_overlay()
            self._update_view_overlay(vs.active_slot)

    def _on_pin_filter_toggled(self, enabled: bool) -> None:
        """The filter bar's pin-filter checkbox changed for this tab. Unlike a single chunk's
        pin toggle, this can change visibility for every chunk in the tab at once."""
        chunk_usd.set_pin_filter_enabled(self.group, enabled)
        self._reconcile_all_children()

    def _on_search_changed(self, query: str, scope: str) -> None:
        """The filter bar's search box or a scope chip changed for this tab — live, on every
        keystroke, same as the create-menu's FilterableChunkMenu search. Can change visibility
        for every chunk in the tab at once."""
        chunk_usd.set_search_query(self.group, query)
        chunk_usd.set_search_scope(self.group, scope)
        self._reconcile_all_children()

    def _on_pin_changed(self, target_chunk: "chunk.ChunkBase", pinned: bool) -> None:
        """A chunk's pin state was toggled from the shared menu (graph or Params surface).

        Only the tab-context widget owning target_chunk's Params page reacts — every other
        ChunkWidget either has no _view_state set (never connected) or belongs to a different
        tab's ViewSlotState instance. Reconciles rather than trusting `pinned` directly: if this
        tab's pin filter is currently OFF, the chunk should stay visible either way.
        """
        if not self._is_tab_context_widget:
            return
        self._reconcile_chunk_visibility(target_chunk)

    def _reconcile_chunk_visibility(self, target_chunk: "chunk.ChunkBase") -> None:
        """Show or hide target_chunk in this tab's Params page to match the current combined
        filter state (right now just the pin filter; issue #06 adds search on top). Used both
        for a single chunk's pin toggle and, looped over every child, for a filter-bar change
        that can affect many chunks in the tab at once."""
        hidden = self._is_hidden_by_params_filter(target_chunk)
        shown = target_chunk.get_hash() in self.group_widgets
        if hidden and shown:
            self._hide_chunk_widget(target_chunk)
        elif not hidden and not shown:
            self._show_chunk_widget(target_chunk)

    def _reconcile_all_children(self) -> None:
        """Re-apply the current filter state to every direct child chunk of this tab context.
        Call after a filter-bar change (e.g. the pin-filter toggle) — unlike a single chunk's
        pin toggle, that can change visibility for many chunks in the tab at once."""
        if not self._is_tab_context_widget:
            return
        for child in self.group.get_my_params():
            if isinstance(child, chunk.ChunkBase):
                self._reconcile_chunk_visibility(child)

    def _show_chunk_widget(self, target_chunk: "chunk.ChunkBase") -> None:
        if target_chunk.get_hash() in self.group_widgets:
            return  # already shown
        if chunk_usd.is_group_chunk(target_chunk):
            self._add_group_widget(target_chunk)
        else:
            self._add_group_layout(target_chunk)

    def _hide_chunk_widget(self, target_chunk: "chunk.ChunkBase") -> None:
        h = target_chunk.get_hash()
        w = self.group_widgets.get(h)
        if w is None:
            return
        self.remove_widget(w)
        try:
            del self.group_widgets[h]
        except KeyError:
            pass

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
            text = "View: –"
            self._view_overlay.setStyleSheet(
                "background: rgba(40,40,40,180); color: rgb(130,130,130);"
                "border-radius: 4px; padding: 3px 8px;"
            )
        else:
            text = f"View: {slot}"
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
            if event.key() in (
                QtCore.Qt.Key.Key_1,
                QtCore.Qt.Key.Key_2,
                QtCore.Qt.Key.Key_3,
            ):
                slot = event.key() - QtCore.Qt.Key.Key_0
                cur = self._view_state.active_slot
                self._view_state.set_slot(0 if cur == slot else slot)
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

            if self._is_tab_context_widget:
                # A distinct "toolbar" panel — prim path, a context's own lofted top-level
                # params (e.g. scripts/chunkgroup.py's "doc" Description TextEdit — the
                # pre-existing description for a chunkgroup; empty/invisible for a plain tab,
                # which has no own params), and the filter bar — visually separated from the
                # chunk list below by a tinted background + embossed edge, not just a plain line.
                self._header_container = QtWidgets.QFrame(self)
                self._header_container.setObjectName("ParamsHeader")
                self._header_container.setStyleSheet(
                    f"QFrame#ParamsHeader {{ background-color: {_PARAMS_HEADER_BG}; }}"
                )
                header_layout = QtWidgets.QVBoxLayout(self._header_container)
                header_layout.setContentsMargins(0, 0, 0, 0)
                header_layout.setSpacing(0)

                # Both are plain QWidgets with no background of their own — set explicitly
                # rather than relying on transparency showing the panel's tint through, which
                # isn't reliable across Qt styles/palettes. WA_StyledBackground is needed too:
                # a bare QWidget's default paintEvent doesn't consult its own stylesheet for
                # background-color without it (unlike QFrame, which paints it natively).
                for w in (self._prim_path_field, self._lofted_container):
                    w.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
                    w.setStyleSheet(f"background-color: {_PARAMS_HEADER_BG};")
                header_layout.addWidget(self._prim_path_field)
                header_layout.addWidget(self._lofted_container)

                self._filter_bar = ParamsFilterBar(self)
                self._filter_bar.set_pin_filter_checked(chunk_usd.is_pin_filter_enabled(self.group))
                self._filter_bar.set_search_state(
                    chunk_usd.get_search_query(self.group), chunk_usd.get_search_scope(self.group)
                )
                self._filter_bar.pin_filter_toggled.connect(self._on_pin_filter_toggled)
                self._filter_bar.search_changed.connect(self._on_search_changed)
                header_layout.addWidget(self._filter_bar)

                self.__layout__.insertWidget(2, self._header_container)
                next_idx = 3

                # A plain QFrame HLine would be invisible here — CHUNK_STYLESHEET sets
                # `QFrame { border: none; }` globally, which strips the border Qt's frame-shape
                # painting relies on. An explicit gradient bar instead gives it a clearly visible
                # embossed edge (dark top lip fading to a bright bottom highlight).
                self._filter_bar_separator = QtWidgets.QFrame(self)
                self._filter_bar_separator.setFixedHeight(4)
                self._filter_bar_separator.setStyleSheet(
                    "background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
                    " stop:0 #0d0d0d, stop:1 #7a7a7a);"
                )
                self.__layout__.insertWidget(next_idx, self._filter_bar_separator)
                next_idx += 1
            else:
                self.__layout__.insertWidget(2, self._prim_path_field)
                self.__layout__.insertWidget(3, self._lofted_container)

        if self._is_group_chunk():
            self.minimize_button.installEventFilter(self)
            if self._is_group_chunk_node():
                # Node view: content has no sub-chunks. Zero out its height so the
                # layout never allocates space for it regardless of frame style or
                # animation side-effects.
                self.content.hide()
                self.content.setMaximumHeight(0)

    def add_widget(self, widget: QtWidgets.QWidget, label=None):
        if self._is_group_chunk_node() and not isinstance(widget, ChunkParamWidgetBase):
            # Shown as a node: suppress all non-lofted widgets.
            return
        if (self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                and isinstance(widget, ChunkParamWidgetBase)):
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)
            labeled = LabeledWidget(widget, self)
            if label is not None:
                labeled.update_label(label)
            labeled._name_hint = widget.param.name
            self._lofted_layout.addWidget(labeled)
            self._lofted_container.setVisible(True)
        else:
            super().add_widget(widget, label)

    def _is_hidden_by_params_filter(self, group_chunk) -> bool:
        """True if group_chunk should be hidden from THIS tab's Params page under the current
        combined filter state — the pin filter AND the search box, two independent filters
        that both have to pass for a chunk to show. Only meaningful when self is the
        tab-context widget — a context's own direct children are the only things this filter
        applies to, never nested groups within a chunk's own UI."""
        if not self._is_tab_context_widget:
            return False
        if not isinstance(group_chunk, chunk.ChunkBase):
            return False
        if chunk_usd.is_pin_filter_enabled(self.group) and not chunk_usd.is_chunk_pinned(group_chunk):
            return True
        query = chunk_usd.get_search_query(self.group)
        if query and not chunk_usd.chunk_matches_search(
            group_chunk, query, chunk_usd.get_search_scope(self.group)
        ):
            return True
        return False

    def _add_group_layout(self, group):
        if self._is_group_chunk_node():
            # Don't create orphan Qt child widgets — they render at (0,0) as overlays.
            return None
        if (isinstance(group, chunk.ChunkBase)
                and group.has_flag(ChunkFlags.GroupFlags.CONTEXT)
                and (cfg := group.get_config()) and cfg.module):
            # ChunkGroup nodes are rendered via _add_group_widget (triggered by CHUNK_LOADED).
            # Suppress the normal nested-layout path to avoid a duplicate widget.
            return None
        if self._is_hidden_by_params_filter(group):
            return None
        return super()._add_group_layout(group)

    def _build_param_widgets(self, group):
        if self._is_group_chunk_node():
            # Shown as a node: only lofted ChunkParamBase params, no sub-chunk containers.
            for param in group.get_my_params():
                if isinstance(param, chunk_core.ChunkParamBase):
                    self._add_param_widget(param)
            return
        super()._build_param_widgets(group)

    def animation_finished(self):
        super().animation_finished()
        if self._is_context_tab() and not self.is_collapsed():
            # After expansion, clear the fixed height the animation pinned on the
            # QScrollArea so it can expand to fill __layout__ naturally.
            self.content.setMinimumHeight(0)
            self.content.setMaximumHeight(16777215)

    def update_from_state(self):
        if self._is_group_chunk_node():
            # Skip super() — it would start the collapse animation which calls
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
            self.set_selected(self.group.has_state(ChunkFlags.State.SELECTED))
            return
        super().update_from_state()

    def _add_group_widget(self, group_chunk):
        """Add a ChunkGroup as a visible item in this tab's params panel.

        We bypass add_param so sub-chunk PARAM_ADDED notices never propagate
        into this tab context's ownership chain.
        """
        if self._is_hidden_by_params_filter(group_chunk):
            return
        w = ChunkWidget(group_chunk, self)
        w.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        # Route through add_widget (LabeledWidget wrapping, label hidden since none is passed)
        # instead of a raw content_layout.addWidget, so this stays trackable in group_widgets
        # and removable via remove_widget the same way a plain chunk's group layout is.
        self.add_widget(w)
        self.group_widgets[group_chunk.get_hash()] = w

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
            chunk_usd.rename_context_prim_path(self.group, val)

        edit.editingFinished.connect(_commit)

        layout.addWidget(label)
        layout.addWidget(edit)
        return container

    def _reorder_chunk_widget_after(
        self,
        chunk_to_move: "chunk.ChunkBase",
        after_chunk: "chunk.ChunkBase",
    ) -> None:
        """Move chunk_to_move's widget to appear immediately after after_chunk's widget."""
        after_idx: Optional[int] = None
        move_widget: Optional[QtWidgets.QWidget] = None
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

    def _find_view_state(self) -> "Optional[ViewSlotState]":
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

    def get_category(self) -> str:
        """Get the category of this Chunk

        Returns:
            str: Category of this Chunk
        """
        if isinstance(self.group, chunk_core.ChunkBase):
            if config := self.group.get_config():
                return config.category
        return chunk.DefaulConfig().category

    def set_selected(self, val: bool):
        selected = bool(
            val and (not self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT) or self._is_group_chunk())
        )

        # setStyleSheet re-polishes the whole subtree beneath the widget and cost ~10ms a
        # call — 112 of them, 1.7s, easily the most expensive thing in building a chunk.
        # Every nested group is its own ChunkWidget and each used to apply the entire
        # CHUNK_STYLESHEET. Qt stylesheets cascade, so only the outermost needs to, and
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
            # Double-click on the header of a ChunkGroup — dive into its tab.
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


def confirm_dial(msg) -> bool:
    dial = QtWidgets.QMessageBox(text=msg)
    dial.setStandardButtons(QtWidgets.QMessageBox.StandardButton.No | QtWidgets.QMessageBox.StandardButton.Yes)
    return dial.exec() == QtWidgets.QMessageBox.StandardButton.Yes


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


class FilterableChunkMenu(QtWidgets.QMenu):
    def __init__(self):
        super().__init__("Chunks")
        self.filter_widget = QtWidgets.QLineEdit(placeholderText="Search...")
        self.filter_widget.textChanged.connect(self._filter_actions)
        self.filter_widget.installEventFilter(self)
        self.filterable = dict()

    def _filter_menus(self, menu: QtWidgets.QMenu):
        c = 0
        for a in menu.actions():
            if not a.isVisible():
                c += 1

            if m := menu.menuInAction(a):
                hide = self._filter_menus(m)
                if hide:
                    m.menuAction().setVisible(False)
                else:
                    m.menuAction().setVisible(True)

        if c == len(menu.actions()):
            return 1

        return 0

    def _filter_actions(self, text: str):
        term = text.strip().lower()
        normal_font = self.font()
        bold_font = QtGui.QFont(normal_font)
        bold_font.setBold(True)

        if not term:
            for action in self.filterable:
                action.setVisible(True)
                action.setFont(normal_font)
            self._filter_menus(self)
            return

        for action, path_elems in self.filterable.items():
            matches = any(term in x for x in path_elems)
            action.setVisible(matches)
            action.setFont(bold_font if matches else normal_font)

        self._filter_menus(self)
        QtCore.QTimer.singleShot(0, self, self._open_first_submenu)

    def _open_first_submenu(self):
        for action in self.actions():
            if (
                action.isVisible()
                and not action.isSeparator()
                and not isinstance(action, QtWidgets.QWidgetAction)
            ):
                self.setActiveAction(action)
                submenu = action.menu()
                if submenu:
                    if not submenu.isVisible():
                        QtWidgets.QApplication.postEvent(
                            self,
                            QtGui.QKeyEvent(
                                QtCore.QEvent.Type.KeyPress,
                                QtCore.Qt.Key.Key_Right,
                                QtCore.Qt.KeyboardModifier.NoModifier,
                            ),
                        )
                    QtCore.QTimer.singleShot(0, self, lambda m=submenu: self._highlight_submenu_leaf(m))
                break

    def _highlight_submenu_leaf(self, submenu: QtWidgets.QMenu):
        for action in submenu.actions():
            if action.isVisible() and not action.isSeparator():
                submenu.setActiveAction(action)
                break

    def eventFilter(self, obj, event):
        if obj is self.filter_widget:
            if event.type() == event.Type.KeyPress:
                key = event.key()
                if key == QtCore.Qt.Key.Key_Escape:
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

        Renders chunk_registry.get_menu_entries() -- the entry list, its namespace nesting
        and its search terms are all core, so this only turns them into QMenus and QActions.

        Args:
            pos (QtCore.QPoint): Global position to pop the menu at

        Returns:
            Optional[chunk_registry.MenuEntry]: The chosen entry, or None if dismissed or
            if the user picked "import" (which is handled here).
        """
        items = dict()
        items["menu"] = self
        self.setToolTipsVisible(True)
        self._flat_results: list = []

        filter_action = QtWidgets.QWidgetAction(items["menu"])
        filter_action.setDefaultWidget(self.filter_widget)

        items["menu"].addAction(filter_action)
        items["menu"].addSeparator()

        for entry in chunk_registry.ChunkRegistry().get_menu_entries(sort_result=True):
            namespace = list(entry.namespace)
            search_term = entry.search_terms
            tooltip = entry.tooltip

            d = items
            last = len(namespace) - 1
            for i, ns in enumerate(namespace):
                if i == last:
                    if m := d.get("menu"):
                        action = m.addAction(ns)
                        action.setData(entry)
                        action.setToolTip(tooltip)
                        self.filterable[action] = search_term
                    break
                elif ns not in d:
                    submenu = d.get("menu").addMenu(ns)
                    submenu.setToolTipsVisible(True)
                    d[ns] = {"menu": submenu}
                d = d[ns]

            # Flat search result — full path label, hidden until search is active
            label = " → ".join(namespace)
            flat_action = self.addAction(label)
            flat_action.setData(entry)
            flat_action.setToolTip(tooltip)
            flat_action.setVisible(False)
            self._flat_results.append((flat_action, search_term))

        self.addSeparator()

        import_action = QtGui.QAction("import")
        import_action.setData("import")
        self.addAction(import_action)

        self.filter_widget.setFocus()
        QtWidgets.QApplication.instance().installEventFilter(self)
        exc = self.exec_(pos)
        QtWidgets.QApplication.instance().removeEventFilter(self)

        if not exc:
            return

        if exc.data() == "import":
            if file := file_dial("*" + chunk_registry.PY_MODULE_EXT):
                chunk_registry.ChunkRegistry().import_chunk(file)
            return

        if not (entry := exc.data()) or not isinstance(entry, chunk_registry.MenuEntry):
            return

        return entry


def file_dial(name_filter: Optional[str] = None, create_new=False, directory=False):
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
        self._layout.setContentsMargins(6, 1, 6, 1)
        self._layout.setSpacing(3)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.MinimumExpanding, QtWidgets.QSizePolicy.Policy.Fixed
        )

    def set_context(self, ctx) -> None:
        """Rebuild the row for ctx, root first."""
        while (item := self._layout.takeAt(0)) is not None:
            if (w := item.widget()) is not None:
                w.setParent(None)
                w.deleteLater()

        for i, node in enumerate(context_ancestry(ctx)):
            if i:
                sep = QtWidgets.QLabel("›", self)  # a single ›, not a > that reads as markup
                sep.setEnabled(False)
                # A touch larger than the names it divides: at the same size the glyph is
                # thin enough to read as punctuation inside a word rather than as a step
                # between two places.
                sep_font = sep.font()
                sep_font.setPointSizeF(sep_font.pointSizeF() + 2)
                sep.setFont(sep_font)
                self._layout.addWidget(sep)
            self._layout.addWidget(self._entry(node, is_current=node is ctx))
        self._layout.addStretch(1)

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
    """ctx and everything it is nested inside, outermost first.

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
