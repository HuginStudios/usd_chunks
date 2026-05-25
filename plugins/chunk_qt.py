from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Dict, Optional, DefaultDict, List, Generic

from pxr import Tf

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_resources, chunk_serial
import usd_chunks.chunk_flags
from usd_chunks.chunk_core import ChunkParamWidgetBase
from usd_chunks import chunk_core

from functools import partial
from usd_chunks.chunk_flags import ChunkFlags
import os


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
            self.setMinimum(10 * (1-self._step))

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

        if isinstance(self.widget, ChunkParamWidgetBase):
            self._listener = chunk_notice.NoticeConnection(
                self.update_value, alias=self.widget.get_notice_alias(), sender=self.widget
            )

        self.setMouseTracking(True)

    def should_show_tooltip(self):
        if not isinstance(self.widget, ChunkParamWidgetBase):
            return False

        t = self.widget.param.evaluate_type()
        if (ChunkFlags.ParamType.EMPTY | ChunkFlags.ParamType.STRING) & t != 0:
            return False
        return True

    def tooltip_pos(self):
        return self.mapToGlobal(self.rect().bottomLeft())

    def show_tooltip(self):
        self._value_tooltip.move(self.tooltip_pos())
        self._value_tooltip.setHidden(False)
        self._value_tooltip.setText(str(self.widget.param.value))
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
            self.show_tooltip()
            event.accept()
        return super().mouseMoveEvent(event)

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        if self.should_show_tooltip():
            self.show_tooltip()
            event.accept()
        return super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.hide_tooltip()
        return super().leaveEvent(event)


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
    Generic[chunk_core.GroupT], chunk_core.ChunkContainerWidgetBase[chunk_core.GroupT], QtWidgets.QWidget
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
            self.content = QtWidgets.QFrame()
            self.content_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom)
            self.content_layout.setContentsMargins(2, 2, 2, 10)
            self.content_layout.setSpacing(1)
            self.content.setLayout(self.content_layout)

        # Setup minimize/maximize button
        self.minimize_button = QtWidgets.QToolButton()
        self.minimize_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.minimize_button.setIconSize(QtCore.QSize(6, 6))
        self.minimize_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)

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
        self.setParent(None)
        self.deleteLater()

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
        if label is not None:
            labeled.update_label(label)

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

    def __init__(
        self,
        group: chunk.ChunkBase,
        parent=None,
    ):
        super().__init__(group, parent)

    def build_collapsable(self):
        super().build_collapsable()

        if not isinstance(self.group, chunk.ChunkBase):
            return

        self.__layout__.setContentsMargins(2, 1, 2, 0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Minimum,
        )

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

        if parent := self.parentWidget():
            res = menu.exec_(parent.mapToGlobal(self.geometry().topLeft()))
        else:
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


class EditableTabBar(QtWidgets.QTabBar):
    request_delete = QtCore.Signal(int)
    request_toggle_disable = QtCore.Signal(int)
    tab_name_changed = QtCore.Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._editor = QtWidgets.QLineEdit(self)
        self._editor.setWindowFlags(QtCore.Qt.WindowType.Popup)
        self._editor.setFocusProxy(self)
        self._editor.editingFinished.connect(self.handleEditingFinished)
        self._editor.installEventFilter(self)

    def mousePressEvent(self, event):
        if self.tabAt(event.pos()) == 0:
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        idx = self.tabAt(event.pos())
        if idx == 0:
            return

        menu = QtWidgets.QMenu()

        remove_action = QtGui.QAction("remove")
        disable_action = QtGui.QAction("toggle enabled")

        menu.addActions([remove_action, disable_action])
        res = menu.exec_(event.globalPos())

        if not res:
            return

        if res == remove_action:
            self.request_delete.emit(idx)
        elif res == disable_action:
            self.request_toggle_disable.emit(idx)

    def eventFilter(self, widget, event):
        if (
            event.type() == QtCore.QEvent.Type.MouseButtonPress
            and not self._editor.geometry().contains(event.globalPos())
        ) or (event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape):
            self._editor.hide()
            return True

        return super().eventFilter(widget, event)

    def mouseDoubleClickEvent(self, event):
        index = self.tabAt(event.pos())
        if index > 0:
            self.editTab(index)

    def editTab(self, index):
        if self.shape() in [
            QtWidgets.QTabBar.Shape.RoundedEast,
            QtWidgets.QTabBar.Shape.RoundedWest,
            QtWidgets.QTabBar.Shape.TriangularEast,
            QtWidgets.QTabBar.Shape.TriangularWest,
        ]:
            rect = self.tabRect(index)
            h = rect.height()
            rect.setHeight(rect.width())
            rect.setWidth(h)

        else:
            rect = self.tabRect(index)

        self._editor.move(self.mapToGlobal(rect.topLeft()))
        self._editor.setText(self.tabText(index))

        self._editor.setFixedSize(rect.size())
        if not self._editor.isVisible():
            self._editor.show()

    def handleEditingFinished(self):
        index = self.currentIndex()
        if index >= 0:
            self._editor.hide()
            self.setTabText(index, self._editor.text())
            self.tab_name_changed.emit(self._editor.text(), index)


class ChunkTabWidget(QtWidgets.QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.tab_bar = EditableTabBar(self)
        self.setTabBar(self.tab_bar)

        self.tab_bar.request_delete.connect(self.delete_tab)
        self.tab_bar.request_toggle_disable.connect(self.toggle_disabled_tab)
        self.tab_bar.tab_name_changed.connect(self.update_context_name)

        self._btn = QtWidgets.QPushButton("+", self)
        self._btn.clicked.connect(self.add_new_tab)

        self._tabPosition = QtWidgets.QTabWidget.TabPosition.North
        self.setMinimumWidth(200)

        self.add_tab_button()
        self.update_stylesheet()

    def update_stylesheet(self):
        code = ""

        if self.count() > 1:
            code = """
                QTabBar::tab {
                    border-color: #4A4A4A;
                    border: 1px solid transparent;
                }
                QTabBar::tab:right {
                    padding: 12 0px;
                    border-top-right-radius: 8px;
                    border-bottom-right-radius: 8px;
                }
                QTabBar::tab:top {
                    padding: 0 12px;
                    border-top-left-radius: 8px;
                    border-top-right-radius: 8px;
                }

                QTabBar::tab:hover:!selected {
                    background: #868686;
                }

                QTabBar::tab:selected:!first {
                    border-color: #c4c7cc;
                }

                QTabBar::tab:first {
                    background: transparent;
                    border: none;
                    padding: 0 0px;
                }
                QTabBar::tab:first:hover {
                    background: transparent;
                }
                """
        else:
            code = """
                QTabBar::tab {
                    background: transparent;
                    border: none;
                    padding: 0 0px;
                }
                """
        self.setStyleSheet(code)

    def toggle_disabled_tab(self, idx):
        if t := self.get_chunk_context(idx):
            if t.group.has_state(ChunkFlags.State.DISABLED):
                t.group.enable()
            else:
                t.group.disable()

    def delete_tab(self, idx):
        if t := self.get_chunk_context(idx):
            t.group.request_delete()
        self.update_stylesheet()

    def update_context_name(self, name, idx):
        if tab := self.get_chunk_context(idx):
            tab.group.name = name

    def add_new_tab(self):
        # Create a new context chunk which will be displayed as the current tab
        name = f"Tab {self.tabBar().count()}"

        if scene := chunk.get_scene():
            scene.add_context(name)
        else:
            c = chunk.Chunk(name, flags=ChunkFlags.GroupFlags.CONTEXT)
            self.add_chunk_context(c)

    def add_tab_button(self):
        w = ChunkArea(self)
        idx = self.addTab(w, "")

        self.tabBar().setTabButton(idx, QtWidgets.QTabBar.ButtonPosition.LeftSide, self._btn)
        return idx

    def update_tab_position(self):
        if self.height() > self.width():
            self._tabPosition = QtWidgets.QTabWidget.TabPosition.East
        else:
            self._tabPosition = QtWidgets.QTabWidget.TabPosition.North
        self.setTabPosition(self._tabPosition)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        self.update_tab_position()
        return super().resizeEvent(event)

    def tabPosition(self) -> QtWidgets.QTabWidget.TabPosition:
        return self._tabPosition

    def contextMenuEvent(self, event):
        mod = FilterableChunkMenu().select_module(event.globalPos())
        if not mod:
            return

        if mod.loaded:
            chunks = mod.create()
        else:
            chunks = mod.load()

        for c in chunks:
            if c.has_flag(ChunkFlags.GroupFlags.CONTEXT) and (scene := chunk.get_scene()):
                # If the chunk has the context flag, we should just add it to the scene and let ChunkWidgetDocker 
                # manage the adding of the context (from CHUNK_LOADED)
                scene.add_chunk(c)
            else:
                self.add_chunk_to_tab(c)

    def get_chunk_context(self, idx) -> ChunkWidget | None:
        w = self.widget(idx)
        assert w is None or isinstance(w, ChunkWidget)
        return w

    def get_chunk_context_index(self, ctx: chunk_core.ChunkBase) -> int | None:
        for i in range(self.count()):
            w = self.widget(i)
            if isinstance(w, ChunkWidget) and w.group == ctx:
                return i
        return None

    def add_chunk_context(self, c: chunk.ChunkBase):
        w = ChunkWidget(c, self)
        idx = self.addTab(w, c.name)
        self.setCurrentIndex(idx)
        self.update_stylesheet()

    def add_chunk_to_tab(self, param: chunk_core.ChunkBase, tabidx=None):
        if tabidx is None:
            tabidx = self.tabBar().currentIndex()
        if tabidx == 0:
            self.add_new_tab()
            tabidx = 1

        if t := self.get_chunk_context(tabidx):
            if param in t.group.get_my_params():
                return

            # This will add the chunk to the Context chunk (tab) and display it in the tab widget
            t.group.add_param(param)


class ChunkWidgetDocker(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self._chunk_loaded_listener = chunk_notice.NoticeConnection(
            self._loaded_module_callback, chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, globally=True
        )
        self.build()

    def _loaded_module_callback(self, notice: Tf.Notice, sender: chunk.Chunk):
        """Called whenever CHUNK_LOADED notice was recieved - will in turn add a new chunk widget

        Args:
            notice (chunk_notice.ChunkLoadedNotice): The notice sent from the registry
            sender (chunk.Chunk): The new chunk that was added
        """
        self.create_chunk_widget(sender)

    def create_chunk_widget(self, loaded_chunk: chunk.ChunkBase):
        if loaded_chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            self.tab_widget.add_chunk_context(loaded_chunk)
        elif ctx := loaded_chunk.get_context():
            idx = self.tab_widget.get_chunk_context_index(ctx)
            self.tab_widget.add_chunk_to_tab(loaded_chunk, idx)

    def build(self):
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.MinimumExpanding, QtWidgets.QSizePolicy.Policy.MinimumExpanding)
        self.tab_widget: ChunkTabWidget = ChunkTabWidget(self)

        registry = chunk_registry.ChunkRegistry()
        self.setStyleSheet("""
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
        """)

        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        if registry.initialized:
            for c in registry.scene.get_chunks():
                self.create_chunk_widget(c)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.tab_widget)

        self.setLayout(self.main_layout)
        self.show()


class ChunkModuleLoader(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.content = QtWidgets.QTableWidget(rowCount=0, columnCount=3, parent=self)

        self.content.setSelectionMode(QtWidgets.QTableWidget.SelectionMode.ExtendedSelection)
        self.content.setSelectionBehavior(QtWidgets.QTableWidget.SelectionBehavior.SelectRows)
        self.content.setEditTriggers(QtWidgets.QTableWidget.EditTrigger.NoEditTriggers)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.addWidget(self.content)

        self.setLayout(self.main_layout)
        self.load_chunks()

        self.content.model().dataChanged.connect(self.handle_data_change)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        if not (item := self.content.currentItem()):
            return

        reg = chunk_registry.ChunkRegistry()
        if not (mod := reg.get_chunk_module(item.text())):
            return

        menu = QtWidgets.QMenu()

        create_action = QtGui.QAction("create")
        export_action = QtGui.QAction("export")
        inspect_action = QtGui.QAction("inspect")

        menu.addActions([create_action, export_action, inspect_action])

        res = menu.exec_(event.globalPos())

        if not res:
            return

        if res == export_action:
            exts = (chunk_serial.FileExtensions.CHUNK_MODULE, chunk_serial.FileExtensions.PY_MODULE)
            flt = " ".join([f"*{x}" for x in exts])
            if file := file_dial(flt, True):
                mod.export(file)
        elif res == inspect_action and mod.module:
            if isinstance(mod.module, chunk_serial.RuntimeModule):
                src = mod.module._code
            else:
                src = chunk_serial.serialize_source(mod.module)
            source_code_dial(src, chunk_core.PySyntaxHighlightMapping())
        elif res == create_action:
            if not mod.loaded:
                cs = mod.load()
            else:
                cs = mod.create()
            if cs:
                chunk_registry.ChunkRegistry().scene.add_chunks(cs)

        return super().contextMenuEvent(event)

    def get_check_state(self, loaded):
        return QtCore.Qt.CheckState.Checked if loaded else QtCore.Qt.CheckState.Unchecked

    def new_row(self, row, mod: chunk_registry.ChunkModule):
        self.content.insertRow(row)

        c1 = QtWidgets.QTableWidgetItem(mod.module_name)
        c1.setToolTip(mod.config.description)

        c2 = QtWidgets.QTableWidgetItem(mod.module_file.as_posix() if mod.module_file.exists() else "Not Found")

        c3 = QtWidgets.QTableWidgetItem(mod.config.category)

        self.content.setItem(row, 0, c1)
        self.content.setItem(row, 1, c2)
        self.content.setItem(row, 2, c3)

    def load_chunks(self):
        reg = chunk_registry.ChunkRegistry()
        for row, c in enumerate(reg.get_chunk_modules()):
            self.new_row(row, c)

    def load_unload(self, mod: chunk_registry.ChunkModule, check):
        if check and not mod.loaded:
            mod.load()
        elif not check and mod.loaded:
            mod.unload()

    def handle_data_change(self, first: QtCore.QModelIndex, last: QtCore.QModelIndex, roles):
        if QtCore.Qt.ItemDataRole.CheckStateRole not in roles:
            return

        reg = chunk_registry.ChunkRegistry()
        check = None

        for r in range(first.row(), last.row() + 1):
            check_idx = self.content.model().index(r, 0)
            mod_idx = self.content.model().index(r, 1)

            mod = reg.get_chunk_module(mod_idx.data())
            if not mod:
                raise ModuleNotFoundError(f"Could not find module {mod_idx.data()}")

            check = check_idx.data(QtCore.Qt.ItemDataRole.CheckStateRole)
            self.load_unload(mod, check)

        if check is None:
            return

        for srange in self.content.selectedRanges():
            for row in range(srange.topRow(), srange.bottomRow() + 1):
                check_idx = self.content.model().index(row, 0)
                mod_idx = self.content.model().index(row, 1)

                if row >= first.row() and row < last.row() + 1:
                    continue

                mod = reg.get_chunk_module(mod_idx.data())

                if not mod:
                    raise ModuleNotFoundError(f"Could not find module {mod_idx.data()}")
                check_item = self.content.item(check_idx.row(), 0)

                if check_item:
                    check_item.setCheckState(self.get_check_state(check))
                    self.load_unload(mod, check)


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

        if not term:
            for action in self.filterable:
                action.setVisible(True)
            self._filter_menus(self)

            return

        for action, path_elems in self.filterable.items():
            action.setVisible(any(term in x for x in path_elems))

        self._filter_menus(self)

    def eventFilter(self, obj, event):
        """Keep arrow/return key presses inside the QLineEdit."""
        if obj is self.filter_widget:
            if event.type() == event.Type.KeyPress:
                key = event.key()
                if key in (
                    QtCore.Qt.Key.Key_Return,
                    QtCore.Qt.Key.Key_Escape,
                ):
                    return True  # prevent menu navigation stealing focus
        return super().eventFilter(obj, event)

    def select_module(self, pos):
        items = dict()
        items["menu"] = self

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

        self.addSeparator()

        import_action = QtGui.QAction("import")
        import_action.setData("import")
        self.addAction(import_action)

        self.filter_widget.setFocus()
        exc = self.exec_(pos)

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


def build_chunk_docker(parent_window: QtWidgets.QMainWindow):
    # Create an empty dock widget to hold the modules
    module_dock = parent_window.findChild(QtWidgets.QDockWidget, "Chunks")
    if module_dock:
        return

    module_dock = QtWidgets.QDockWidget("Chunks", parent_window)

    module_dock.setWidget(ChunkWidgetDocker(parent=module_dock))
    parent_window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, module_dock)
    parent_window.resizeDocks([module_dock], [300], QtCore.Qt.Orientation.Horizontal)
