from PySide6 import QtWidgets, QtCore, QtGui
from typing import List, Callable, Any, Type

from abc import abstractmethod

from pxr import Tf

from . import chunk_notice, chunk_registry, chunk_templates, chunk


class ChunkParamWidget:
    """Base class for a chunk param widget. Creating a custom widget should subclass
    the relevant Qt widget as well as this class, such as:
    ```
        class myWidget(QtWidgets.QPushButton, ChunkParamWidget)
    ```
    The subclass should override the relevant methods from this class
    """

    def __init__(self):
        self.param: chunk.ChunkParam = None

    def param_changed_callback(self, chunkItem: chunk.ChunkParamBase, notice, param: chunk.ChunkParam):
        if param.value is not None:
            self.set_value(param.value)

    def setup_chunk_widget(self, param: chunk.ChunkParam):
        self.param = param
        value = self.param.value if self.param.value is not None else self.param.default_value
        label = self.param.label if self.param.label else self.param.name

        self.set_value(value)
        self.set_label(label)

        if self.param.icon:
            self.set_icon(self.param.icon)

        self.set_param_layout(param.layout_flags)

        self._connection = chunk_notice.NoticeConnection(
            self.param_changed_callback, chunk_notice.NoticeType.PARAM_CHANGED, param, chunk_item=param
        )

    @abstractmethod
    def get_value(self):
        return None

    @abstractmethod
    def set_value(self, value: Any):
        pass

    @abstractmethod
    def set_label(self, text: str):
        pass

    @abstractmethod
    def set_icon(self, icon: str):
        pass

    @abstractmethod
    def set_param_layout(self, layout_flags: chunk.ChunkLayoutFlags):
        pass

    def value_changed(self, value: Any):
        self.param.value = value


class ChunkSliderWidget(QtWidgets.QSlider, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)

        self.valueChanged.connect(self.value_changed)

        self._step = 1000 if chunk_templates.ChunkParamType.FLOAT in param.get_type() else 1

        min_val = param.template.min_value
        max_val = param.template.max_value

        if min_val is not None:
            self.setMinimum(min_val * self._step)
        else:
            self.setMinimum(0)

        if max_val is not None:
            self.setMaximum(max_val * self._step)
        else:
            self.setMaximum(10 * self._step)

        self.setup_chunk_widget(param)

    def get_value(self):
        return self.value() / self._step

    def set_value(self, value):
        return self.setValue(value * self._step)

    def set_param_layout(self, layout_flags: chunk.ChunkLayoutFlags):
        is_horizontal = chunk.ChunkLayoutFlags.HORIZONTAL in layout_flags

        orient = QtCore.Qt.Orientation.Horizontal if is_horizontal else QtCore.Qt.Orientation.Vertical

        self.setOrientation(orient)

    def value_changed(self, value):
        self.param.value = value / self._step


# TODO: Make this widget nice
class ChunkSpinboxWidget(QtWidgets.QDoubleSpinBox, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)

        self.valueChanged.connect(self.value_changed)

        self._step = 1

        min_val = param.template.min_value
        max_val = param.template.max_value

        if min_val is not None:
            self.setMinimum(min_val * self._step)

        if max_val is not None:
            self.setMaximum(max_val * self._step)

        self.setup_chunk_widget(param)

    def get_value(self):
        return self.value()

    def set_value(self, value):
        return self.setValue(value)


class ChunkLabelWidget(QtWidgets.QLabel, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)
        self.setup_chunk_widget(param)

    def get_value(self):
        return self.text()

    def set_value(self, value):
        return self.set_label(value)

    def set_label(self, value):
        return self.setText(value)


class ChunkLineEditWidget(QtWidgets.QLineEdit, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)

        self.textChanged.connect(self.value_changed)
        self.setup_chunk_widget(param)

    def get_value(self):
        return self.text()

    def set_value(self, value):
        return self.setText(value)


class ChunkComboBoxWidget(QtWidgets.QComboBox, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)

        self.currentTextChanged.connect(self.value_changed)
        self.setup_chunk_widget(param)

        menu_items = self.param.get_template().menu_items
        if menu_items:
            self.addItems(menu_items)

    def get_value(self):
        return self.currentText()

    def set_value(self, value):
        return self.setCurrentText(value)


class ChunkCheckBoxWidget(QtWidgets.QCheckBox, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)
        self.toggled.connect(self.value_changed)
        self.setup_chunk_widget(param)

    def get_value(self):
        return self.isChecked()

    def set_value(self, value):
        return self.setChecked(value)


class ChunkPushButtonWidget(QtWidgets.QPushButton, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)
        self.clicked.connect(self.value_changed)
        self.setup_chunk_widget(param)

    def set_label(self, text):
        self.setText(text)

    def set_icon(self, path: str):
        icon = QtGui.QIcon(path)
        self.setIcon(icon)

    def value_changed(self, value):
        # Just emit the ParamChangeNotice since a button does not have a value
        chunk_notice.ParamChangeNotice().Send(self.param)


class ChunkSeparatorWidget(QtWidgets.QFrame, ChunkParamWidget):
    def __init__(self, param: chunk.ChunkParam, parent=None):
        super().__init__(parent)

        is_horizontal = chunk.ChunkLayoutFlags.HORIZONTAL in param.layout_flags
        shape = QtWidgets.QFrame.Shape.HLine if is_horizontal else QtWidgets.QFrame.Shape.VLine
        self.setFrameShape(shape)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        self.setup_chunk_widget(param)


class ChunkParamWidgetFactory:
    def __init__(self):
        self.widget_map: dict = {
            chunk_templates.ChunkParamType.FLOAT_SLIDER: ChunkSliderWidget,
            chunk_templates.ChunkParamType.INT_SLIDER: ChunkSliderWidget,
            chunk_templates.ChunkParamType.FLOAT_SPINBOX: ChunkSpinboxWidget,
            chunk_templates.ChunkParamType.INT_SPINBOX: ChunkSpinboxWidget,
            chunk_templates.ChunkParamType.LABEL: ChunkLabelWidget,
            chunk_templates.ChunkParamType.LINEEDIT: ChunkLineEditWidget,
            chunk_templates.ChunkParamType.FILE: ChunkLineEditWidget,
            chunk_templates.ChunkParamType.STRING_MENU: ChunkComboBoxWidget,
            chunk_templates.ChunkParamType.CHECKBOX: ChunkCheckBoxWidget,
            chunk_templates.ChunkParamType.BUTTON: ChunkPushButtonWidget,
            chunk_templates.ChunkParamType.SEPARATOR: ChunkSeparatorWidget,
        }

    def get_map(self) -> dict:
        return self.widget_map

    def add_mapping(self, key: chunk_templates.ChunkParamType, widget_type: Type[ChunkParamWidget]):
        self.widget_map[key] = widget_type

    def has_mapping(self, key: chunk_templates.ChunkParamType):
        return key in self.get_map()

    def create_widget(self, param: chunk.ChunkParam) -> ChunkParamWidget:
        """Create a Qt widget based on the ChunkParam object. the info.get_type() should determine the type of Qt Widget to create

        Args:
            info (ChunkParam): The information about the parameter. The widget will update info.value when widget value is changed

        Returns:
            QtWidgets.QWidget: The QtWidget created
            # None: If failed to create a widget
        """
        key = param.get_type()
        widget_class = self.get_map().get(key, None)
        if not widget_class:
            print("Could not create widget for param: ", param.name)
            return None

        widget: ChunkParamWidget = widget_class(param)

        if param.tooltip:
            widget.setToolTip(param.tooltip)

        return widget


class ChunkWidget(QtWidgets.QWidget):
    """
    Chunk is a base class for creating UI components.
    It provides a structure for initializing the API and creating UI Chunks.
    """

    def __init__(
        self,
        chunk_info: chunk.Chunk = chunk.Chunk(),
        parent=None,
    ):
        super().__init__(parent)

        # Associate ChunkParamGroup objects with Qt Layouts
        self.param_groups: dict[chunk.ChunkParamGroup, QtWidgets.QLayout] = {}

        # Container for all widgets in this chunk. Dict will map between ChunkParam -> [QtWidgets.QWidget,]
        # for each widget created from the same ChunkParam instance
        self.param_widgets: dict[chunk.ChunkParam, QtWidgets.QWidget] = {}

        # Hold the Chunk instance
        self.chunk = chunk_info

        # Factory for creating the widgets
        if self.chunk.widget_factory:
            self.factory: ChunkParamWidgetFactory = self.chunk.widget_factory
        else:
            self.factory: ChunkParamWidgetFactory = ChunkParamWidgetFactory()

        # Holds a temporary layout for nesting groups
        self._groupParentLayout = None

        self.build_chunk()
        self.build_param_widgets(self.chunk)

    def build_chunk(self):
        # Setup chunk content area
        self.content = QtWidgets.QScrollArea()
        self.content.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.content.setMaximumHeight(0)
        self.content.setMinimumHeight(0)
        self.content_layout = QtWidgets.QVBoxLayout()
        self.content.setLayout(self.content_layout)

        # Setup toggle minimize/maximize animation.
        self.toggle_anim = QtCore.QParallelAnimationGroup()
        self.toggle_anim.addAnimation(QtCore.QPropertyAnimation(self, b"minimumHeight"))
        self.toggle_anim.addAnimation(QtCore.QPropertyAnimation(self, b"maximumHeight"))
        self.toggle_anim.addAnimation(QtCore.QPropertyAnimation(self.content, b"maximumHeight"))

        # Setup minimize/maximize button
        self.minimize_button = QtWidgets.QToolButton()
        self.minimize_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.minimize_button.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding, QtWidgets.QSizePolicy.Maximum)

        self.minimize_button.setArrowType(QtCore.Qt.RightArrow)
        self.minimize_button.setText(self.chunk.name)
        self.minimize_button.setCheckable(True)
        self.minimize_button.setChecked(False)
        self.minimize_button.clicked.connect(self._animate)

        self.__layout__ = QtWidgets.QVBoxLayout()
        self.__layout__.addWidget(self.minimize_button)
        self.__layout__.addWidget(self.content)
        self.setLayout(self.__layout__)
        self.setMinimumSize(300, 10)

        if self.chunk.tooltip:
            self.setToolTip(self.chunk.tooltip)

    def _animate(self, checked):
        arrow_type = QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow
        direction = QtCore.QAbstractAnimation.Forward if checked else QtCore.QAbstractAnimation.Backward
        self.minimize_button.setArrowType(arrow_type)
        self.toggle_anim.setDirection(direction)
        self.toggle_anim.start()

    def update_anim(self):
        self.minimize_button.setText(self.chunk.name)
        collapsed_height = self.sizeHint().height() - self.content.maximumHeight()
        content_height = self.content_layout.sizeHint().height()
        for i in range(self.toggle_anim.animationCount() - 1):
            animation = self.toggle_anim.animationAt(i)
            animation.setDuration(self.chunk.animation_duration)
            animation.setStartValue(collapsed_height)
            animation.setEndValue(collapsed_height + content_height)
        content_animation = self.toggle_anim.animationAt(self.toggle_anim.animationCount() - 1)
        content_animation.setDuration(self.chunk.animation_duration)
        content_animation.setStartValue(0)
        content_animation.setEndValue(content_height)

    def _get_or_create_layout(self, group: chunk.ChunkParamGroup):
        """Create a a layout from group"""

        parent_layout = self._groupParentLayout

        # Use content_layout for groups without specified parent
        if not parent_layout:
            parent_layout = self.content_layout

        layout = self.param_groups.get(group, None)
        if not layout:
            if group.layout_flags is chunk.ChunkLayoutFlags.HORIZONTAL:
                layout = QtWidgets.QHBoxLayout()
            elif group.layout_flags is chunk.ChunkLayoutFlags.VERTICAL:
                layout = QtWidgets.QVBoxLayout()

            if not layout:
                return parent_layout

            if group.label:
                layout.addWidget(QtWidgets.QLabel(group.label))

            parent_layout.addLayout(layout)
            self.param_groups[group] = layout

        return layout

    def add_param_widget(self, info: chunk.ChunkParam, group: chunk.ChunkParamGroup = None):
        """Add a ChunkParamWidget from a ChunkParam object. If a ChunkParamGroup is specified, the created widget will be added to the group layout

        Args:
            info (chunk.ChunkParam): The ChunkParam to represent as a widget
            group (chunk.ChunkParamGroup, optional): A group that contains the param. If specified, will add the param widget to the group layout. Defaults to None.
        """
        if info.get_type() == chunk_templates.ChunkParamType.NONE:
            return

        # TODO: info is mutable, so duplicate entries will mutate. Should we force a copy of the info before adding?
        # info.name = self.get_unique_param_name(info.name)

        widget = None

        # Custom widget is created and managed externally
        if info.get_type() == chunk_templates.ChunkParamType.CUSTOM:
            widget = info.custom_widget
        else:
            if not self.factory.has_mapping(info.get_type()):
                print("Missing rules for creating param", info.name)
                return

            # Create a widget from the factory based on the info.get_type()
            widget = self.factory.create_widget(info)

        if not widget:
            return

        if group is not None:
            layout = self._get_or_create_layout(group)
        else:
            layout = self.content_layout

        self.param_widgets[info.name] = widget
        layout.addWidget(widget)

    def add_group_layout(self, group: chunk.ChunkParamGroup):
        """Adds a group of parameters to a shared layout

        Args:
            info (ChunkGroupInfo): The group containing the parameters
        """
        for param in group.get_params():
            self.add_param_widget(param, group)

        for nested in group.get_groups():
            # Enanbles nested groups
            self._groupParentLayout = self._get_or_create_layout(nested)
            self.add_group_layout(nested)

    def build_param_widgets(self, info: chunk.Chunk):
        """Build params from a ChunkInfo or ChunkGroupInfo. Will generate the parameter widgets and display them in the Chunk

        Args:
            info (ChunkGroupInfo | ChunkInfo): The info class for the Chunk or ChunkGroup. Will create parameters from each ChunkParamInfo in the parameters member list
            layout (QtWidgets.QLayout, optional): A layout to add the parameters to. Usually not needed but used by recursion when adding groups. Defaults to None.
        """
        for param in info.get_params():
            self.add_param_widget(param)
        for group in info.get_groups():
            self.add_group_layout(group)

        self.update_anim()

    def get_category(self) -> str:
        """Get the category of this Chunk

        Returns:
            str: Category of this Chunk
        """
        return self.chunk.category

    def has_param_widget(self, param: chunk.ChunkParam | str) -> bool:
        """Check to see if param exists in this Chunk widget

        Args:
            param (chunk.ChunkParam | str): The parameter to look for. Either name of param, or ChunkParam object

        Returns:
            bool: True if found, otherwise False
        """
        return self.get_param_widget(param) is not None

    def get_param_widget(self, param: chunk.ChunkParam | str) -> ChunkParamWidget | None:
        """Get the ChunkParamWidget for a ChunkParam. If a string is passed as param, then look for the associated name

        Args:
            param (ChunkParam | str): The name of the param, or ChunkParam object, to find

        Returns:
            ChunkParamWidget | None: The found ChunkParamWidget if found, otherwise None
        """
        if isinstance(param, chunk.ChunkParam):
            return self.param_widgets.get(param, None)

        for bound_param, widget in self.param_widgets.items():
            if bound_param.name == param:
                return widget

    def has_group_layout(self, group: chunk.ChunkParamGroup | str) -> bool:
        """Check to see if group exists in this Chunk widget

        Args:
            group (chunk.ChunkParamGroup | str): The group to look for. Either name of group, or ChunkParamGroup object

        Returns:
            bool: True if found, otherwise False
        """
        return self.get_group_layout(group) is not None

    def get_group_layout(self, group: chunk.ChunkParamGroup | str) -> QtWidgets.QLayout | None:
        """Get the QLayout object for a ChunkParamGroup. If a string is passed as param, then look for the associated name

        Args:
            group (ChunkParamGroup | str): The name of the group, or ChunkParamGroup object, to find

        Returns:
            QLayout | None: The found QLayout associated with group. If not found, returns None
        """
        if isinstance(group, chunk.ChunkParamGroup):
            return self.param_groups.get(group, None)

        for bound_group, layout in self.param_groups.items():
            if bound_group.name == group:
                return layout


class ChunkArea(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.flow = QtWidgets.QVBoxLayout()
        self.flow.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        self.widget_holder = QtWidgets.QWidget(self)
        self.widget_holder.setLayout(self.flow)

        self.setWidgetResizable(True)
        self.setWidget(self.widget_holder)


class ChunkWidgetDocker(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.build()

        self._chunk_loaded_listener: chunk_notice.NoticeConnection = chunk_notice.NoticeConnection(
            self._loaded_module_callback, chunk_notice.NoticeType.CHUNK_LOADED, globally=True
        )

    def _loaded_module_callback(self, notice: chunk_notice.ChunkLoadedNotice, sender: chunk.Chunk):
        """Called whenever CHUNK_LOADED notice was recieved - will in turn add a new chunk widget

        Args:
            notice (chunk_notice.ChunkLoadedNotice): The notice sent from the registry
            sender (chunk.Chunk): The new chunk that was added
        """
        self.add_chunk_widget(sender)

    def build(self):
        self.chunk_tabs: dict[str, ChunkArea] = {}
        self.tab_widget: QtWidgets.QTabWidget = QtWidgets.QTabWidget(self)

        self.tab_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

        registry = chunk_registry.ChunkRegistry()
        for chunk_module in registry.registered_modules:
            loaded_chunk = chunk_module.loaded_chunk
            if not loaded_chunk:
                continue

            if not self.add_chunk_widget(loaded_chunk):
                continue

        self.main_layout = QtWidgets.QVBoxLayout()
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

        widget.minimize_button.setChecked(True)
        widget._animate(True)
        self.chunk_tabs[category].flow.addWidget(widget)

        return widget


def build_chunk_docker(parent_window: QtWidgets.QMainWindow):
    # Create an empty dock widget to hold the modules
    module_dock = QtWidgets.QDockWidget("Chunks", parent_window)
    widget_docker = ChunkWidgetDocker(parent=parent_window)
    module_dock.setWidget(widget_docker)
    parent_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, module_dock)
    return widget_docker
