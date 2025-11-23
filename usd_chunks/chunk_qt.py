from PySide6 import QtWidgets, QtCore, QtGui
from typing import Any, Dict, Optional, List


from pxr import Tf

from . import chunk, chunk_notice, chunk_registry
from .chunk_interface import ChunkFlags, ChunkParamWidget
from usd_chunks import chunk_interface

class ChunkSliderWidget(ChunkParamWidget[chunk.Slider], QtWidgets.QSlider):
    def __init__(self, param: chunk.Slider):
        super().__init__(param)

        self.valueChanged.connect(self.value_changed)

        self._step = 1000 if chunk.ChunkFlags.ParamType.FLOAT in param.evaluate_type() else 1

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

    def get_value(self):
        return self.value() / self._step

    def set_value(self, value):
        return self.setValue(value * self._step)

    def set_param_layout(self, layout_flags: chunk.ChunkFlags.LayoutFlags):
        is_horizontal = chunk.ChunkFlags.LayoutFlags.HORIZONTAL in layout_flags

        orient = QtCore.Qt.Orientation.Horizontal if is_horizontal else QtCore.Qt.Orientation.Vertical

        self.setOrientation(orient)

    def value_changed(self, value: int | float):
        if not self.param:
            return
        self.param.value = value / self._step


# TODO: Make this widget nice
class ChunkSpinboxWidget(ChunkParamWidget[chunk.Spinbox], QtWidgets.QDoubleSpinBox):
    def __init__(self, param: chunk.Spinbox):
        super().__init__(param)

        self.valueChanged.connect(self.value_changed)

        self._step = 1

        min_val = param.min_value
        max_val = param.max_value

        if min_val is not None:
            self.setMinimum(min_val * self._step)

        if max_val is not None:
            self.setMaximum(max_val * self._step)

        self.setup_chunk_widget()

    def get_value(self):
        return self.value()

    def set_value(self, value):
        return self.setValue(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()


class ChunkLabelWidget(ChunkParamWidget[chunk.Label], QtWidgets.QLabel):
    def __init__(self, param: chunk.Label):
        super().__init__(param)

        self.setup_chunk_widget()

    def get_value(self):
        return self.text()

    def set_value(self, value):
        return self.set_label(value)

    def set_label(self, value):
        return self.setText(value)


class ChunkLineEditWidget(ChunkParamWidget[chunk.LineEdit], QtWidgets.QLineEdit):
    def __init__(self, param: chunk.LineEdit):
        super().__init__(param)

        self.editingFinished.connect(self.value_changed)
        self.setup_chunk_widget()

    def get_value(self):
        return self.text()

    def set_value(self, value):
        return self.setText(value)


class ChunkFileWidget(ChunkParamWidget[chunk.FileBrowser], QtWidgets.QWidget):
    def __init__(self, param: chunk.FileBrowser):
        super().__init__(param)

        self.button = QtWidgets.QToolButton(self)
        self.button.setStyleSheet("border: 0px; padding: 0px;")
        self.button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.button.clicked.connect(self.open_file_dial)

        self.text = QtWidgets.QLineEdit(self)

        self.setup_chunk_widget()

        self.text.editingFinished.connect(self.value_changed)

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

    def get_value(self):
        return self.text.text()

    def set_value(self, value):
        return self.text.setText(value)

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


class ChunkComboBoxWidget(ChunkParamWidget[chunk.Menu], QtWidgets.QComboBox):
    def __init__(self, param: chunk.Menu):
        super().__init__(param)

        self.setup_chunk_widget()

        menu_items = param.menu_items
        if menu_items:
            self.addItems(menu_items)
            val = param.value if param.value is not None else param.default_value
            if val:
                self.set_value(val)
        self.currentTextChanged.connect(self.value_changed)

        if param.default_value:
            self.setCurrentIndex(menu_items.index(param.default_value))

    def get_value(self):
        return self.currentText()

    def set_value(self, value):
        return self.setCurrentText(value)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()


class ChunkCheckBoxWidget(ChunkParamWidget[chunk.Checkbox], QtWidgets.QCheckBox):
    def __init__(self, param: chunk.Checkbox):
        super().__init__(param)

        self.toggled.connect(self.value_changed)

        self.setup_chunk_widget()

    def get_value(self):
        return self.isChecked()

    def set_value(self, value: bool):
        return self.setChecked(value)


class ChunkPushButtonWidget(ChunkParamWidget[chunk.Button], QtWidgets.QPushButton):
    def __init__(self, param: chunk.Button):
        super().__init__(param)
        self.clicked.connect(self.value_changed)

        self.setup_chunk_widget()

    def set_label(self, text):
        self.setText(text)

    def set_icon(self, path: str):
        icon = QtGui.QIcon(path)
        self.setIcon(icon)

    def value_changed(self, _: Any):
        """Override for buttons as the param do not have a value. Better to just send the param notice directly

        Args:
            _ (Any): Ignored
        """
        if self.param:
            self.param.send_self()


class ChunkSeparatorWidget(ChunkParamWidget, QtWidgets.QFrame):
    def __init__(self, param: chunk.Separator):
        super().__init__(param)

        is_horizontal = chunk.ChunkFlags.LayoutFlags.HORIZONTAL in param.layout
        shape = QtWidgets.QFrame.Shape.HLine if is_horizontal else QtWidgets.QFrame.Shape.VLine
        self.setFrameShape(shape)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)

        self.setup_chunk_widget()


class ChunkQTWidgetFactory(chunk_interface.ChunkWidgetFactoryBase):
    widget_map: Dict[ChunkFlags.ParamType, type[QtWidgets.QWidget]] = {
        ChunkFlags.ParamType.FLOAT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.INT_SLIDER: ChunkSliderWidget,
        ChunkFlags.ParamType.FLOAT_SPINBOX: ChunkSpinboxWidget,
        ChunkFlags.ParamType.INT_SPINBOX: ChunkSpinboxWidget,
        ChunkFlags.ParamType.LABEL: ChunkLabelWidget,
        ChunkFlags.ParamType.LINEEDIT: ChunkLineEditWidget,
        ChunkFlags.ParamType.FILE: ChunkFileWidget,
        ChunkFlags.ParamType.STRING_MENU: ChunkComboBoxWidget,
        ChunkFlags.ParamType.CHECKBOX: ChunkCheckBoxWidget,
        ChunkFlags.ParamType.BUTTON: ChunkPushButtonWidget,
        ChunkFlags.ParamType.SEPARATOR: ChunkSeparatorWidget,
    }

    def create_widget(self, param: chunk.ChunkParamBase) -> ChunkParamWidget | None:
        """Create a Qt widget based on the ChunkParam object. the info.get_type() should determine the type of Qt Widget to create

        Args:
            info (ChunkParam): The information about the parameter. The widget will update info.value when widget value is changed

        Returns:
            QtWidgets.QWidget: The QtWidget created
            # None: If failed to create a widget
        """
        key = param.evaluate_type()
        widget_class = self.get_map().get(key, None)

        if not widget_class:
            print("Could not create widget for param: ", param.name, "Missing mapping in Qt factory")
            return None

        widget: ChunkParamWidget = widget_class(param)

        if param.tooltip and isinstance(widget, QtWidgets.QWidget):
            widget.setToolTip(param.tooltip)

        return widget


class FormLabelWidget(QtWidgets.QLabel):
    def __init__(self, text, parent=None):
        super().__init__(parent)

        self.setText(text)

        self.setMargin(3)


class ContainerWidget(QtWidgets.QWidget):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    group_height_changed = QtCore.Signal(float)

    def __init__(
        self,
        group: chunk_interface.ChunkContainerBase,
        parent=None,
    ):
        super().__init__(parent)

        # self._group: weakref.ReferenceType = weakref.ref(group)
        self.group = group

        self.animation_duration = 300
        self._animation_value = 0

        self._callbacks: List[chunk_notice.NoticeConnection] = []

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self.delete_self, chunk_notice.ChunkNoticeAlias.CHUNK_DELETED, group)
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self.param_added_callback, chunk_notice.ChunkNoticeAlias.PARAM_ADDED, group)
        )
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self.param_removed_callback, chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, group)
        )


        # Associate ChunkParamGroup objects with Qt Layouts
        self.param_groups: dict[chunk_interface.ChunkContainerBase, ContainerWidget] = {}

        # # Container for all widgets in this chunk. Dict will map between ChunkParam -> [QtWidgets.QWidget,]
        # # for each widget created from the same ChunkParam instance
        self.param_widgets: dict[chunk.ChunkParamBase, Any] = {}

        # Factory for creating the widgets
        if self.group.widget_factory:
            self.factory: chunk_interface.ChunkWidgetFactoryBase = self.group.widget_factory
        else:
            self.factory: chunk_interface.ChunkWidgetFactoryBase = ChunkQTWidgetFactory()

        self.build_collapsable()
        self.build_param_widgets(self.group)
        self.update_from_flags()
        self.update_from_state()

    # @property
    # def group(self) -> chunk_interface.ChunkContainerBase:
    #     g = self._group()
    #     if not g:
    #         raise ReferenceError("Group is deleted!")
    #     return g

    def delete_self(self, notice, sender):
        """
        The group should be deleted, so call deleteLater on self

        Args:
            notice (_type_): _description_
            sender (_type_): _description_
        """
        if not chunk_notice.is_notice_type(notice, chunk_notice.ChunkNoticeAlias.CHUNK_DELETED):
            return

        if sender is not self.group:
            return

        self.param_groups.clear()
        self.param_widgets.clear()

        self.setParent(None)
        self.deleteLater()

    def remove_param(self, param: chunk_interface.ChunkParamBase):
        if param not in self.param_widgets:
            return
        wid = None

        if isinstance(param, chunk_interface.ChunkParamBase):
            wid = self.param_widgets.get(param)

        if not wid:
            return

        self.content_layout.removeRow(wid)
        del self.param_widgets[param]

    def param_added_callback(self, notice, sender: chunk_interface.ChunkContainerBase):
        h = sender.children.last_added
        if not h:
            return
        param = sender.get_param(h)
        if not param:
            return

        self.add_param(param)

    def param_removed_callback(self, notice, sender: chunk_interface.ChunkContainerBase):
        h = sender.children.last_removed
        if not h:
            return

        param = None

        for p in self.param_widgets.keys():
            if p == h:
                param = p
                break

        if not param:
            return

        self.remove_param(param)

    def state_changed(self, notice, sender):
        self.update_from_state()


    @QtCore.Property(float)
    def animation_value(self):  # type: ignore
        return self._animation_value

    @animation_value.setter
    def animation_value(self, val):
        self._animation_value = val
        scale = max(self._animation_value / self.animation_duration, 0)

        h = self.content_layout.sizeHint().height()
        self.content.setFixedHeight(h * scale)

    def is_collapsed(self) -> bool:
        val = self.group.has_state(ChunkFlags.State.COLLAPSED)
        parent = self.group.get_owner()
        if parent and isinstance(parent, chunk_interface.ChunkContainerBase):
            val |= parent.has_state(ChunkFlags.State.COLLAPSED)
        return val

    def build_collapsable(self):
        self.content = QtWidgets.QScrollArea()
        self.content.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored)

        self.content_layout = QtWidgets.QFormLayout()
        self.content_layout.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
        self.content_layout.setContentsMargins(2, 2, 2, 2)
        self.content_layout.setHorizontalSpacing(0)

        self.content.setLayout(self.content_layout)

        # Setup minimize/maximize button
        self.minimize_button = QtWidgets.QToolButton()
        self.minimize_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.minimize_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Maximum)

        # Used when collapsable
        self.toggle_anim = QtCore.QPropertyAnimation(self, b"animation_value")
        self.toggle_anim.setStartValue(0)
        self.toggle_anim.setEndValue(self.animation_duration)

        self.__layout__ = QtWidgets.QVBoxLayout()
        self.__layout__.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.__layout__.setContentsMargins(10, 3, 0, 0)

        self.__layout__.addWidget(self.minimize_button)
        self.__layout__.addWidget(self.content)

        self.setLayout(self.__layout__)

    def animation_finished(self):
        """Modify the layout sizeConstraint when collapsed. Otherwise the child groups of collapsed ContainerWidgets will scale the widget when addWidget is called on the cild"""
        if self.is_collapsed():
            self.content_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        else:
            self.content_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)

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

        if chunk.ChunkFlags.GroupFlags.LABELED in self.group.flags:
            self.minimize_button.setText(self.group.get_label())
        else:
            self.minimize_button.setText("")

        if chunk.ChunkFlags.GroupFlags.FRAMED in self.group.flags:
            self.content.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
            self.content.setLineWidth(2)
        else:
            self.content.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

    def update_from_state(self):
        self.setHidden(self.group.has_state(ChunkFlags.State.HIDDEN))
        self.setEnabled(not self.group.has_state(ChunkFlags.State.DISABLED))

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

    def addWidget(self, widget: QtWidgets.QWidget, label=None):
        if label is None:
            self.content_layout.addRow(widget)
        else:
            self.content_layout.addRow(label, widget)


    def update_label(self, param: chunk_interface.ChunkCommon, notice, sender: chunk_interface.AttributeSender):
        """
        When a param name, label or flag is changed, this will correct the label in the FormLayout.
        If LABELED was unset, then remove the label and set the widget role to SpanningRole
        If LABELED was set, add the label
        otherwise, just update the label text if name or label changed

        Args:
            param (chunk_interface.ChunkCommon): The param that was changed
            notice (Tf.Notice): The notice that was sent
            sender (chunk_interface.AttributeSender): The attribute that changed
        """
        labeled = param.has_flag(ChunkFlags.ParamFlags.LABELED)

        for i in range(self.content_layout.rowCount()):
            item = self.content_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.FieldRole)
            if not item:
                item = self.content_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.SpanningRole)

            if not item:
                continue

            w = item.widget()
            if isinstance(w, ChunkParamWidget) and w.param == param:
                label = self.content_layout.labelForField(w)

                if isinstance(label, QtWidgets.QLabel) and labeled:
                    label.setText(param.get_label())
                elif label is not None and not labeled:
                    self.content_layout.setWidget(i, QtWidgets.QFormLayout.ItemRole.SpanningRole, w)
                    label.deleteLater()
                elif label is None and labeled:
                    self.content_layout.setWidget(
                        i, QtWidgets.QFormLayout.ItemRole.LabelRole, FormLabelWidget(param.get_label(), w)
                    )
                    self.content_layout.setWidget(i, QtWidgets.QFormLayout.ItemRole.FieldRole, w)

    def add_param_widget(self, info: chunk.ChunkParamBase, group: Optional[chunk_interface.ChunkContainerBase] = None):
        """Add a ChunkParamWidget from a ChunkParam object. If a ChunkParamGroup is specified, the created widget will be added to the group layout

        Args:
            info (chunk.ChunkParam): The ChunkParam to represent as a widget
            group (chunk.ChunkParamGroup, optional): A group that contains the param. If specified, will add the param widget to the group layout. Defaults to None.
        """

        t = info.evaluate_type()
        if t == ChunkFlags.ParamType.NONE:
            return

        widget: Optional[QtWidgets.QWidget] = None
        # Custom widget is created and managed externally
        if (
            t == ChunkFlags.ParamType.CUSTOM
            and info.custom_widget
            and isinstance(info.custom_widget, QtWidgets.QWidget)
        ):
            widget = info.custom_widget
        else:
            if not self.factory.has_mapping(t):
                print("Missing mapping in factory for creating widget", info.name)
                return

            # Create a widget from the factory based on the info.get_type()
            widget = self.factory.create_widget(info)

        if not widget:
            return

        self._callbacks.extend(
            [
                chunk_notice.NoticeConnection(
                    self.update_label,
                    chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                    info.get_attribute_sender("name"),
                    send_owner=True,
                    owner=info,
                ),
                chunk_notice.NoticeConnection(
                    self.update_label,
                    chunk_notice.ChunkNoticeAlias.GROUP_CHANGED,
                    info.get_attribute_sender("name"),
                    send_owner=True,
                    owner=info,
                ),
                chunk_notice.NoticeConnection(
                    self.update_label,
                    chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                    info.get_attribute_sender("label"),
                    send_owner=True,
                    owner=info,
                ),
                chunk_notice.NoticeConnection(
                    self.update_label,
                    chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED,
                    info.get_attribute_sender("flags"),
                    send_owner=True,
                    owner=info,
                ),
            ]
        )

        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Minimum)

        if group is not None:
            layout = self.add_group_layout(group)
        else:
            layout = self

        self.param_widgets[info] = widget

        if info.has_flag(ChunkFlags.ParamFlags.LABELED):
            layout.addWidget(widget, FormLabelWidget(info.get_label(), widget))
        else:
            layout.addWidget(widget)


    def add_group_layout(self, group: chunk_interface.ChunkContainerBase):
        """Adds a group of parameters to a shared layout

        Args:
            info (ChunkGroupInfo): The group containing the parameters
        """

        container = self.param_groups.get(group, None)
        if not container:
            container = ContainerWidget(group, self)
            self.addWidget(container)
            self.param_groups[group] = container

        return container

    def build_param_widgets(self, info: chunk_interface.ChunkContainerBase):
        """Build params from a ChunkInfo or ChunkGroupInfo. Will generate the parameter widgets and display them in the Chunk

        Args:
            info (ChunkGroupInfo | ChunkInfo): The info class for the Chunk or ChunkGroup. Will create parameters from each ChunkParamInfo in the parameters member list
            layout (QtWidgets.QLayout, optional): A layout to add the parameters to. Usually not needed but used by recursion when adding groups. Defaults to None.
        """
        for param in info.get_my_params():
            self.add_param(param)

    def add_param(self, param: chunk_interface.ChunkCommon):
        if isinstance(param, chunk_interface.ChunkContainerBase):
            self.add_group_layout(param)
        elif isinstance(param, chunk_interface.ChunkParamBase):
            self.add_param_widget(param)

    def has_param_widget(self, param: chunk.ChunkParamBase | str) -> bool:
        """Check to see if param exists in this Chunk widget

        Args:
            param (chunk.ChunkParam | str): The parameter to look for. Either name of param, or ChunkParam object

        Returns:
            bool: True if found, otherwise False
        """
        return self.get_param_widget(param) is not None

    def get_param_widget(self, param: chunk.ChunkParamBase | str) -> ChunkParamWidget | None:
        """Get the ChunkParamWidget for a ChunkParam. If a string is passed as param, then look for the associated name

        Args:
            param (ChunkParam | str): The name of the param, or ChunkParam object, to find

        Returns:
            ChunkParamWidget | None: The found ChunkParamWidget if found, otherwise None
        """
        if isinstance(param, chunk.ChunkParamBase):
            return self.param_widgets.get(param, None)

        for bound_param, widget in self.param_widgets.items():
            if bound_param.name == param:
                return widget

    def has_group_layout(self, group: chunk.ChunkGroupBase | str) -> bool:
        """Check to see if group exists in this Chunk widget

        Args:
            group (chunk.ChunkParamGroup | str): The group to look for. Either name of group, or ChunkParamGroup object

        Returns:
            bool: True if found, otherwise False
        """
        return self.get_group_layout(group) is not None

    def get_group_layout(self, group: chunk.ChunkGroupBase | str) -> Optional["ContainerWidget"]:
        """Get the QLayout object for a ChunkParamGroup. If a string is passed as param, then look for the associated name

        Args:
            group (ChunkParamGroup | str): The name of the group, or ChunkParamGroup object, to find

        Returns:
            QLayout | None: The found QLayout associated with group. If not found, returns None
        """
        if isinstance(group, chunk.ChunkGroupBase):
            return self.param_groups.get(group, None)

        for bound_group, layout in self.param_groups.items():
            if bound_group.name == group:
                return layout


class ChunkWidget(ContainerWidget):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    group: chunk.Chunk

    def __init__(
        self,
        group: chunk.Chunk,
        parent=None,
    ):
        super().__init__(group, parent)

    def get_category(self) -> str:
        """Get the category of this Chunk

        Returns:
            str: Category of this Chunk
        """
        return self.group.category


class ChunkArea(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.flow = QtWidgets.QVBoxLayout()
        self.flow.setContentsMargins(0, 5, 0, 0)

        self.flow.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.widget_holder = QtWidgets.QWidget(self)
        self.widget_holder.setLayout(self.flow)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

        self.setWidget(self.widget_holder)
        self.setWidgetResizable(True)


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
        self.chunk_tabs: dict[str, ChunkArea] = {}
        self.tab_widget: QtWidgets.QTabWidget = QtWidgets.QTabWidget(self)
        registry = chunk_registry.ChunkRegistry()

        for module_name, mod in registry.registered_modules.items():
            loaded_chunk = mod.loaded_chunk
            if not loaded_chunk or mod.loaded:
                continue

            if not self.add_chunk_widget(loaded_chunk):
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


def build_chunk_docker(parent_window: QtWidgets.QMainWindow):
    # Create an empty dock widget to hold the modules
    module_dock = parent_window.findChild(QtWidgets.QDockWidget, "Chunks")
    if module_dock:
        return

    module_dock = QtWidgets.QDockWidget("Chunks", parent_window)

    widget_docker = ChunkWidgetDocker(parent=parent_window)
    module_dock.setWidget(widget_docker)
    parent_window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, module_dock)
