from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Dict, Optional, DefaultDict, List, Generic

from pxr import Tf

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_resources, chunk_serial
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


class ChunkSliderWidget(ChunkParamWidgetBase[chunk.Slider], QtWidgets.QSlider):
    def __init__(self, param: chunk.Slider):
        super().__init__(param)

        self._step = 1000 if usd_chunks.chunk_flags.ChunkFlags.ParamType.FLOAT in param.evaluate_type() else 1

        min_val = param.min_value
        max_val = param.max_value

        if min_val is not None:
            self.setMinimum(int(min_val * self._step))
        else:
            self.setMinimum(10 * (1 - self._step))

        if max_val is not None:
            self.setMaximum(int(max_val * self._step))
        else:
            self.setMaximum(10 * self._step)

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value() / self._step

    def set_value(self, value):
        self.setValue(value * self._step)

    def set_param_layout(self, layout_flags: usd_chunks.chunk_flags.ChunkFlags.LayoutFlags):
        is_horizontal = usd_chunks.chunk_flags.ChunkFlags.LayoutFlags.HORIZONTAL == layout_flags
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

        if max_val is not None:
            self.setMaximum(max_val * self._step)
        else:
            self.setMaximum(999999999.0)

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
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

        if max_val is not None:
            self.setMaximum(int(max_val * self._step))
        else:
            self.setMaximum(999999999)

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
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

        if not context:
            event.ignore()
            return

        menu = QtWidgets.QMenu()
        loft_action = menu.addAction("Loft up")

        if action := menu.exec_(event.globalPos()):
            if action == loft_action:
                chunk_usd.loft_param_to_context(param, context)


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

    def build_collapsable(self):
        if self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            # Context chunks are scrollable
            self.content = QtWidgets.QScrollArea(self)
            self.content_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom)
            self.content_layout.setContentsMargins(2, 2, 2, 10)
            self.content_layout.setSpacing(20)
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
    ):
        super().__init__(group, parent)

        self.installEventFilter(self)

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
            self._lofted_layout = QtWidgets.QVBoxLayout(self._lofted_container)
            self._lofted_layout.setContentsMargins(4, 2, 4, 4)
            self._lofted_layout.setSpacing(2)
            self._lofted_container.setVisible(False)
            self.__layout__.insertWidget(3, self._lofted_container)

    def add_widget(self, widget: QtWidgets.QWidget, label=None):
        if self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT) and isinstance(widget, ChunkParamWidgetBase):
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)
            labeled = LabeledWidget(widget, self)
            if label is not None:
                labeled.update_label(label)
            labeled._name_hint = widget.param.name
            self._lofted_layout.addWidget(labeled)
            self._lofted_container.setVisible(True)
        else:
            super().add_widget(widget, label)

    def _build_prim_path_field(self) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
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
            chunk_usd.rename_context_prim_path(self.group, val)

        edit.editingFinished.connect(_commit)

        layout.addWidget(label)
        layout.addWidget(edit)
        return container

    def contextMenuEvent(self, event):
        if self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            event.ignore()
            return

        menu = QtWidgets.QMenu()
        actions: Dict[QtGui.QAction, chunk_notice.NoticeConnection] = {}
        for conn in self.group.find_callbacks(notice_type=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE):
            meth = conn.get_method()
            if not meth:
                continue

            actions[QtGui.QAction(meth.__name__)] = conn

        act_names = list(actions.keys())

        # Custom UI actions
        inspect_layer_action = QtGui.QAction("inspect layer")

        act_names.append(inspect_layer_action)
        menu.addActions(act_names)

        res = menu.exec_(event.globalPos())

        if not res:
            event.ignore()
            return

        if conn := actions.get(res):
            conn.handle_callback(chunk_notice.MenuActionNotice(self.group.get_notice_data()), self.group, False)

        elif res == inspect_layer_action:
            layer = self.group.get_edit_context()
            if layer:
                source_code_dial(layer.ExportToString(), chunk_core.UsdSyntaxHighlightMapping())

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
        if val and not self.group.has_flag(ChunkFlags.GroupFlags.CONTEXT):
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
