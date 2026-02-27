from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Dict, Optional, DefaultDict, List

from pxr import Tf

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_resources
import usd_chunks.chunk_flags
from usd_chunks.chunk_core import ChunkParamWidgetBase
from usd_chunks import chunk_core

from functools import partial
from collections import defaultdict
from usd_chunks.chunk_flags import ChunkFlags
from enum import Enum


class ChunkSliderWidget(ChunkParamWidgetBase[chunk.Slider], QtWidgets.QSlider):
    def __init__(self, param: chunk.Slider):
        super().__init__(param)

        self._step = 1000 if usd_chunks.chunk_flags.ChunkFlags.ParamType.FLOAT in param.evaluate_type() else 1

        min_val = param.min_value
        max_val = param.max_value

        if min_val is not None:
            self.setMinimum(int(min_val * self._step))
        else:
            self.setMinimum(0)

        if max_val is not None:
            self.setMaximum(int(max_val * self._step))
        else:
            self.setMaximum(10 * self._step)

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value() / self._step

    def set_value(self, value):
        # self.blockSignals(True)
        self.setValue(value * self._step)
        # self.blockSignals(False)

    def set_param_layout(self, layout_flags: usd_chunks.chunk_flags.ChunkFlags.LayoutFlags):
        is_horizontal = usd_chunks.chunk_flags.ChunkFlags.LayoutFlags.HORIZONTAL in layout_flags
        orient = QtCore.Qt.Orientation.Horizontal if is_horizontal else QtCore.Qt.Orientation.Vertical

        self.setOrientation(orient)

    def value_changed(self, value: Optional[int | float] = None):
        if not self.param or value is None:
            return
        super().value_changed(value / float(self._step))


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

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
        return self.setValue(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()


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

        self.setup_chunk_widget()
        self.valueChanged.connect(self.value_changed)

    def get_value(self):
        return self.value()

    def set_value(self, value):
        return self.setValue(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()


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


class ChunkLineEditWidget(ChunkParamWidgetBase[chunk.LineEdit], QtWidgets.QLineEdit):
    def __init__(self, param: chunk.LineEdit):
        super().__init__(param)
        self.setup_chunk_widget()

        self.editingFinished.connect(self.value_changed)

    def get_value(self):
        return self.text()

    def set_value(self, value):
        return self.setText(str(value))


class ChunkFileWidget(ChunkParamWidgetBase[chunk.FileBrowser], QtWidgets.QWidget):
    ValueChanged = QtCore.Signal(str)

    def __init__(self, param: chunk.FileBrowser):
        super().__init__(param)

        self.button = QtWidgets.QToolButton(self)
        self.button.setText("Test button")

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

        dial = QtWidgets.QFileDialog()

        pattern = ""
        if self.param.pattern:
            pattern = self.param.pattern

        if not pattern:
            pattern = "*"

        if self.param.extensions:
            pattern = " ".join([pattern + x for x in self.param.extensions])

        dial.setNameFilter(pattern)

        if self.param.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING):
            dial.setFileMode(dial.FileMode.ExistingFile)

        if dial.exec():
            sel = dial.selectedFiles()
            if not sel:
                return

            self.set_value(sel[0])
            self.ValueChanged.emit(sel[0])

    def get_value(self):
        return self.text.text()

    def set_value(self, value):
        return self.text.setText(str(value))

    def set_icon(self, icon: str):
        self.button.setIcon(QtGui.QIcon(icon))

    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        if ChunkFlags.LayoutFlags.VERTICAL in layout_flags:
            self.main_layout = QtWidgets.QVBoxLayout()
        else:
            self.main_layout = QtWidgets.QHBoxLayout()

        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.text)
        self.main_layout.addWidget(self.button)

        self.setLayout(self.main_layout)


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


class ChunkCheckBoxWidget(ChunkParamWidgetBase[chunk.Checkbox], QtWidgets.QCheckBox):
    def __init__(self, param: chunk.Checkbox):
        super().__init__(param)
        self.setup_chunk_widget()

        self.toggled.connect(self.value_changed)

    def get_value(self):
        return self.isChecked()

    def set_value(self, value: bool):
        return self.setChecked(value)


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
            self.param.execute()


class ChunkSeparatorWidget(ChunkParamWidgetBase, QtWidgets.QFrame):
    def __init__(self, param: chunk.Separator):
        super().__init__(param)

        is_horizontal = usd_chunks.chunk_flags.ChunkFlags.LayoutFlags.HORIZONTAL in param.layout
        shape = QtWidgets.QFrame.Shape.HLine if is_horizontal else QtWidgets.QFrame.Shape.VLine
        self.setFrameShape(shape)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)

        self.setup_chunk_widget()


class ChunkQTWidgetFactory(chunk_core.ChunkWidgetFactoryBase):
    widget_map = {
        ChunkFlags.ParamType.FLOAT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.INT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.FLOAT_SPINBOX: ChunkFloatSpinboxWidget,
        ChunkFlags.ParamType.INT_SPINBOX: ChunkIntSpinboxWidget,
        ChunkFlags.ParamType.LABEL: ChunkLabelWidget,
        ChunkFlags.ParamType.LINEEDIT: ChunkLineEditWidget,
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

        self.vlayout = QtWidgets.QVBoxLayout()
        self.vlayout.setContentsMargins(0, 0, 0, 0)
        self.vlayout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        self.widget: ChunkParamWidgetBase = widget
        self.label = QtWidgets.QLabel()
        self.label.setHidden(True)

        self.vlayout.addWidget(self.label)
        self.vlayout.addWidget(widget)
        self.setLayout(self.vlayout)

    def update_label(self, text):
        self.label.setHidden(not bool(text))
        self.label.setText(text)


class ContainerWidget(chunk_core.ChunkContainerWidgetBase, QtWidgets.QWidget):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    group_height_changed = QtCore.Signal(float)

    def __init__(
        self,
        group: chunk_core.ChunkContainerBase,
        parent=None,
    ):
        self.animation_duration = 300
        self._animation_value = 0
        super().__init__(group, parent=parent)

    def build_collapsable(self):
        self.content = QtWidgets.QFrame()
        self.content.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Ignored)

        self.content_layout = QtWidgets.QFormLayout()
        self.content_layout.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
        self.content_layout.setContentsMargins(2, 1, 2, 1)
        self.content_layout.setHorizontalSpacing(0)
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
        self.content.setFixedHeight(h * scale)

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

    def delete_self(self):
        self.setParent(None)
        self.deleteLater()

    def remove_widget(self, widget: QtWidgets.QWidget):
        count = self.content_layout.rowCount()
        for i in reversed(range(count)):
            item = self.content_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.FieldRole)
            if not item:
                continue
            iw = item.widget()

            if isinstance(iw, LabeledWidget) and iw.widget == widget:
                self.content_layout.removeRow(i)

    def add_widget(self, widget: QtWidgets.QWidget, label=None):  # type: ignore
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Minimum)

        labeled = LabeledWidget(widget, self)
        if label is not None:
            labeled.update_label(label)

        self.content_layout.addWidget(labeled)

        if self.group.has_flag(ChunkFlags.GroupFlags.LIST):
            del_icon = QtGui.QIcon(str(chunk_resources.get_icon("remove")))

            i = self.content_layout.rowCount() - 1
            item = self.content_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.FieldRole)
            if not item or not (w := item.widget()):
                return

            label = self.content_layout.labelForField(w)

            if label is None:
                del_btn = QtWidgets.QToolButton()
                del_btn.setIcon(del_icon)
                del_btn.setIconSize(QtCore.QSize(15, 15))
                del_btn.clicked.connect(partial(self.remove_from_list_group, widget))

                self.content_layout.setWidget(i, QtWidgets.QFormLayout.ItemRole.LabelRole, del_btn)
                self.content_layout.setWidget(i, QtWidgets.QFormLayout.ItemRole.FieldRole, w)

    def remove_from_list_group(self, widget: QtWidgets.QWidget):
        if isinstance(self.group, chunk_core.ChunkListGroupBase):
            for i in range(self.content_layout.rowCount()):
                item = self.content_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.FieldRole)
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

    def update_param_label(self, notice, sender: chunk_core.AttributeSender):
        """
        When a param name, label or flag is changed, this will correct the label in the FormLayout.
        If LABELED was unset, then remove the label and set the widget role to SpanningRole
        If LABELED was set, add the label
        otherwise, just update the label text if name or label changed

        Args:
            param (chunk_core.ChunkCommon): The param that was changed
            notice (Tf.Notice): The notice that was sent
            sender (chunk_core.AttributeSender): The attribute that changed
        """
        param = sender.get_owner()
        if not param or not isinstance(param, chunk_core.ChunkCommon):
            return

        labeled = param.has_flag(ChunkFlags.ParamFlags.LABELED)
        if not labeled:
            return

        for i in range(self.content_layout.count()):
            item = self.content_layout.itemAt(i)
            if not item:
                continue

            w = item.widget()
            if isinstance(w, LabeledWidget):
                if not isinstance(w.widget, ChunkParamWidgetBase):
                    continue

                if isinstance(w.widget.param, chunk_core.ProxyParam):
                    if not w.widget.param._param_ref == param:
                        continue
                else:
                    if not w.widget.param == param:
                        continue

                if labeled:
                    w.update_label(param.get_label())


class ChunkWidget(ContainerWidget):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    def __init__(
        self,
        group: chunk.Chunk,
        parent=None,
    ):
        super().__init__(group, parent)

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu()
        actions: Dict[QtGui.QAction, chunk_notice.NoticeConnection] = {}
        for conn in self.group.find_callbacks(notice_type=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE):
            meth = conn.get_method()
            if not meth:
                continue

            actions[QtGui.QAction(meth.__name__)] = conn

        menu.addActions(list(actions.keys()))

        if parent := self.parentWidget():
            res = menu.exec_(parent.mapToGlobal(self.geometry().topLeft()))
        else:
            res = menu.exec_(event.globalPos())

        if not res:
            return

        conn = actions[res]
        conn.handle_callback(chunk_notice.MenuActionNotice(self.group.get_notice_data()), self.group, False)

    def get_category(self) -> str:
        """Get the category of this Chunk

        Returns:
            str: Category of this Chunk
        """
        if isinstance(self.group, chunk_core.ChunkBase):
            if config := self.group.get_config():
                return config.category
        return chunk.DefaulConfig().category


class ChunkArea(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.flow = QtWidgets.QVBoxLayout()
        self.flow.setContentsMargins(0, 0, 0, 0)

        self.flow.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.widget_holder = QtWidgets.QWidget(self)
        self.widget_holder.setLayout(self.flow)

        self.setWidget(self.widget_holder)
        self.setWidgetResizable(True)


class ChunkTabWidget(QtWidgets.QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self._tabPosition = QtWidgets.QTabWidget.TabPosition.North

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
        items = dict()
        items["menu"] = QtWidgets.QMenu("chunks")

        for mod in chunk_registry.ChunkRegistry().registered_modules.values():
            cat = mod.config.category
            if isinstance(cat, Enum):
                cat = cat.value

            namespace = mod.module_name.split(".")
            if namespace[0].lower() == cat.lower():
                namespace.pop(0)

            if cat:
                namespace.insert(0, cat)

            d = items
            for ns in namespace:
                if ns == namespace[-1]:
                    if m := d.get("menu"):
                        action = m.addAction(ns)
                        action.setData(mod)
                    break
                elif ns not in d:
                    d[ns] = {"menu": d.get("menu").addMenu(ns)}
                d = d[ns]

        res = items["menu"].exec_(event.globalPos())

        if not res or not (mod := res.data()):
            return

        if mod.loaded:
            mod.create()
        else:
            mod.load()


class ChunkWidgetDocker(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.build()

        self._chunk_loaded_listener = chunk_notice.NoticeConnection(
            self._loaded_module_callback, chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, globally=True
        )
        self._chunk_unloaded_listener = chunk_notice.NoticeConnection(
            self._unloaded_module_callback, chunk_notice.ChunkNoticeAlias.CHUNK_UNLOADED, globally=True
        )

    def _unloaded_module_callback(self, notice: Tf.Notice, sender: chunk.Chunk):
        pass

    def _loaded_module_callback(self, notice: Tf.Notice, sender: chunk.Chunk):
        """Called whenever CHUNK_LOADED notice was recieved - will in turn add a new chunk widget

        Args:
            notice (chunk_notice.ChunkLoadedNotice): The notice sent from the registry
            sender (chunk.Chunk): The new chunk that was added
        """
        self.add_chunk_widget(sender)

    def build(self):
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.MinimumExpanding, QtWidgets.QSizePolicy.Policy.MinimumExpanding)
        self.chunk_tabs: dict[str, ChunkArea] = {}
        self.tab_widget: ChunkTabWidget = ChunkTabWidget(self)
        registry = chunk_registry.ChunkRegistry()
        self.setStyleSheet("""
        QFrame{
            background-color: #333333;
        }
        QComboBox{
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
            background: #808080;
            height: 10px;
            width: 10px;
            border-radius: 5px;
            margin: -3px 0px;
        }
        QSlider::handle:hover {
            background: #999999;
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
        for mod in registry.registered_modules.values():
            if not mod.loaded:
                continue

            for c in mod.loaded_chunks.values():
                if not self.add_chunk_widget(c):
                    continue

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.tab_widget)

        self.setLayout(self.main_layout)
        self.show()

    def add_chunk_widget(self, loaded_chunk: chunk.Chunk | ChunkWidget) -> ChunkWidget:
        """Adds a ChunkWidget to the docker

        Args:
            loaded_chunk (chunk.Chunk | ChunkWidget): The chunk to add as a widget

        Returns:
            ChunkWidget: The new ChunkWidget created for the loaded_chunk
        """
        widget = None

        if isinstance(loaded_chunk, chunk.Chunk):
            # If the returned value is a Chunk, we need to create the widget here as well
            widget = ChunkWidget(loaded_chunk, self)
        elif isinstance(loaded_chunk, ChunkWidget):
            # If main returned a ChunkWidget, we can use it directly
            widget = loaded_chunk
        else:
            return None

        category = widget.get_category()
        if category not in self.chunk_tabs.keys():
            self.chunk_tabs[category] = ChunkArea(self)
            self.tab_widget.addTab(self.chunk_tabs[category], category)

        self.chunk_tabs[category].flow.addWidget(widget)
        return widget


class ChunkModuleLoader(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.content = QtWidgets.QTableWidget(rowCount=0, columnCount=4)

        self.content.setSelectionMode(QtWidgets.QTableWidget.SelectionMode.ExtendedSelection)
        self.content.setSelectionBehavior(QtWidgets.QTableWidget.SelectionBehavior.SelectRows)
        self.content.setEditTriggers(QtWidgets.QTableWidget.EditTrigger.NoEditTriggers)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.addWidget(self.content)

        self.setLayout(self.main_layout)
        self.load_chunks()

        self.content.model().dataChanged.connect(self.handle_data_change)

    def get_check_state(self, loaded):
        return QtCore.Qt.CheckState.Checked if loaded else QtCore.Qt.CheckState.Unchecked

    def new_row(self, row, mod: chunk_registry.ChunkModule):
        self.content.insertRow(row)

        c0 = QtWidgets.QTableWidgetItem()
        c0.setCheckState(self.get_check_state(mod.loaded))

        c1 = QtWidgets.QTableWidgetItem(mod.module_name)

        self.content.setItem(row, 0, c0)
        self.content.setItem(row, 1, c1)
        if mod.loaded_chunks:
            c2 = QtWidgets.QTableWidgetItem()
            c2.setText(", ".join([x.name for x in mod.loaded_chunks.values()]))
            self.content.setItem(row, 2, c2)
        c3 = QtWidgets.QTableWidgetItem(mod.config.category)
        self.content.setItem(row, 3, c3)

    def load_chunks(self):
        reg = chunk_registry.ChunkRegistry()
        mods = list(reg.registered_modules.values())
        for row, c in enumerate(mods):
            self.new_row(row, c)

    def load_unload(self, mod: chunk_registry.ChunkModule, check):
        if check and not mod.loaded:
            mod.load()
        elif not check and mod.loaded:
            mod.remove_chunk()

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


def build_chunk_docker(parent_window: QtWidgets.QMainWindow):
    # Create an empty dock widget to hold the modules
    module_dock = parent_window.findChild(QtWidgets.QDockWidget, "Chunks")
    if module_dock:
        return

    module_dock = QtWidgets.QDockWidget("Chunks", parent_window)

    module_dock.setWidget(ChunkWidgetDocker(parent=module_dock))
    parent_window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, module_dock)
