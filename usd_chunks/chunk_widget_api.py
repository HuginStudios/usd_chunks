"""The contracts a UI toolkit implements to draw a chunk's params.

Two classes, lifted out of `chunk_core` because that is a five-thousand-line file and a
frontend author needs exactly this part of it: what a param widget must provide, and how a
factory maps a param type to a widget. `chunk_qt` implements them, `chunk_flet` implements
them, and so would ImGui, QML or a Blender panel.

ChunkContainerWidgetBase deliberately stayed behind. It refers to the param and group model
-- ChunkContainerBase, ChunkListGroupBase, ChunkParamBase -- so moving it here would only
relocate that dependency, and chunk_core would then have to import this module while this
module imported chunk_core.

The dependency runs one way: chunk_core imports these, not the reverse. That is forced,
not chosen -- ChunkParamBase annotates a custom_widget field with ChunkParamWidgetBase, and
attrs resolves that annotation when the class is built.

This is a comprehension split, not a weight one. The obvious hope -- a frontend taking the
contracts without pulling in USD -- does not hold: ChunkParamWidgetBase is a
ChunkNoticePropagator, the notice system is built on Tf.Notice, and Tf is pxr. Decoupling
notices from USD is a larger job, and until someone does it a widget contract cannot be
USD-free.
"""
from __future__ import annotations

from dataclasses import dataclass

import weakref
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from usd_chunks import chunk_notice, chunk_utils

if TYPE_CHECKING:
    # One way only: chunk_core imports this module, never the reverse at runtime.
    from usd_chunks.chunk_core import ChunkParamBase
from usd_chunks.chunk_flags import ChunkFlags

ParamT = TypeVar("ParamT")


class ChunkParamWidgetBase(Generic[ParamT], chunk_notice.ChunkNoticePropagator):
    """Base class for a chunk param widget, the bridge between a param and a UI toolkit.

    Any UI element associated with a chunk or parameter should subclass this. A custom
    widget subclasses the relevant toolkit widget as well as this class::

        class MyWidget(QtWidgets.QPushButton, ChunkParamWidgetBase):
            ...

    and overrides the relevant methods from this class.
    """

    def __init__(self, param: ParamT, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Store a weakref to the parameter in order to not take ownership. The param should be managed by
        # the chunks
        self._param: weakref.ReferenceType = weakref.ref(param)

        # If the param is inside a vector group, the vector index corresponding to the param needs to be set.
        self.index = 0

        self.add_callback(
            self.handle_param_changed,
            param,
            notice_alias=chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
            send_on_create=True,
        )

    @property
    def param(self) -> ParamT:
        """The param this widget shows.

        Raises:
            ReferenceError: The param has been garbage collected.
        """
        if param := self._param():
            return param
        raise ReferenceError("reference to param is dead! :'(")

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        """The alias this widget's notices go out under: WIDGET_CHANGED."""
        return chunk_notice.ChunkNoticeAlias.WIDGET_CHANGED

    def handle_param_changed(self, notice: chunk_notice.AttributeChangeNotice, sender: ChunkParamBase):
        """Update the widget when its param's value, name, label or icon changes.

        Args:
            notice (chunk_notice.ChunkNotice): The notice sent for the change
            sender (ChunkParamBase): The sender
        """
        attrname = notice.data.name
        value = notice.data.value
        if attrname == "value":
            self.set_value(self.param.raw_value)

        elif attrname in ["name", "label"]:
            self.set_label(self.param.get_label())

        elif attrname == "icon" and isinstance(value, str):
            self.set_icon(value)

        elif attrname == "state":
            self.update_from_state()

        elif attrname == "layout" and isinstance(value, ChunkFlags.LayoutFlags):
            self.set_param_layout(value)

        self.send_self(data=notice.data)

    def setup_chunk_widget(self):
        """Initialize the widget's value and label from the param."""
        value = self.param.raw_value
        label = self.param.get_label()

        if value is not None:
            self.set_value(value)
        self.set_label(label)

        if self.param.icon:
            self.set_icon(self.param.icon)

        self.set_param_layout(self.param.layout)

    @abstractmethod
    def get_value(self) -> Any:
        """Need to be implemented in a subcclass and return the value of the widget.

        Returns:
            Any: The value of the widget
        """
        return None

    @abstractmethod
    def set_value(self, value: Any):
        """Show a value in the widget. Implemented by a subclass.

        This should only set the value of the widget, not modify the chunk object, as that would
        result in an infinite loop.

        Args:
            value (Any): The new value of the widget
        """
        pass

    @abstractmethod
    def set_label(self, text: str):
        """Show a label on the widget. Implemented by a subclass.

        This should only set the label of the widget, not modify the chunk object, as that would
        result in an infinite loop.

        Args:
            text (str): The new label of the widget
        """
        pass

    @abstractmethod
    def set_icon(self, icon: str):
        """Show an icon on the widget. Implemented by a subclass.

        This should only set the icon of the widget, not modify the chunk object, as that would
        result in an infinite loop.

        Args:
            icon (str): A path to an image file which should be used as icon for this widget
        """
        pass

    @abstractmethod
    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        """Apply the param's LayoutFlags to the widget.

        This can be ignored if the widget does not support the layout flags.

        Args:
            layout_flags (ChunkFlags.LayoutFlags): The LayoutFlags of the parameter which the widget should respect
        """
        pass

    @abstractmethod
    def set_hidden(self, val: bool):
        """Hide or show the widget.

        Args:
            val (bool): Whether to hide or show the widget

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def set_enabled(self, val: bool):
        """Enable or disable the widget.

        Args:
            val (bool): Whether to enable or disable the widget

        Raises:
            NotImplementedError: A subclass does not implement it.
        """
        raise NotImplementedError

    @abstractmethod
    def update_from_state(self):
        """Apply the param's HIDDEN and DISABLED state, and its OUTPUT flag, to the widget."""
        self.set_hidden(self.param.has_state(ChunkFlags.State.HIDDEN))
        is_output = self.param.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        self.set_enabled(not is_output and not self.param.has_state(ChunkFlags.State.DISABLED))

    def value_changed(self, value: Any | None = None):
        """Forward a value the user entered in the widget to the param.

        Ignored while the param is solving its own attributes, so a value it just pushed to the
        widget does not come straight back.

        Args:
            value (Any, optional): The new value set on the widget. Defaults to None, which
                reads the widget's current value.
        """
        if self.param is not None:
            if self.param._solving_attributes:
                return
            # A wire owns the value (chunk_utils.is_wire_locked): a frontend that still let
            # the widget move must not replace the wire with what the widget says.
            if chunk_utils.is_wire_locked(self.param):
                return

            if value is None:
                value = self.get_value()
            self.param.value = value


class ChunkWidgetFactoryBase(Protocol):
    """Creates widgets for chunk params, for one UI toolkit.

    Each toolkit implements a factory that maps between chunk objects and
    ChunkParamWidgetBase subclasses. The mapping can be extended with custom widgets. See
    chunk_qt.ChunkQTWidgetFactory in plugins/ for an implementation.

    Attributes:
        widget_map (dict[ChunkFlags.ParamType, type[ChunkParamWidgetBase]]): This attribute
            holds the mapping between chunk object types and their corresponding widget.
    """

    widget_map: dict[ChunkFlags.ParamType, type[ChunkParamWidgetBase]]

    def get_map(self) -> dict[ChunkFlags.ParamType, type[ChunkParamWidgetBase]]:
        """Return the widget_map of the factory.

        Returns:
            dict[ChunkFlags.ParamType, type[ChunkParamWidgetBase]]: The widget_map
        """
        return self.widget_map

    def add_mapping(self, key: ChunkFlags.ParamType, widget_type: type[ChunkParamWidgetBase]):
        """Add a mapping from key to widget_type.

        Args:
            key (ChunkFlags.ParamType): The ParamType to map from
            widget_type (type[ChunkParamWidgetBase]): The widget class to map to
        """
        self.widget_map[key] = widget_type

    def has_mapping(self, key: ChunkFlags.ParamType) -> bool:
        """Check if a mapping for key exists.

        Args:
            key (ChunkFlags.ParamType): The key to check for

        Returns:
            bool: True if a mapping from key exists, else False
        """
        return key in self.get_map()

    @abstractmethod
    def create_widget(self, param: ChunkParamBase) -> ChunkParamWidgetBase | None:
        """Create a new widget for a param. Implemented by a subclass.

        Args:
            param (ChunkParamBase): The param we want to represent in the UI

        Raises:
            NotImplementedError: A subclass does not implement it.

        Returns:
            Any: The UI library widget, e.g. a QWidget for Qt
        """
        raise NotImplementedError


@dataclass(frozen=True)
class FileDialogSpec:
    """What a FileBrowser param is asking a file dialog for.

    Every frontend needs the same four answers and none of them are toolkit-specific, so
    they are computed once here rather than three times differently. Qt honoured all of
    this already; the Flet and ImGui frontends rendered a FileBrowser as a bare text field
    with no browse button at all, which is what this exists to fix.

    Attributes:
        patterns (tuple[str, ...]): Glob patterns to filter on, e.g. ("*.usda", "*.usdc").
            ("*",) when the param names no extensions.
        directory (bool): Pick a folder rather than a file.
        must_exist (bool): The chosen path has to exist already. False means the param is
            a destination -- an output file that has not been written yet -- which is what
            an unset FILE_EXISTING flag means.
        title (str): A dialog title, derived from the param's label.
    """

    patterns: tuple[str, ...]
    directory: bool
    must_exist: bool
    title: str


def file_dialog_spec(param) -> FileDialogSpec:
    """Read a FileBrowser param's own settings into a FileDialogSpec.

    `pattern` defaults to "*", and each extension is appended to it -- so pattern "*" with
    extensions [".usda", ".usdc"] means ("*.usda", "*.usdc"). That is the shape Qt builds;
    it is defined here so every frontend builds the same one.

    Args:
        param: The FileBrowser param.

    Returns:
        The dialog spec.
    """
    pattern = getattr(param, "pattern", "") or "*"
    extensions = list(getattr(param, "extensions", None) or [])
    patterns = tuple(pattern + ext for ext in extensions) if extensions else (pattern,)
    label = ""
    try:
        label = param.get_label() or param.name
    except AttributeError:
        label = getattr(param, "name", "File")
    directory = bool(param.has_flag(ChunkFlags.ParamFlags.DIRECTORY))
    # FILE_EXISTING unset (the default) means the file need not exist yet -- a destination
    # like Export ChunkGroup's Output File.
    must_exist = bool(param.has_flag(ChunkFlags.ParamFlags.FILE_EXISTING))
    return FileDialogSpec(patterns, directory, must_exist, label)
