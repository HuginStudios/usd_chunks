from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Optional, Generic

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_resources, chunk_serial, chunk_utils
import usd_chunks.chunk_flags
from usd_chunks.chunk_core import ChunkParamWidgetBase
from usd_chunks import chunk_core
from plugins import chunk_usd

from functools import partial
from usd_chunks.chunk_flags import ChunkFlags
import os

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
"""

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

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)

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
        self.setValue(int(value * self._step))

    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        is_horizontal = ChunkFlags.LayoutFlags.HORIZONTAL == layout_flags
        orient = QtCore.Qt.Orientation.Horizontal if is_horizontal else QtCore.Qt.Orientation.Vertical

        self.setOrientation(orient)

    def value_changed(self, value: Optional[int | float] = None):
        if not self.param or value is None:
            return
        super().value_changed(value / float(self._step))

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
        return self.setValue(value)

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
        return self.setValue(value)

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

    def handle_param_changed(self, notice: chunk_notice.AttributeChangeNotice, sender: chunk.ChunkParamBase):
        if notice.alias == chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED and notice.data.value:
            self.button.setHidden(ChunkFlags.ParamFlags.PRIM_SELECTION not in notice.data.value)
        return super().handle_param_changed(notice, sender)

    def pin_prim(self):
        self.param.set_prims(self.param.get_scene().get_selected_prims())

    def get_value(self):
        return self.text.text()

    def set_value(self, value):
        return self.text.setText(str(value))

    def set_hidden(self, val: bool):
        self.setHidden(val)

    def set_enabled(self, val: bool):
        self.setEnabled(val)


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
        create_new = False
        if directory:
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
        self.clear()

        if menu_items:
            self.addItems(menu_items)
            val = self.param.value if self.param.value is not None else self.param.default_value
            if val:
                self.set_value(val)

        if self.param.default_value:
            self.setCurrentIndex(menu_items.index(self.param.default_value))

    def get_value(self):
        return self.currentText()

    def set_value(self, value):
        return self.setCurrentText(value)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # QComboBox.showPopup() can't use the proxy-aware popup path for widgets
        # that are deep descendants of a QGraphicsProxyWidget (graphicsProxyWidget()
        # returns None for non-direct embeds).  Replace with a QMenu so the popup
        # always appears at the correct screen position.
        QtCore.QTimer.singleShot(0, self._show_as_menu)

    def _show_as_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        current = self.currentText()
        for i in range(self.count()):
            text = self.itemText(i)
            act = menu.addAction(text)
            if text == current:
                act.setCheckable(True)
                act.setChecked(True)
        pos = self.mapToGlobal(QtCore.QPoint(0, self.height()))
        if chosen := menu.exec_(pos):
            self.setCurrentText(chosen.text())

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
        return self.setChecked(value)

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

        self._value_tooltip = QtWidgets.QLabel(parent=self)
        self._value_tooltip.setHidden(True)
        self._value_tooltip.setWindowFlags(QtCore.Qt.WindowType.ToolTip)
        self._tooltip_timer = QtCore.QTimer(self)
        self._tooltip_timer.timeout.connect(self.hide_tooltip)
        self._show_timer = QtCore.QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self.show_tooltip)

        if isinstance(self.widget, ChunkParamWidgetBase):
            self._listener = chunk_notice.NoticeConnection(
                self.update_value, alias=self.widget.get_notice_alias(), sender=self.widget
            )

        self._name_hint = ""
        self._label_hovered = False
        self.label.installEventFilter(self)

        self.setMouseTracking(True)

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
                    self._show_timer.start(600)
            elif event.type() == QtCore.QEvent.Type.Leave:
                self._label_hovered = False
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
        self._value_tooltip.setText(text)
        self._value_tooltip.adjustSize()
        self._value_tooltip.move(self.tooltip_pos())
        self._value_tooltip.setHidden(False)
        self._tooltip_timer.start(3000)

    def hide_tooltip(self):
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
            self._show_timer.start(600)
            event.accept()
        return super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
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
                text, ok = QtWidgets.QInputDialog.getText(
                    self, "Set Expression", "Expression:", QtWidgets.QLineEdit.EchoMode.Normal, current
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
        self.animation_duration = 300
        self._animation_value = 0
        self._depth = 0
        super().__init__(group, parent=parent)

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

        h = self.content_layout.sizeHint().height()
        self.content.setFixedHeight(int(h * scale))

    def animation_finished(self):
        """Modify the layout sizeConstraint when collapsed. Otherwise the child groups of collapsed ContainerWidgets will scale the widget when addWidget is called on the cild"""
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
        if checked:
            self.group.unset_state(ChunkFlags.State.COLLAPSED)
        else:
            self.group.set_state(ChunkFlags.State.COLLAPSED)

        self.minimize_button.setArrowType(arrow_type)
        self.toggle_anim.setDirection(direction)
        self.toggle_anim.start()

        if hasattr(self, "_group_bg"):
            bg = self._group_bg
            if checked:
                self.minimize_button.setStyleSheet(
                    f"QToolButton {{ background-color: {bg}; "
                    f"border-top-left-radius: 4px; border-top-right-radius: 4px; "
                    f"border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; }}"
                )
            else:
                self.minimize_button.setStyleSheet(f"QToolButton {{ background-color: {bg}; border-radius: 4px; }}")

    def delete_self(self):
        try:
            self.setParent(None)
            self.deleteLater()
        except RuntimeError:
            # C++ object already destroyed (e.g. by QGraphicsProxyWidget teardown)
            pass

    def remove_widget(self, widget: QtWidgets.QWidget):
        count = self.content_layout.count()
        for i in reversed(range(count)):
            item = self.content_layout.itemAt(i)
            if not item:
                continue
            iw = item.widget()

            if isinstance(iw, LabeledWidget) and iw.widget == widget:
                self.content_layout.removeItem(item)

    def add_widget(self, widget: QtWidgets.QWidget, label=None):  # type: ignore
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)

        labeled = LabeledWidget(widget, self)
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

    def show_chunk_menu(
        self,
        grp: "chunk.ChunkBase",
        prim_path: str,
        global_pos: QtCore.QPoint,
    ) -> None:
        """Build and exec the shared right-click menu for a chunk.

        Handles custom callback actions, Inspect Layer, and Set View / Activate
        View N.  The actual view-node creation is delegated to ChunkGraphView
        via the create_view_for signal.
        """
        menu = QtWidgets.QMenu()
        cb_actions: dict = {}
        for conn in grp.find_callbacks(notice_type=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE):
            meth = conn.get_method()
            if meth:
                cb_actions[menu.addAction(meth.__name__)] = conn

        inspect_action = menu.addAction("Inspect Layer")
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

        res = menu.exec(global_pos)
        if not res:
            return

        if res == inspect_action:
            layer = grp.get_edit_context()
            if layer:
                source_code_dial(layer.ExportToString(), chunk_core.UsdSyntaxHighlightMapping())
        elif view_action and res == view_action:
            if existing_view_slot is not None:
                self.set_slot(existing_view_slot)
            else:
                self.create_view_for.emit(prim_path)
        elif conn := cb_actions.get(res):
            conn.handle_callback(
                chunk_notice.MenuActionNotice(grp.get_notice_data()), grp, False
            )


class ChunkWidget(ContainerWidget[chunk.ChunkBase]):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    SELECTED_COLOR = "#cab46a"

    def __init__(
        self,
        group: chunk.ChunkBase,
        parent=None,
        is_tab_context: bool = False,
    ):
        # Must be set before super().__init__ so build_collapsable sees the correct role.
        self._is_tab_context_widget = is_tab_context
        self._view_state: Optional[ViewSlotState] = None
        self._view_overlay: Optional[QtWidgets.QLabel] = None
        super().__init__(group, parent)

        self.installEventFilter(self)
        if is_tab_context:
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def set_view_state(self, vs: "ViewSlotState") -> None:
        if self._view_state is not None:
            self._view_state.slot_changed.disconnect(self._update_view_overlay)
        self._view_state = vs
        vs.slot_changed.connect(self._update_view_overlay)
        if self._is_tab_context_widget:
            self._ensure_view_overlay()
            self._update_view_overlay(vs.active_slot)

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

        if not isinstance(self.group, chunk.ChunkBase):
            return

        self.__layout__.setContentsMargins(2, 1, 2, 0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )

        if self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            self._prim_path_field = self._build_prim_path_field()
            self.__layout__.insertWidget(2, self._prim_path_field)

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
            lofted_idx = 3 if hasattr(self, '_prim_path_field') else 2
            self.__layout__.insertWidget(lofted_idx, self._lofted_container)

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
        w = ChunkWidget(group_chunk, self)
        w.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Maximum)
        self.content_layout.addWidget(w, 0, QtCore.Qt.AlignTop)

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
        if val and (not self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT) or self._is_group_chunk()):
            col = self.SELECTED_COLOR
            pxs = 1
        else:
            col = "transparent"
            pxs = 0

        self.setStyleSheet(
            CHUNK_STYLESHEET +
            f""".ChunkWidget {{
                border: {pxs}px solid {col};
                border-radius: {pxs}px;
                }}
            """
        )
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
        QtCore.QTimer.singleShot(0, self._open_first_submenu)

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
                    QtCore.QTimer.singleShot(0, lambda m=submenu: self._highlight_submenu_leaf(m))
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
        items = dict()
        items["menu"] = self
        self._flat_results: list = []

        filter_action = QtWidgets.QWidgetAction(items["menu"])
        filter_action.setDefaultWidget(self.filter_widget)

        items["menu"].addAction(filter_action)
        items["menu"].addSeparator()

        for mod in chunk_registry.ChunkRegistry().get_chunk_modules(sort_result=True):
            namespace = mod.get_menu_name().split(os.pathsep)
            search_term = set([x.lower() for x in namespace])

            d = items
            for ns in namespace:
                if ns is namespace[-1]:
                    if m := d.get("menu"):
                        action = m.addAction(ns)
                        action.setData(mod)
                        self.filterable[action] = search_term
                    break
                elif ns not in d:
                    d[ns] = {"menu": d.get("menu").addMenu(ns)}
                d = d[ns]

            # Flat search result — full path label, hidden until search is active
            label = " → ".join(namespace)
            flat_action = self.addAction(label)
            flat_action.setData(mod)
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
            ext = (
                chunk_serial.FileExtensions.PY_MODULE,
                chunk_serial.FileExtensions.CHUNK_MODULE,
            )
            flt = " ".join(f"*{x}" for x in ext)
            if file := file_dial(flt):
                chunk_registry.ChunkRegistry().import_chunk(file)
            return

        if not (mod := exc.data()) or not isinstance(mod, chunk_registry.ChunkModule):
            return

        return mod


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
