"""
Defines all base classes which the chunks and associated classes should derive from. These classes should not
be instatiated, instead use the implementations from [usd_chunks.chunk](chunk.md).

We use the attrs library behind the scenes to get dataclass-like behaviour for all the Chunk classes in order to
easily create and manage attributes. The attributes and function definitions from attrs are inherited through the mro
so we don't need to define __init__ for each and every subclass, we can instead just define some fields and trust attrs
with building the right functions for us. Instead of using @attrs.define, we use AttributesMetaclass as metaclass for
all chunk objects. AttributesMetaclass wraps the classes around attrs.define with the correct settings, removing the need
of further interfering with attrs. AttributesMetaclass also add some custom validation and conversion behaviour.

All chunk objects inherits from ChunkCommon, which declare some fields that all chunk objects should have; name, f.e.

Every modifyable field are themselves instances of AttributeSender. As AttributeSender also inherits from ChunkNoticePropagator,
we can propagate the attribute changes up the hierarchy in order to send notices and similar when any attribute is modified.

"""

import copy
import inspect
import logging
import os
import re
import uuid
import weakref
from abc import abstractmethod
from collections import ChainMap
from enum import IntEnum, IntFlag, Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Generic,
    Iterable,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypedDict,
    TypeVar,
    Union,
    Sequence,
    get_origin,
    TypeGuard,
    overload,
)

from pathlib import Path
import attrs
from attr import _make as attrMake
from pxr import Sdf, Usd, UsdGeom, Gf
from typing_extensions import Self, dataclass_transform, Required

from usd_chunks.chunk_notice import Predicate
from usd_chunks import ATTRS_VERSION, chunk_notice, chunk_registry, chunk_serial, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags


_ATTRS_SUPPORT_CONVERTER = ATTRS_VERSION >= (24, 1)  # type: ignore
_ATTRS_SUPPORT_INSPECT = ATTRS_VERSION >= (25, 4)  # type: ignore

logger = logging.getLogger(__name__)

ValueT = TypeVar("ValueT")
ParamT = TypeVar("ParamT", bound="ChunkParamBase")
GroupT = TypeVar("GroupT", bound="ChunkContainerBase")
ChunkT = TypeVar("ChunkT", bound="ChunkCommon")

################################
# BASE CLASSES FOR UI ELEMENTS #
################################


class SyntaxHighlightStyle(TypedDict, total=False):
    color: Required[str]
    italic: bool
    bold: bool


class SyntaxHighlightMapping:
    def __init__(self):
        self._mappings: Dict[str, SyntaxHighlightStyle] = {}

    def add_mapping(self, pattern, style: SyntaxHighlightStyle):
        self._mappings[pattern] = style

    def get_mappings(self):
        return self._mappings.items()

    def match_text(self, text: str) -> Generator[Tuple[int, int, SyntaxHighlightStyle], None, None]:
        for pattern, fmt in self._mappings.items():
            for m in re.finditer(pattern, text):
                start, end = m.span()
                yield start, end, fmt


class PySyntaxHighlightMapping(SyntaxHighlightMapping):
    def __init__(self) -> None:
        super().__init__()

        pattern = r"^\s*class\s+\w+\(.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4c6ea3"))

        pattern = r"^\s*def\s+\w+\s*\(.*\)\s*:\s*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#31a56b"))

        pattern = r"^\s*#.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#3f883f"))


class UsdSyntaxHighlightMapping(SyntaxHighlightMapping):
    def __init__(self) -> None:
        super().__init__()

        pattern = r"^\s*class\s+\w+\(.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4c6ea3"))

        pattern = r"^\s*(def|over)"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4a80ec"))

        pattern = r"^\s*#.*$"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#3f883f"))

        pattern = r"(?<=def )(\w+)"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#51c765"))

        pattern = r"(\"|\')\w+(\"|\')"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#e17a3f"))

        pattern = r"(double|float|int|uniform|asset|token|color)\w?.(\[\])?"
        self.add_mapping(pattern, SyntaxHighlightStyle(color="#4aabec"))


class ChunkParamWidgetBase(Generic[ParamT], chunk_notice.ChunkNoticePropagator):
    """Base class for a chunk param widget. Any ui element associated to a chunk or parameter should subclass from this.
    This class acts as an bridge between this toolkit and the UI library.

    Creating a custom widget should subclass
    the relevant UI widget as well as this class, such as:
    ```
        class myWidget(QtWidgets.QPushButton, ChunkParamWidget)
    ```
    The subclass should override the relevant methods from this class
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
        if param := self._param():
            return param
        raise ReferenceError("reference to param is dead! :'(")

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.WIDGET_CHANGED

    def handle_param_changed(self, notice: chunk_notice.AttributeChangeNotice, sender: "ChunkParamBase"):
        """
        callback for handling changes to the parameter. Currently implements changes to
        value, name, label and icon attributes

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
        """
        Called when initializing the widget. Sets the value and label on this object to represent the values of the chunk object
        """

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
        """
        Need to be implemented in a subcclass and return the value of the widget.

        Returns:
            Any: The value of the widget
        """
        return None

    @abstractmethod
    def set_value(self, value: Any):
        """
        Need to be implemented in a subclass. This should only set the value of the widget,
        not modify the chunk object as that would result in an infinite loop

        Args:
            value (Any): The new value of the widget
        """
        pass

    @abstractmethod
    def set_label(self, text: str):
        """
        Need to be implemented in a subclass. This should only set the label of the widget,
        not modify the chunk object as that would result in an infinite loop

        Args:
            text (str): The new label of the widget
        """
        pass

    @abstractmethod
    def set_icon(self, icon: str):
        """
        Need to be implemented in a subclass. This should only set the icon of the widget,
        not modify the chunk object as that would result in an infinite loop

        Args:
            icon (str): A path to an image file which should be used as icon for this widget
        """
        pass

    @abstractmethod
    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        """
        Implement the different LayoutFlags here. This can be ignored if the widget does not support the layout flags

        Args:
            layout_flags (ChunkFlags.LayoutFlags): The LayoutFlags of the parameter which the widget should respect
        """
        pass

    @abstractmethod
    def set_hidden(self, val: bool):
        """
        Set the ui widget hidden based on the value of val

        Args:
            val (bool): Whether to hide or show the widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_enabled(self, val: bool):
        """
        Set the ui widget enabled or disabled based on value of val

        Args:
            val (bool): Whether to enable or disable the widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def update_from_state(self):
        """
        Called when state of the group has changed
        """
        self.set_hidden(self.param.has_state(ChunkFlags.State.HIDDEN))
        is_output = self.param.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        self.set_enabled(not is_output and not self.param.has_state(ChunkFlags.State.DISABLED))

    def value_changed(self, value: Optional[Any] = None):
        """
        Called whenever the value of the widget changed, and forwards the new value to self.set_value.
        This will also update param.value

        Args:
            value (Optional[Any], optional): The new value set on the widget. Defaults to None.
        """

        if self.param is not None:
            if self.param._solving_attributes:
                return

            if value is None:
                value = self.get_value()
            self.param.value = value


class ChunkWidgetFactoryBase(Protocol):
    """
    A ChunkWidgetFactory exists to handle the creation of widgets from chunk objects. This is
    implemented for any specific UI library and should map between chunk objects and ChunkParamWidget's.
    This can also be used to create a custom mapping for custom widget, extending the base library.
    See chunk_qt.ChunkQTWidgetFactory in plugins/ for an implementation.

    Attributes:
        widget_map (dict[ChunkFlags.ParamType, type[ChunkParamWidget]]): This attribute holds the mapping between chunk
            object types and its corresponding widget.
    """

    widget_map: Dict[ChunkFlags.ParamType, type[ChunkParamWidgetBase]]

    def get_map(self) -> Dict[ChunkFlags.ParamType, type[ChunkParamWidgetBase]]:
        """
        Return the widget_map of the factory

        Returns:
            Dict[ChunkFlags.ParamType, type[ChunkParamWidget]]: The widget_map
        """
        return self.widget_map

    def add_mapping(self, key: ChunkFlags.ParamType, widget_type: type[ChunkParamWidgetBase]):
        """
        Add a mapping from key to widget_type

        Args:
            key (ChunkFlags.ParamType): The ParamType to map from
            widget_type (type[ChunkParamWidget]): The widget class to map to
        """
        self.widget_map[key] = widget_type

    def has_mapping(self, key: ChunkFlags.ParamType) -> bool:
        """
        Check if a mapping for key exists

        Args:
            key (ChunkFlags.ParamType): The key to check for

        Returns:
            bool: True if a mapping from key exists, else False
        """
        return key in self.get_map()

    @abstractmethod
    def create_widget(self, param: "ChunkParamBase") -> ChunkParamWidgetBase | None:
        """
        A subclass should implement this and create a new widget from the param

        Args:
            param (ChunkParamBase): The param we want to represent in the UI

        Raises:
            NotImplementedError: Base implementation throw, as to make sure this is properly implemented in a subclass

        Returns:
            Any: The UI library widget. I.e QtWidget for Qt
        """
        raise NotImplementedError


class ChunkContainerWidgetBase(Generic[GroupT]):
    """
    Chunk is a base class for creating UI representation of chunk containers.
    """

    def __init__(self, group: GroupT, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Hold a weakref to the group. If all other references to the group is killed, then
        # remove it from the UI as well, as the ui should not take ownership of the group
        self._group = weakref.ref(group, self._group_deleted)

        self._callbacks: List[chunk_notice.NoticeConnection] = []

        # Add a parameter to this widget whenever a param was added to the group
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._param_added_callback, chunk_notice.ChunkNoticeAlias.PARAM_ADDED, self.group
            )
        )

        # Remove a parameter when the parameter was removed from the group
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._param_removed_callback, chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, self.group
            )
        )

        # Calls _delete_self() whenever the chunk sends the CHUNK_DELETED
        self._callbacks.append(
            chunk_notice.NoticeConnection(self._delete_self, chunk_notice.ChunkNoticeAlias.CHUNK_DELETED, self.group)
        )

        # Listen to state changes on the group
        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._state_changed,
                chunk_notice.ChunkNoticeAlias.STATE_CHANGED,
                self.group.get_attribute_sender("state"),
            )
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._name_changed,
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                self.group.get_attribute_sender("name"),
            )
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._name_changed,
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                self.group.get_attribute_sender("label"),
            )
        )

        self._callbacks.append(
            chunk_notice.NoticeConnection(
                self._layout_changed,
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                self.group.get_attribute_sender("layout"),
            )
        )

        # Associate ChunkParamGroup objects with group widgets
        self.group_widgets: weakref.WeakValueDictionary[ChunkHash, ChunkContainerWidgetBase] = (
            weakref.WeakValueDictionary()
        )

        # Container for all widgets in this chunk. Dict will map between ChunkParam -> widget
        # for each widget created from the same ChunkParam instance
        self.param_widgets: weakref.WeakValueDictionary[ChunkHash, ChunkParamWidgetBase] = weakref.WeakValueDictionary()

        # Initialize everything
        self.factory = self.get_widget_factory()
        self.build_collapsable()
        self._build_param_widgets(self.group)
        self.update_from_layout()
        self.update_from_flags()
        self.update_from_state()

    @property
    def group(self) -> GroupT:
        if group := self._group():
            return group
        raise ReferenceError("reference to group is dead! :'(")

    @abstractmethod
    def get_widget_factory(self) -> ChunkWidgetFactoryBase:
        """
        Override this function to return a specific ui plugin widget factory

        Raises:
            AttributeError: _description_

        Returns:
            ChunkWidgetFactoryBase: The factory that can create the valued parameters from ChunkParamBase objects
        """
        factory = self.group.widget_factory
        if factory is None:
            raise AttributeError(
                "No widget factory supplied to ChunkContainerWidgetBase. Implement get_widget_factory()"
            )
        return factory

    @abstractmethod
    def delete_self(self):
        """
        The entire group should be deleted

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def remove_widget(self, widget: Any):
        """
        Called when a child param widget should be removed from this group widget

        Args:
            widget (Any): The widget to remove from the group widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    def is_collapsed(self) -> bool:
        """
        Returns whether the group widget should be collapsed

        Returns:
            bool: True if the group should be collapsed
        """
        val = self.group.has_state(ChunkFlags.State.COLLAPSED)
        parent = self.group.get_owner()
        if parent and isinstance(parent, ChunkContainerBase):
            val |= parent.has_state(ChunkFlags.State.COLLAPSED)
        return val

    @abstractmethod
    def build_collapsable(self):
        """
        Build the group widget.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def update_from_flags(self):
        """
        Update the widget from the flags of self.group

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_hidden(self, val: bool):
        """
        Set the ui widget hidden based on the value of val

        Args:
            val (bool): Whether to hide or show the widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_enabled(self, val: bool):
        """
        Set the ui widget enabled or disabled based on value of val

        Args:
            val (bool): Whether to enable or disable the widget

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_selected(self, val: bool):
        """
        Called when the SELECTED state is changed. Should display the selection on the widget. By default,
        this does nothing

        Args:
            val (bool): Whether to enable or disable the widget
        """
        pass

    @abstractmethod
    def update_from_state(self):
        """
        Called when state of the group has changed
        """
        self.set_hidden(self.group.has_state(ChunkFlags.State.HIDDEN))
        # NB: OUTPUT is a ParamFlags value and has no meaning for a group. Checking it
        # here against the group's GroupFlags mis-fires because ParamFlags.OUTPUT and
        # GroupFlags.COLLAPSABLE share the same bit (2), so every collapsible group
        # read as "output" and disabled all its children. A group is only disabled by
        # its own DISABLED state.
        self.set_enabled(not self.group.has_state(ChunkFlags.State.DISABLED))
        self.set_selected(self.group.has_state(ChunkFlags.State.SELECTED))

    @abstractmethod
    def update_from_layout(self):
        """
        Called when layout of the group has changed
        """
        pass

    @abstractmethod
    def set_expanded(self, checked: bool):
        """
        Set if the widget should be expanded and show all child widgets. If false, this should
        hide all the child widgets

        Args:
            checked (bool): Whether the group widget should be expanded or not

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def update_param_label(self, notice, sender: "AttributeSender"):
        """
        When a param name, label or flag is changed, this will correct the label in the FormLayout.
        If LABELED was unset, then remove the label and set the widget role to SpanningRole
        If LABELED was set, add the label
        otherwise, just update the label text if name or label changed.

        A ui implementation plugin should implement this function to get labeled widgets

        Args:
            notice (Tf.Notice): The notice that was sent
            sender (chunk_core.AttributeSender): The attribute that changed
        """
        pass

    @abstractmethod
    def update_label(self, text: str):
        """
        Update the label of this container

        Args:
            text (str): The new label to display
        """
        pass

    @abstractmethod
    def supports_widget(self, widget: Any) -> bool:
        """
        For ui implementation plugins to make sure the supplied widget is supported. F.e:
        ``` py
        return isinstance(widget, QtWidgets.QWidget)
        ```

        Args:
            widget (Any): The widget to check

        Raises:
            NotImplementedError: _description_

        Returns:
            bool: True if the widget is supported by the current ui plugin
        """
        raise NotImplementedError

    @abstractmethod
    def add_widget(self, widget: "ChunkContainerWidgetBase | ChunkParamWidgetBase", label: Optional[str] = None):
        """
        Called after a param has been added to the group. This should add the widget to the relevant layouts

        Args:
            widget (ChunkContainerWidgetBase | ChunkParamWidgetBase): The widget to add
            label (str, optional): An optional label of the widget. Defaults to None.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def add_list_widget(
        self, widget: "ChunkContainerWidgetBase | ChunkParamWidgetBase", label: Optional[str] = None, list_index=0
    ):
        """
        Called after a param has been added to the group. This should add the widget to the relevant layouts

        Args:
            widget (ChunkContainerWidgetBase | ChunkParamWidgetBase): The widget to add
            label (str, optional): An optional label of the widget. Defaults to None.

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError

    def _name_changed(self, notice, sender):
        self.update_label(sender.value)

    def _layout_changed(self, notice, sender):
        self.update_from_layout()

    def _disconnect_callbacks(self):
        """Revoke all notice subscriptions held by this widget.

        Must be called when the widget is torn down. The host (Qt) may destroy the
        underlying C++ object while the Python wrapper — and therefore its notice
        subscriptions — stays alive, so without this a later notice would invoke a
        method on a deleted object (e.g. ``setHidden`` on a dead ChunkWidget).
        """
        for conn in self._callbacks:
            try:
                conn.disconnect()
            except Exception:
                pass
        self._callbacks.clear()

    def _group_deleted(self, ref):
        self._disconnect_callbacks()
        self.delete_self()

    def _delete_self(self, notice, sender):
        if sender is not self.group:
            return

        self.group_widgets.clear()
        self.param_widgets.clear()

        self._disconnect_callbacks()
        self.delete_self()

    def _param_added_callback(self, notice, sender: GroupT):
        h = notice.data
        if not h:
            return
        param = None

        if isinstance(self.group, ChunkListGroupBase):
            param = self.group.get_my_params()[-1]
        else:
            param = self.group.get_param(h)

        if not param:
            return

        self._add_param(param)

    def _param_removed_callback(self, notice, sender: GroupT):
        h = notice.data
        if not h:
            return

        if isinstance(h, ChunkParamBase):
            if wid := self.param_widgets.get(h):
                self.remove_widget(wid)

        elif isinstance(h, ChunkContainerBase):
            if wid := self.group_widgets.get(h):
                self.remove_widget(wid)

    def _state_changed(self, notice, sender):
        if notice.data.get_owner() == self.group:
            try:
                self.update_from_state()
            except RuntimeError:
                # Host C++ object was already destroyed but this widget's subscription
                # outlived it (stale widget from a panel rebuild). Self-heal by revoking.
                self._disconnect_callbacks()

    def _add_param_widget(self, param: "ChunkParamBase", group: Optional["ChunkContainerBase"] = None, list_index=0):
        is_output = param.has_flag(ChunkFlags.ParamFlags.OUTPUT)
        t = param.evaluate_type()
        if t == ChunkFlags.ParamType.NONE:
            return

        widget: ChunkParamWidgetBase | None
        # Custom widget is created and managed externally
        if t == ChunkFlags.ParamType.CUSTOM and param.custom_widget and self.supports_widget(param.custom_widget):
            # If the widget already has a parent (displayed in another panel),
            # create a fresh instance so both panels get their own widget.
            existing = param.custom_widget
            if hasattr(existing, 'parent') and callable(existing.parent) and existing.parent() is not None:
                widget = type(existing)(param)
            else:
                widget = existing
        else:
            if not self.factory.has_mapping(t):
                logger.warning("Missing widget mapping for param %s", param.name)
                return

            # Create a widget from the factory based on the param.get_type()
            widget = self.factory.create_widget(param)

        if not widget:
            return

        if is_output:
            widget.set_enabled(False)

        self._callbacks.extend(
            [
                chunk_notice.NoticeConnection(
                    self.update_param_label,
                    chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                    param.get_attribute_sender("name"),
                ),
                chunk_notice.NoticeConnection(
                    self.update_param_label,
                    chunk_notice.ChunkNoticeAlias.GROUP_CHANGED,
                    param.get_attribute_sender("name"),
                ),
            ]
        )

        if group is not None:
            layout = self._add_group_layout(group)
        else:
            layout = self

        self.param_widgets[param.get_hash()] = widget

        if param.has_flag(ChunkFlags.ParamFlags.LABELED):
            layout.add_widget(widget, param.get_label())
        else:
            layout.add_widget(widget)

        return widget

    def _add_group_layout(self, group: "ChunkContainerBase") -> "ChunkContainerWidgetBase":
        container = self.group_widgets.get(group, None)
        if not container:
            container = self.__class__.__new__(self.__class__)
            container.__init__(group, parent=self)  # type: ignore
            self.add_widget(container)
            self.group_widgets[group.get_hash()] = container

        return container

    def _build_param_widgets(self, group: "ChunkContainerBase"):
        for param in group.get_my_params():
            self._add_param(param)

    def _add_param(self, param: "ChunkCommon"):

        if isinstance(param, ChunkContainerBase):
            self._add_group_layout(param)
        if isinstance(param, ChunkParamBase):
            self._add_param_widget(param)


#####################
# FIELD TRANSFORMER #
#####################


def get_attr_index(cls, data):
    try:
        fields_dict = attrs.fields_dict(cls)
    except Exception:
        return None

    if data not in fields_dict:
        return None

    fields = attrs.fields(cls)
    idx = [i for i in range(len(fields)) if data == fields[i].name]
    if idx:
        idx = idx[0]
    else:
        idx = None

    for base in cls.__bases__:
        i = get_attr_index(base, data)
        if i is not None:
            idx = i
    return idx


def add_validate_and_convert(cls: type["AttributesBase"], fields: List[attrs.Attribute]):
    """Used as attrs.field_transformer for chunk attributes in order to sort the __init__ arguments to
    match pylance
    """

    sort_keys = {}
    results = []
    for i, f in enumerate(fields):
        # Find the first occurance of the attribute in the inheritance hierarchy, and use the index of the
        # first declaration. This is to match the constructor arguments of pylance
        index = get_attr_index(cls, f.name)
        if index is not None:
            sort_keys[f.name] = index
        else:
            sort_keys[f.name] = i

        do_repr = f.init and f.repr

        results.append(f.evolve(repr=do_repr))

    return sorted(results, key=lambda x: sort_keys[x.name])


@dataclass_transform(field_specifiers=(attrs.Attribute, attrs.field), eq_default=False)
class AttributesMetaclass(type):
    """Metaclass used to wrap Chunk attributes classes around attrs. Using it through metaclass instead of decorating all
    classes with @attrs.define order to have proper default behaviour of all chunk classes

    This metaclass also injects some functions in to the defaul attrs __setattr__ in order to use out custom validation and conversion functions:
    calls value_changed() on subclasses after __setattr__, and adds
    add _validate() and _convert() as default validator and conversion functions to all attrs.fields where applicable
    """

    def __new__(cls, name, bases, attributes):
        # Construct new class and wrap it around attrs.define

        res = super().__new__(cls, name, bases, attributes)

        # we use a custom __setattr__ on all subclasses for a couple of reasons:
        #   - We need the 'self' argument to the converter methods, which the old-style (pre attrs 24.1) converters does not support.
        #   - the on_setattr method is not called when first setting the attributes, meaning if we were to hook this to
        #     on_setattr, the _safe_convert() function is not called when first initializing the values
        #   - value_changed() should be called *after* __setattr__, which is seemingly not possible with on_setattr.

        setattr_override = attrs.setters.NO_OP

        def _wrapped_setattr(inst: "AttributesBase", name, value):
            nonlocal res

            if name not in attributes:
                return super(res, inst).__setattr__(name, value)

            resval = value

            # First run converters and validators
            attrib = None
            fields = attrs.fields(res)  # type: ignore
            try:
                attrib = getattr(fields, name)
            except AttributeError:
                return super(res, inst).__setattr__(name, resval)

            # Run conversion
            if attrib and attrib.metadata.get("convert", True):
                resval = inst._safe_convert(attrib, value)

            if attrib and attrib.metadata.get("validate", True):
                inst.validate_attr(attrib, resval)

            has_attr = hasattr(inst, name)
            old_value = None
            if has_attr:
                old_value = getattr(inst, name)

            super(res, inst).__setattr__(name, resval)

            if has_attr:
                try:
                    inst.value_changed(name, old_value, resval)
                except AttributeError as e:
                    logger.exception(e)

        res.__setattr__ = _wrapped_setattr  # type: ignore

        res = attrs.define(
            res,
            slots=False,
            eq=False,
            repr=False,
            field_transformer=add_validate_and_convert,
            on_setattr=setattr_override,
        )
        return res


class AttributeSender(Generic[ValueT], chunk_notice.ChunkNoticePropagator):
    """An AttributeSender gets sent when an attribute changes on a AttributesBase instance, as attrs.Attribute
    is not able to send through the Tf.Notice system. Instances of this class can be added to a propagator hierarchy
    as it subclass from chunk_notice.ChunkNoticePropagator. This enables us to automatically send the
    ATTRIBUTE_CHANGED notice whenever the value is modified.

    A valued parameter stores the value of the attributes itself, this class is a handle to thos attributes but with the
    ability to use with the Tf.Notice system. See value_changed function of AttributesBase for more info

    Args:
        owner (AttributesBase): The owning objcet of the attribute
        name (str): The name of the attribute
        alias (chunk_notice.ChunkNoticeAlias): The ChunkNoticeAlias that is sent when the attribute changed

    Attributes:
        name (str): The name of the attribute
        value (Any): A property looking up the value of the owner
        old_value (Any): A copy of the old value. This may be not-None after the value has been changed

    """

    __slots__ = ("name", "alias", "old_value")

    def __init__(
        self,
        owner: chunk_notice.ChunkNoticePropagator,
        name: str,
        alias: chunk_notice.ChunkNoticeAlias = chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
    ):
        super().__init__()
        self.name: str = name
        self.alias: chunk_notice.ChunkNoticeAlias = alias
        self.old_value: Optional[ValueT] = None

        self.set_owner(owner)

    def get_notice_data(self) -> Self:
        """
        The notice data for AttributeSender's is simply self. A reciever of a notice may use this
        to get any data available on the AttributeSender. This is possible if the original sender
        of the notice is an AttributeSender, as the notice.data is retrieved from get_notice_data()

        ``` py
        def callback(owner, notice, sender):
            val = notice.data.value
            old_val = notice.data.old_value
        ```

        Returns:
            Tuple[Any, Any]: A tuple of new and old values
        """
        return self

    @property
    def value(self) -> ValueT | None:
        if not self.get_owner():
            return None
        return getattr(self.get_owner(), self.name)

    @value.setter
    def value(self, val: ValueT):
        if not self.get_owner():
            return
        if not hasattr(self.get_owner(), self.name):
            logger.error(f"Failed to set attribute {self.name} from AttributeSender")
            return

        setattr(self.get_owner(), self.name, val)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return self.alias

    def is_presentation_change(self) -> bool:
        """
        A change to the state attribute is presentation only unless it moved DISABLED.

        DISABLED decides whether the chunk cooks at all, so it is a real input to everything
        downstream. HIDDEN, COLLAPSED, SELECTED and STALE are hints to the UI and nothing
        else -- hiding in particular is a *consequence* of a chunk reconfiguring (math_ops
        hides b/c because the operation changed), so treating it as a cause re-cooks the
        chunk for something its own cook just did.

        Returns:
            bool: True if only presentation bits moved
        """
        if self.alias is not chunk_notice.ChunkNoticeAlias.STATE_CHANGED:
            return False

        old = ChunkFlags.State(self.old_value or ChunkFlags.State.NONE)
        new = ChunkFlags.State(self.value or ChunkFlags.State.NONE)
        return ChunkFlags.State.DISABLED not in (old ^ new)

    def get_usd_prop(self) -> Usd.Property | None:
        """
        Retrieve the UsdProperty that this attribute represents

        Returns:
            Usd.Property | None: The UsdProperty of the attribute if it is bound, else None
        """
        if owner := self.get_owner():
            if not isinstance(owner, ChunkUsdAttributeSupport):
                return
            return owner.get_usd_prop()

    def _copy_connection(
        self,
        conn: chunk_notice.NoticeConnection,
        src: chunk_notice.ChunkNoticePropagator,
    ):
        # Add connections to the copy by creating new NoticeConnection objects
        # Rewire the connection to the copy if conn.sender is the source of the copy (self)
        if meth := conn.get_method():
            sender = self if conn.sender == src else conn.sender
            new_conn = chunk_notice.NoticeConnection(
                meth, conn.alias, sender, conn.globally, self if conn.owner is not None else None
            )
            self._callbacks.add(new_conn)


class CopyMode(IntEnum):
    """
    This enum is used for deciding how the parameters should be created when copied from another
    parameter. The defsault is to create a copy, but if the parameters should be references (share recources),
    SHARE may be better
    """

    COPY = 0  # copy.copy() attribute. This is default if nothing else is specified
    DEEPCOPY = 1  # copy.copy() attribute. This is default if nothing else is specified
    NO_COPY = 2  # Does not copy attribute, sets default instead
    SHARE = 3  # Share the the attribute with the source


class SendMode(IntEnum):
    """
    Setting for whether an attribute should send a notice when changed

    """

    NO_SEND = 0
    SEND = 1


class AttributeMetadata(TypedDict, total=False):
    """Use in field(metadata=AttributeMetadata(...)).


    Attributes:
    copy_mode (CopyMode): Decides how the attribute should be treated when copying. If not set, use a COPY
    send_mode (SendMode): Decides if the attribute should send a notice when modified. If not set, use SEND
    sender_alias (chunk_notice.ChunkNoticeAlias): What ChunkNoticeAlias to use when sending. If not set, will use ATTRIBUTE_CHANGED
    validate (bool): Whether to validate this attribute's value when changed
    convert (bool): whether to convert this attribute's value to a valid value when changed
    documentation (str): The documentation for this parameter, which will be added to the parameter's description through MkDocs
    """

    copy_mode: CopyMode
    send_mode: SendMode
    sender_alias: chunk_notice.ChunkNoticeAlias
    validate: bool
    convert: bool
    documentation: str
    serialize: bool


class AttributesBase(chunk_notice.ChunkNoticePropagator, metaclass=AttributesMetaclass):
    """This class is used as base for Chunk dataclasses. Sends modification notices and validates/converts attribute values

    Any modifications to an attribute with `metadata["send_mode"]==SendMode.SEND`, will send a notice of type metadata["sender_alias"] when modified.
    This is achieved by using the AttributeSender class, which holds a handle to the owner (AttributesBase), name of the attribute it represents,
    and the alias for which notice to send. It also holds the old_value (the value of the attribute prior to the change) when available.

    Instances of this class will automatically set self as owner on all AttributeSenders it manages.

    AttributesMetaclass is used as metaclass to automatically hook into the attrs framework. Any subclass is thus treated as it was
    created with attrs.define. All fields will call _validate() and _convert() as validation and conversion functions. Any changes made to an
    attribute (through __setattr__) will call value_changed() after the original __setattr__ finished.

    """

    _failed_converters: Dict[str, Any] = attrs.field(
        factory=dict,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )

    _attr_senders: Dict[str, AttributeSender] = attrs.field(
        factory=dict,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )

    _solving_attributes: bool = attrs.field(
        default=False,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )

    _attrib_functions: Dict[str, Callable[[Self, AttributeSender], Any]] = attrs.field(
        factory=dict,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False, serialize=True
        ),
    )
    _logger: Optional[logging.Logger] = attrs.field(
        init=False,
        default=None,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    @classmethod
    def print_init_signature(cls):
        """
        A debug function for printing the generated __init__ function signature
        """
        signature = inspect.signature(cls.__init__)
        msg = "__init__("
        for param in signature.parameters.keys():
            msg += "\n    " + param + ","
        msg += "\n)"
        print(f"{cls.__name__} __init__ signature: \n", msg)

    def __attrs_post_init__(self):
        # initialise ChunkNoticePropagator
        super().__init__()

        # Loop over all fields and add an AttributeSender for the attribute, which will be sent in value_changed. Doing this instead of
        # get_notice_alias_for_attr in order to have a seperate sender for each attribute
        attr = attrs.fields_dict(type(self))
        for name, attr in attr.items():
            if attr.metadata.get("send_mode", SendMode.SEND) is SendMode.NO_SEND:
                continue

            alias = attr.metadata.get("sender_alias", chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
            if not alias:
                # Includes ChunkNoticeAlias.NONE
                continue

            sender = AttributeSender(self, name, alias)
            self._attr_senders[name] = sender

        self._resolve_failed_converters()

    def get_logger(self) -> logging.Logger:
        if not self._logger:
            self._logger = logging.getLogger(f"usd_chunks.{self.__class__.__name__}")
        return self._logger

    def clone(self) -> Self:
        """
        Create and return a copy of self. Most attributes are copied, but depending on the fields metadata copy_mode (CopyMode),
        the attribute may behave differently. NoticeConnections are not copied, so in the following example, only the first param ("slider1")
        will trigger the callback to myFunc:

        ``` py
        param = chunk.Slider("slider1")
        param.add_callback(myFunc)

        myChunk = chunk.Chunk("myCHunk")
        myChunk.add_params([param, param.clone()])
        ```

        Returns:
            Self: a copy of self
        """
        return copy.copy(self)

    def __copy__(self) -> Self:
        """Create a new instance of self.__class__ and set member variabled appropriately. A fields metadata["copy_mode"] sets how the field should transfer to the copy.
        If a non-attrs class exists in the hierarchy, its members will be ignored. The exception being ChunkNoticePropagator, which is initialized in AttributesBase.__attrs_post_init__().

        No __init__ is called on the copied class. Instead, the attrs pre and post init methods are called, if available
        __attrs_pre_init__ is called prior to copying the fields
        __attrs_post_init__ is called after copying the fields.
        Any instantiation of base classes should be done inside either of these functions. For example, ChunkNoticePropagaor is initialized inside of AttributesBase.__attrs_post_init__()

        Returns:
            Self: Return a copy of self, with updated uuid and rewired connections
        """
        copied = type(self).__new__(self.__class__)

        # Run pre-init if available
        pre_init = getattr(copied, "__attrs_pre_init__", None)
        if callable(pre_init):
            pre_init()

        # For each field on this class, set the value on the copy, based on the CopyMode metadata. If no metadata is set, will use copy.copy()
        attributes = attrs.fields_dict(type(self))
        data = {}
        for name, attr in attributes.items():
            copy_mode = attr.metadata.get("copy_mode", CopyMode.COPY)

            if copy_mode is CopyMode.COPY:
                # By default, shallow copy the attribute value
                data[name] = copy.copy(getattr(self, name))

            elif copy_mode is CopyMode.DEEPCOPY:
                # If specified, deep copy the value
                data[name] = copy.deepcopy(getattr(self, name))

            elif copy_mode is CopyMode.NO_COPY:
                # NO_COPY sets the value to the attrs.Attribute.default. If the field was created with factory=... or default=Factory(...),
                # call the factory method and use the result as the attribute value
                val = attr.default
                if isinstance(val, attrMake.Factory):
                    if val.takes_self:
                        val = val.factory(copied)
                    else:
                        val = val.factory()

                data[name] = val

            elif copy_mode is CopyMode.SHARE:
                # SHARE should not copy nor get from default. Simply pass the same reference. Should be used with caution
                data[name] = self.__dict__.get(name)

        copied.__dict__.update(data)  # type: ignore

        # Call the post-init if available. If not explicitly overwritten, this should initialize the base ChunkNoticePropagator
        post_init = getattr(copied, "__attrs_post_init__", None)
        if callable(post_init):
            post_init()

        if len(copied.__dict__) != len(self.__dict__):
            # Source may have extra non-attrs entries set dynamically after init (e.g. lazy-init
            # attributes). Copy them verbatim so the clone is complete. If the copy somehow ended
            # up with MORE entries than the source, that is a genuine bug and we raise.
            extra = {k: v for k, v in self.__dict__.items() if k not in copied.__dict__}
            if extra:
                copied.__dict__.update(extra)
            if len(copied.__dict__) > len(self.__dict__):
                raise copy.Error(
                    f"Failed to copy {type(self).__name__}, copy has more attributes than source"
                )

        copied.set_owner(self.get_owner())
        # Copy any connections to self over to the copy
        return copied

    @classmethod
    def inspect_class(cls) -> "attrs.ClassProps":  # type: ignore
        """Wrapper around attrs.inspect. Can be used to debug the class attributes

        Returns:
            attrs.ClassProps: ClassProps of this class
        """
        if not _ATTRS_SUPPORT_INSPECT:
            return None  # type: ignore
        return attrs.inspect(cls)  # type: ignore

    def get_attribute_sender(self, attribname: str) -> AttributeSender:
        """Get a sender for an attribute on this instance. The AttributeSender can be used to
        subscribe to specific attribute changes in callbacks, either by:
            attributeSender.add_callback(some_method)
        or:
            chunkParamBase.add_callback(some_method, sender=attributeSender)

        A gotcha with the first method is that AttributeSenders are not preserved in copies, so:
            c = chunk.Checkbox("someName")
            c.get_attribute_sender("value").add_callback(someFunc)
            myChunk.add_params([C, C.clone()])
        would mean that only the first instance of C will call someFunc when its value attribute changes

        Args:
            attribname (str): Name of the attribute

        Returns:
            AttributeSender: The AttributeSender mapped by attribname, if found

        Raises:
            AttributeError: If the attribute sender was not found
        """
        if sender := self._attr_senders.get(attribname):
            return sender
        raise AttributeError(f"Attribute sender not found {attribname}")

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        """
        Called after any attr __setattr__ was called.
        This function should not mutate self to avoid infitite recursion. The purpose is instead to only send Notices of changed attributes when applicable.
        If an attribute exists in self._attr_senders (added in __attrs_post_init__), the mapped AttributeSender is used as sender for the notice. Otherwise, use self as sender

        This method is explicitly injected to __setattr__ by AttributesMetaclass

        Args:
            attrname (str): The name of the attribute that was changed
            value (Any): The new value
        """

        if old_value == value or attrname.startswith("_"):
            # No change was made, we should not send the notice
            return

        if attrname in self._failed_converters:
            return

        if self.is_blocked() and attrname != "state":
            return

        if chunk_notice._loading_stage:
            return

        if sender := self._attr_senders.get(attrname):
            sender.old_value = old_value
            sender.execute()
        else:
            self.execute()

    def set_function(self, attrname: str, method: Callable[[Self, AttributeSender], Any]):
        """
        Set a function that will be called to evaluate the attribute with name attrname. The result of this function will be
        the new value of the attribute. For example, a parameters 'value' attribute may evaluate to the product of two inputs:

        ``` py
        c_param = chunk.Slider()
        c_param.add_dependency(a_param)
        c_param.add_dependency(b_param)
        c_param.set_function(
            "value",
            lambda p, attr: (
                p.get_dependency(0).value
                * p.get_dependency(1).value
            ),
        )
        ```
        The sliders value, in this case, will update any time a_param or b_baram updates.

        Args:
            attrname (str): The name of the attribute that should use the function when evaluating
            method (Callable[[Self, AttributeSender], Any]): The function to run when evaluating the attribute. The returned value from the function
                will be the new attribute's value
        """
        self._attrib_functions[attrname] = method

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> chunk_notice.ChunkNotice | None:
        if not self._solving_attributes:
            # Run any attribute functions before continuing. Since calling __setattr__ calls value_changed, we need to disable
            # the notices to not get recursion. Only the first call to __setattr__ should send notices.
            if notice and notice.alias in [
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                chunk_notice.ChunkNoticeAlias.VALUE_CHANGED,
                chunk_notice.ChunkNoticeAlias.INPUT_CHANGED,
            ]:
                for name, func in self._attrib_functions.items():
                    attr_sender = self._attr_senders.get(name)

                    if attr_sender and func:
                        self._solving_attributes = True
                        res = func(self, attr_sender)
                        setattr(self, name, res)
                self._solving_attributes = False

        return super().data_changed(notice)

    def _safe_convert(self, attr: attrs.Attribute, val: Any) -> Any:
        """
        Wraps self.convert_attr, but puts any failed conversion attributes in to self._failed_converters
        to try and resolve them after __init__. This is so a client does not need to worry about hasattr()
        in their custom convert_attr() functions.

        Args:
            attr (attrs.Attribute): The attribute to check conversion for
            val (Any): The value to convert

        Returns:
            Any: The converted value if success, otherwise the input val
        """
        res = val
        try:
            res = self.convert_attr(attr, val)
            if attr.name in self._failed_converters:
                self._failed_converters.pop(attr.name)
        except AttributeError:
            self._failed_converters[attr.name] = val

        return res

    def _resolve_failed_converters(self):
        """
        Naively try to resolve any failed converters in self._failed_converters, by simply trying to setting the
        attributes again. This resolves because attributes are added to _failed_converters when convert_attr raises
        AttributeError (some referenced dependency attribute did not exist when the original convert_attr was called)
        An example of this is the chunk.Menu class; the value should convert to a value in the menu_items list, but
        the 'value' and 'default_value' attributes are initialized prior to the menu_items
        """
        max_count = 1000
        count = 0
        while len(self._failed_converters) > 0:
            failed_attrs = tuple(self._failed_converters.keys())
            for attr in reversed(failed_attrs):
                setattr(self, attr, self._failed_converters[attr])

            count += 1
            if count > max_count:
                logger.error("Failed to resolve failed converters. Tests exceeded max count")
                self._failed_converters = {}
                return

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        """Attempts to convert a value to a desired value when modifying an attribute. Used like attrs.Attrs.Converter but for all attributes
        with attrs.field_transformer=add_validate_and_conver for the current instance. Makes it easier for clients to modify the conversion of attributes.

        In this example, inst.b = 2, should convert 2 to True

        class SomeClass:
            b: bool
        inst = SomeClass()
        inst.b = 2

        Args:
            attr (Attrs.Attribute): The attribute that was modified. Use attr.name to check the name of the modified attribute
            val (Any): The value which was set to the attribute. This should be the base of the conversion

        Returns:
            Any: A valid, possibly converted, attribute value.
        """
        return attrs.converters.default_if_none(attr.default)(val)

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        """Check if val is a supported value for attr prior to setting it. Raises ValueError if validation failed
        Args:
            val (Any): A value to check

        Returns:
            bool: If this value is supported by the Interface
        """
        # Attemp to cast to the type hint where applicable
        if attr.type:
            t = get_origin(attr.type)

            if t and (t is Generic or t is Union or t is Optional):
                return True

            if t and not isinstance(val, t):
                raise ValueError(f"Validation failed. Tried to set value of type: {type(val)} to parameter {attr.name}")
        return True


class ChunkHash(Protocol):
    """Protocol for hashable Chunk object"""

    def __hash__(self) -> int: ...
    def __eq__(self, value: object, /) -> bool: ...


class ProxyParam(chunk_notice.ChunkNoticePropagator):
    """
    A proxy parameter lies ontop of a concrete parameter. The proxy stores only a uniuqe value and unique children
    if the parameter is a group. This is useful for lists, where an original group dictates the content of each list
    entry, but the list entries can hold unique values. This means we can change any attribute except 'value' on the
    original, and the proxies will update.

    The proxy will have a unique hash value. To get the original parameter, use the param_ref property of the proxy.
    Worth noting is the ChunkCommon's _hashable attribute is not overridden, meaning ProxyParam._hashable will evaluate to the
    referenced HashVal

    Args:
        param (ChunkCommon): A chunk object which to reference
    """

    def __init__(self, param: "ChunkCommon", *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__hashable = HashVal()
        self.__param_ref = weakref.ref(param, self._param_died)

        if isinstance(param, ChunkParamBase):
            self._value = param.value
            self.__value_sender = AttributeSender(self, "value", param.get_attribute_sender("value").alias)
        else:
            self._value = None
            self.__value_sender = None

        # A ProxyParam mirrors its template's non-value attribute changes via this
        # dependency, but must not be pulled into the template's graph_id: set_graph_id
        # merges the *established* side into whichever DagGraphID it's called with, and a
        # freshly-constructed proxy's root would otherwise become the new root for the
        # whole tab it was just added to, orphaning that tab's graph_XXXX layer.
        self.get_dag_node()._part_of_graph = False
        self.add_dependency(param)

        self._children = NameSyncedContainer()

        if isinstance(param, ChunkContainerBase):
            self._children.set_owner(self)
            self._children = NameSyncedContainer.create_proxy(param.children, self._children)

            self.add_callback(self._ref_child_added, param, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_ADDED)
            self.add_callback(self._ref_child_removed, param, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_REMOVED)

    def serialize(self, glb):
        if (ref := super().serialize(glb)) and isinstance(ref, chunk_serial.SerializedClass):
            ref.kwargs["param"] = self._param_ref
            ref.attributes["value"] = self._value
        return ref

    def _ref_child_added(self, notice, sender):
        if isinstance(notice.data, ChunkParamBase):
            self._children.add(ProxyParam(notice.data))

    def _ref_child_removed(self, notice, sender):
        if isinstance(notice.data, ChunkCommon):
            p = None
            for param in self._children.get_my_params():
                if isinstance(param, ProxyParam) and param._param_ref == notice.data:
                    p = param
                    break
                if param == notice.data:
                    p = param
                    break

            if not p:
                raise ReferenceError("Could not find ref child")

            del self._children[p]

    def _param_died(self, ref):
        self.request_delete()

    @property
    def _param_ref(self):
        if param := self.__param_ref():
            return param
        raise ReferenceError("Reference to param is dead")

    def __eq__(self, value: object) -> bool:
        return self.__hashable == value

    def __hash__(self) -> int:
        return self.__hashable.__hash__()

    def _get_my_params(self):
        return self._children.get_my_params()

    def _get_params(self):
        return self._children.get_params()

    def _get_param(self, *args, **kwargs):
        return self._children.get_param(*args, **kwargs)

    def _iter_hierarchy(self, level=0):
        yield level, self
        for p in self._get_my_params():
            if isinstance(p, ChunkGroupBase):
                yield from p.iter_hierarchy(level + 1)
            else:
                yield level + 1, p

    def _get_attribute_sender(self, name):
        if name == "value":
            return self.__value_sender
        return self._param_ref.get_attribute_sender(name)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return self._param_ref.get_notice_alias()

    def request_delete(self):
        self._children.clear()
        self._children.set_owner(None)

        if self.__param_ref():
            # If the parameter is alive, we should send
            owner = self.get_owner()
            if owner and isinstance(owner, NameSyncedContainer):
                # Owner is a NameSyncedContainer, pop it from the contiainer. When successfully removed,
                # the container will send PARAM_REMOVED with it's owning ChunkContainerBase as sender.
                owner.send_removed(self)

        self.set_owner(None)

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        if not notice or not (res := super().data_changed(notice)):
            return None

        if notice.data == self._param_ref:
            for alias, prop in notice.data.get_dag_node().iter_changes():
                self.send_self(alias, prop)
        return res

    def send_self(
        self, alias: chunk_notice.ChunkNoticeAlias | None = None, data: Any = None
    ) -> chunk_notice.ChunkNotice | None:

        if not (res := super().send_self(alias, data)):
            return None

        if alias is None:
            alias = self.get_notice_alias()

        if alias is chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED:
            # Run all methods connected to the reference param, with self as
            # sender. This is possibly idiotic
            notice_class = chunk_notice.get_notice(alias)
            if issubclass(notice_class, chunk_notice.ChunkNotice):
                notice = notice_class(self.get_notice_data())
            else:
                notice = notice_class()

            for conn in self._param_ref.get_callbacks():
                conn.handle_callback(notice, self)
        return res

    def __getattribute__(self, name: str) -> Any:
        if name in ("value", "raw_value"):
            # Return the value stored on the proxy so widgets display the proxy's own value,
            # not the template's default.
            return super().__getattribute__("_value")
        if name == "iter_hierarchy":
            return super().__getattribute__("_iter_hierarchy")
        if name == "get_my_params":
            return super().__getattribute__("_get_my_params")
        if name == "get_params":
            return super().__getattribute__("_get_params")
        if name == "get_param":
            return super().__getattribute__("_get_param")
        if name == "get_attribute_sender":
            return super().__getattribute__("_get_attribute_sender")
        if name == "_param_died":
            return super().__getattribute__("_param_died")
        if name == "request_delete":
            return super().__getattribute__("request_delete")

        if name in ["_dag_node", "_callbacks", "_blocked"]:
            return super().__getattribute__(name)

        if name != "__class__" and hasattr(chunk_notice.ChunkNoticePropagator, name):
            return super().__getattribute__(name)

        if name in ["children", "_children"]:
            return super().__getattribute__("_children")

        try:
            if not name.endswith("_param_ref") and hasattr(self._param_ref, name):
                return getattr(self._param_ref, name)
        except (ReferenceError, AttributeError):
            pass

        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        # All attributes except 'owner' and 'value' are proxied, meaning we set the attributes
        # back on to the parameter we're proxying.

        if name != "__class__" and hasattr(chunk_notice.ChunkNoticePropagator, name):
            return super().__setattr__(name, value)

        if name in ["_dag_node", "_callbacks", "_blocked"]:
            return super().__setattr__(name, value)

        if name == "children":
            return super().__setattr__("_children", value)

        if name == "value" and self.__value_sender:
            # Instead of setting the value on the param, we intercept and store the value on the
            # proxy itself. This will propagate the notices through the correct channel instead
            # of through the original param ownership channel.
            self.__value_sender.old_value = self._value
            self._value = value
            # A list-group row (e.g. Sublayer's imports:sublayer_path) is a ProxyParam, so
            # _load_wire_inputs' bulk restore reaches this setattr directly -- not through
            # ChunkBase.value_changed, the only place chunk_notice._loading_stage is normally
            # checked (see its own guard). Without this, restoring a row's saved value fired a
            # real, immediate ATTRIBUTE_CHANGED here regardless of the bulk load in progress,
            # live-reported as the whole downstream chain re-executing repeatedly on every
            # reload of a saved scene (a list-group row was the one param path load_chunks'
            # suppression never actually covered). load_chunks' own later, correctly-ordered
            # "trigger the DAG solve for root nodes" pass still runs once everything is
            # restored -- and now reliably reaches this chunk's real content (see the layer
            # pre-registration added just above the re-sort pass in _add_prim_mods), so this
            # doesn't lose the eventual real execute(), only the early, repeated ones.
            if not chunk_notice._loading_stage:
                self.__value_sender.execute()
            return

        try:
            if not name.endswith("_param_ref") and hasattr(self._param_ref, name):
                return setattr(self._param_ref, name, value)
        except (ReferenceError, AttributeError):
            pass
        return super().__setattr__(name, value)


class NameSyncedContainer(Generic[ChunkT], chunk_notice.ChunkNoticePropagator):
    """Container for holding ChunkCommon objects. The container holds two maps: params & names. The maps
    are ChainMaps, meaning they provide access to all nested NameSyncedContainer param and names as well.
    In practice, this means we can quickly lookup nested children of chunks or groups, without having to
    manually iterate the hierarchy. For this reason, this class provide get_* as well as get_my_* functions,
    where get_my_* functions will only return objects directly owned by this container, and not any nested
    objects.

    params maps from a uuid to a ChunkCommon object. ChunkCommon objects hash by their uuid, so accessing a param
    can be done either by the ChunkCommon object as params[obj], or the uuid: params[obj._hashable]. Where a typehint
    suggests a HashVal argument for lookup (get_param(), get_my_param()), passing a ChunkCommon obj should
    return same as passing obj._hashable.
    TODO: This is a bit un-intuituve, and should perhaps be reconsidered.

    names map betweeen the current (mutable) name of a param and the uuid. This exists in order to quicker acccess
    params by their name. This container need to be maintained and updated whenever a param name is changed, which is currently
    achieved by subclassing ChunkNoticePropagator, in order to insert itself in hierarchy of objects and modifying the mapping
    whenever a name changes.

    Any objects added though
    ``` py
    NameSyncedContainer.add()
    ```
    will have the NameSyncedContainer as owner. Any concrete chunk object who holds an instance of this class should therefore not
    manage ownership of params, instead, it should own the NameSyncedContainer only. This class will not send any notices though, only propagate
    notices from children.
    """

    def __init__(self):
        super().__init__()

        self._last_added: Optional[HashVal] = None
        self._last_removed: Optional[HashVal] = None

        self._params: ChainMap[ChunkHash, ChunkT] = ChainMap()
        self._names: ChainMap[str, ChunkHash] = ChainMap()

    def __copy__(self):
        copied = NameSyncedContainer()

        for param in self._params.maps[0].values():
            copied.add(copy.copy(param))

        copied._copy_callbacks(self)
        return copied

    def empty(self):
        return not bool(self._params)

    def serialize(self, glb):
        if (res := super().serialize(glb)) and isinstance(res, chunk_serial.SerializedClass):
            # Serialize params as a temp 'params' attribute. This will be de-serialized in post_serial_setattr
            res.attributes["params"] = list(self.get_my_params())

        return res

    def post_serial_setattr(self, name, value):
        if name == "params":
            # Add the params to this container by calling add. Previously we created the params in place and passed them to
            # the NameSyncedContainer constructor. The reason for the change is to be able to modify the container before
            # adding the parameters; Owner is set on the params in add(), but this sets the graph_id as well, which is wrong
            # on the scene 'chunks' container. The graph_id should ideally be smarter, but for now this solves the issue.
            # TODO: Rework graph_id
            for i in value:
                if self.get_param(i):
                    continue

                self.add(i)
                if isinstance(i, ChunkContainerBase):
                    self.add_nested(i.children)  # type: ignore
            return
        return super().post_serial_setattr(name, value)

    def get_notice_data(self) -> Self:
        return self

    def clear(self):
        """
        Clear all children from this container. This will not delete the children explicitly as references to the children may
        be held elsewhere. However, it can be expected that the children are implicitly deleted when calling this function, as
        other objects should preferably only store weakrefs to the params
        """
        self._names.clear()
        self._params.clear()

    def add(self, obj: "ChunkT | ProxyParam", send=True):
        """Add a chunk object to this container. This will make the container the owner of the param, and store the
        object in self.params, and obj.name in self.names. Access to the object can thus be done through
            self.get_param(), self.get_by_name()
        or
            self.params.maps[0].get(self.names.maps[0].get("some_name"))
        since the container manages the name<->obj relationship, some care need to be taken in order for this relationship to be maintained.
        For example, do not add params to self.params or self.names directly, instead, use this function to get proper behaviour.

        If the added param is an instance of ChunkContainerBase, the children of the new param will be appended to the self.params and self.names
        ChainMaps, which allows for fast access of nested params.

        Args:
            obj (ChunkCommon): The param to add. Nothing is done to avoid duplication across layers.
        """

        # Append child the object + name to the first layer of the ChainMaps
        self._params.maps[0][obj.get_hash()] = obj  # type: ignore
        self._names.maps[0][obj.name] = obj.get_hash()

        # Mark self as the owner of the added obj as we need to be notified when the object changes
        obj.set_owner(self)

        if send:
            self.send_added(obj)

    def add_nested(self, nested: "NameSyncedContainer[ChunkT]"):
        """
        Appends nested params to the ChainMap. This allows for fast lookups accross the layers.

        Args:
            nested ("NameSyncedContainer[ChunkT]"): The nested params to add
        """
        self._params.maps.append(nested._params)
        self._names.maps.append(nested._names)

    @staticmethod
    def create_proxy(source: "NameSyncedContainer", target: "NameSyncedContainer"):
        for param in source.get_my_params():
            proxy = ProxyParam(param)
            target.add(proxy)
        return target

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        # Override to reflect any changes on the children in this ds. Used to update the names when a name changes
        if notice and (p := notice.data):
            if isinstance(p, AttributeSender) and p.name == "name" and p.old_value in self._names.maps[0]:
                self.name_changed(p.old_value)  # type: ignore

        return super().data_changed(notice)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        # We are only interested in hijacking data_changed, and should not send any signals.
        return chunk_notice.ChunkNoticeAlias.NONE

    def update_names(self):
        """Update the names ChainMap for all layers to reflect the names of the params. Can be used as brute-force
        to get correct mapping
        """
        invalid = []
        for i, mapping in enumerate(self._names.maps):
            for name, idx in mapping.items():
                obj = self.get_param(idx)

                if obj and obj.name != name:
                    invalid.append((i, name, obj.name))

        for i, old_name, new_name in invalid:
            idx = self._names.maps[i].pop(old_name)
            self._names.maps[i][new_name] = idx

    def name_changed(self, old_name: str):
        """Marks the old_name as changed, and update the names dict for this layer only, to reflect the change.
        old_name will be replaced with whatever the new name of the corresponding object is. This allows for:
        ``` py
            c = Chunk.Slider("A")
            g = Chunk.Group("group")
            g.add_param(c)

            p = g.get_param("A")
            asset(p != None)

            p.name = "B"
            p = g.get_param("B")
            asset(p != None && g.get_param("A") == None)
        ```
        Args:
            old_name (str): The old name mapped to the changed object. This will be removed from the first layer of the ChainMap if found

        Raises:
            ValueError: The old_name was not found
        """

        # Only look for names on the first layer. Groups are responsible for their respective layers
        mapping = self._names.maps[0]

        if old_name in mapping:
            idx = mapping.pop(old_name)
            if obj := self.get_param(idx):
                mapping[obj.name] = idx
        else:
            raise ValueError(f"Failed to update old name {old_name} in container")

    @overload
    def get_param(self, key: Union[ChunkHash, str]) -> Optional[ChunkT]: ...

    @overload
    def get_param(self, key: Union[ChunkHash, str], predicate: Optional[Predicate[ValueT]]) -> ValueT: ...

    def get_param(
        self, key: Union[ChunkHash, str], predicate: Optional[Predicate[ValueT]] = None
    ) -> Optional[ChunkT] | ValueT:
        """Retrieve a param from this datastructure. Key can be either a ChunkCommon (It hashes to the HashVal),
        an explicit HashVal (ChunkObject.get_hash()) or a string (name or path). If the key is a path (identifier) to a param such as:
        aChunk.aGroup.aParam, the returned param will look for a param at the path specified. If just a name is supplied, the first
        found param with the name is returned.

        Returns:
            Optional[ChunkT]: The found Chunk object if found
            ValueT: The found Chunk object matching the predicate if supplied
        """
        if isinstance(key, str):
            res = self.get_by_name(key)
        else:
            res = self._params.get(key)

        if predicate is None:
            return res

        if not predicate(res):
            raise ValueError(f"get_param predicate {predicate} failed for parameter {key}")
        return res

    @overload
    def get_by_name(self, name: str) -> Optional[ChunkT]: ...

    @overload
    def get_by_name(self, name: str, predicate: Optional[Predicate[ValueT]]) -> ValueT: ...

    def get_by_name(self, name: str, predicate: Optional[Predicate[ValueT]] = None) -> Optional[ChunkT] | ValueT:
        """
        Excplicitly retrieve a param by its name. In order to find nested children, one can
        pass the hierarchy to the 'name' argument by adding a dot (.) for each level in the hierarchy:

        ``` py
        myChunk.get_param("groupA.groupB.slider")
        ```

        Returns:
            Optional[ChunkT]: The found Chunk object if found
            ValueT: The found Chunk object matching the predicate, if supplied
        """
        if "." in name:
            hierarchy = name.split(".")
            cm: ChainMap = self._names
            idx = 0
            g = hierarchy[0]

            while idx < len(hierarchy) - 1:
                if g not in cm:
                    return None

                if g in cm.maps[0]:
                    # Found g in first layer, so can look for next entry in the query
                    idx += 1

                    # FInd the parameter matching g and update cm to reflected the parameters own children.
                    grp = self._params.get(cm.maps[0].get(g))
                    if not grp or not isinstance(grp, ChunkContainerBase):
                        raise NameError(f"Could not find param {name}. {g} is not a container type parameter")

                    cm = grp.children._names

                g = hierarchy[idx]

            if key := cm.get(g):
                res = self._params.get(key)
                if not predicate or predicate(res):
                    return res
                raise ValueError(f"get_by_name predicate {predicate} failed for parameter {key}")

        if key := self._names.get(name):
            res = self.get_param(key)
            if not predicate or predicate(res):
                return res
            raise ValueError(f"get_by_name predicate {predicate} failed for parameter {key}")

    def get_names(self) -> Tuple[str, ...]:
        """Get all names of this layer and all child layers

        Returns:
            tuple[str, ...]: All param names on this layer and under
        """
        return tuple(self._names.keys())

    def get_my_names(self) -> Tuple[str, ...]:
        """Get all names on this layer only

        Returns:
            tuple[str, ...]: The param names of the first layer
        """
        return tuple(self._names.maps[0].keys())

    @overload
    def get_params(self) -> Tuple[ChunkT, ...]: ...

    @overload
    def get_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ChunkT, ...] | Tuple[ValueT, ...]:
        """Get all params on this layer and under

        Returns:
            Tuple[ChunkT, ...]: The params
            Tuple[ValueT, ...]: The params matching the predicate if supplied
        """
        ps = self._params.values()

        if not predicate:
            return tuple(ps)
        return tuple(filter(predicate, ps))

    @overload
    def get_my_params(self) -> Tuple[ChunkT, ...]: ...
    @overload
    def get_my_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_my_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ChunkT, ...] | Tuple[ValueT, ...]:
        """Get all params on this layer only

        Returns:
            Tuple[ChunkT, ...]: The params
            Tuple[ValueT, ...]: The params matching the predicate, if supplied
        """
        ps = self._params.maps[0].values()
        if not predicate:
            return tuple(ps)
        return tuple(filter(predicate, ps))

    def send_added(self, obj: "ChunkCommon | ProxyParam"):
        """
        Sends a PARAM_ADDED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the new parameter
        """
        self._last_added = obj._hashable

        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, obj)

    def send_removed(self, obj: "ChunkCommon | ProxyParam"):
        """
        Sends a PARAM_REMOVED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the removed parameter
        """

        self._last_removed = obj._hashable
        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, obj)

    def __delitem__(self, key: ChunkHash):
        if obj := self.get_param(key):
            # send_removed uses obj.send_self(). For this reason, we need to call send_remove before set_owner(None),
            # as send_self() will propagate the notice to the owners
            self.send_removed(obj)

            # obj.name can drift from the name-map key (a rename or a partially-applied clone can
            # desync the map). Remove the entry by matching value (the key) and fall back to name,
            # popping defensively so delete never crashes with a KeyError.
            names0 = self._names.maps[0]
            if names0.get(obj.name) == key:
                del names0[obj.name]
            else:
                for nm in [n for n, k in list(names0.items()) if k == key]:
                    del names0[nm]
            self._params.maps[0].pop(key, None)

            if obj.get_owner() == self:
                obj.set_owner(None)

    def pop(self, key: ChunkHash) -> Optional["ChunkCommon"]:
        """
        Pop the object at key without calling send_remove

        Args:
            key (ChunkHash): The chunk object to remove

        Returns:
            ChunkCommon | None: The pop'ed object, or None if the object is unavailable
        """
        obj = self.get_param(key)
        if obj:
            del self[obj]
            return obj


######################
# CHUNK BASE CLASSES #
######################


@attrs.frozen
class HashVal:
    """
    A hash value used by Chunk objects. The actual hash is a created from a uuid4. This
    is held by ChunkCommon, and thus provides a unique id for all chunk objects
    """

    @staticmethod
    def generate_key() -> int:
        """Generate a new uuid value used for hashing. Should only be used internally"""
        return uuid.uuid4().int  # type: ignore

    _uuid: int = attrs.field(
        factory=generate_key,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    def __hash__(self) -> int:
        return hash(self._uuid)

    def __eq__(self, value: object) -> bool:
        return hash(self) == hash(value)

    def __copy__(self):
        # Should never be called, but just in case...
        # Copying this class should result in a new key
        cls = self.__class__
        c = cls.__new__(cls)
        c.__init__()
        return c


class ChunkCommon(AttributesBase):
    """Base class for all Chunk objects. Should not be instantiated directly, but inherited by concrete classes.
    Holds attributes that all chunk objects are excpected to support

    Attributes:
        name: The name of the chunk object
        label: An optional label to display in the ui
        tooltip: A description of the chunk object
        icon: A path to an image which to use as icon
        user_data: A dict of arbitrary data to store on the param
        layout: Flags for setting the layout of the chunk object in the UI
        state: The current state
        flags: Some implementation specific flags. Unused by default

    """

    # Please do not mutate!
    _hashable: HashVal = attrs.field(
        init=False,
        factory=HashVal,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Name for the chunk object
    name: str = attrs.field(default="")

    # Explicit label (nice name). Will be derived from name if not set
    label: Optional[str] = attrs.field(default="")
    tooltip: Optional[str] = attrs.field(default="")
    icon: Optional[str] = attrs.field(default="")  # Path to icon

    # Hold custom data which is not used directly by the chunk
    user_data: Dict[str, Any] = attrs.field(factory=dict, repr=False)

    # Orientation of the widget, respected if relevant
    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.VERTICAL)

    # State flags of the param - hidden, collapsed, disabled
    state: ChunkFlags.State = attrs.field(
        default=ChunkFlags.State.NONE,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.STATE_CHANGED),
    )

    # Type specific flags, can be used for setting custom flags.
    flags: IntFlag = attrs.field(
        default=0,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED),
    )  # type: ignore

    # Hold a weakref to the owning chunk for faster access to it, without having to traverse the
    # hierarchy
    _chunk_parent: Optional[weakref.ReferenceType] = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    def get_identifier(self) -> str:
        """
        Get an identifier for this chunk object. The identifier is a dot(.) separated path, from the top-most
        owner, usually the ChunkBase, to this object. F.e. MyChunk.SomeGroup.MyParam

        Returns:
            str: The string identifier for this chunk object
        """
        names = [self.name]
        owner = self.get_owner()
        while owner:
            if isinstance(owner, ChunkCommon):
                names.append(owner.name)
            owner = owner.get_owner()
        return ".".join(reversed(names))

    def get_chunk(self) -> Optional["ChunkBase"]:
        """
        Get the owning Chunk if it exists.

        Returns:
            ChunkBase | None: The chunk that owns this object
        """
        if self._chunk_parent is None or self._chunk_parent() is None:
            owner = self.get_owner()
            while owner and not isinstance(owner, ChunkBase):
                owner = owner.get_owner()

            if not owner:
                return None
            self._chunk_parent = weakref.ref(owner)

        return self._chunk_parent()

    def get_layer_identifier(self) -> str:
        """
        Get the layer identifier used by the ChunkLayerManager

        Returns:
            str: the layer identifier for this param
        """

        if c := self.get_chunk():
            name = c.name
            val = str(hash(c))
        else:
            name = self.name
            val = str(hash(self))

        return f"{name}_{val}"

    def get_logger(self) -> logging.Logger:
        if not self._logger:
            self._logger = logging.getLogger(f"usd_chunks.{self.__class__.__name__}.{self.name}")
        return self._logger

    def is_blocked(self) -> bool:
        v = self.has_state(ChunkFlags.State.DISABLED)
        return v or super().is_blocked()

    def get_hash(self) -> HashVal:
        """
        Get the HasVal associated with the chunk object. Can be useful to not increase the ref
        count when storing this object is a hash map

        Returns:
            ChunkHash: The hashval of this object
        """
        return self._hashable

    def get_edit_context(self) -> Sdf.Layer | None:
        """
        Use when modifying the stage and the changes should be added to the local
        Sdf.Layer corresponding to this chunk object. Will traverse up the hierarchy
        to find the nearest ChunkBase parent and use the layer from it, instead of having
        seperate layers for every parameter

        ``` py
        with Usd.EditContext(stage, param.get_edit_context()):
            # modify the stage
        ```

        Returns:
            Sdf.Layer: A layer for this object
        """
        if not chunk_registry.ChunkLayerManager().initialized:
            return

        if c := self.get_chunk():
            layer = chunk_registry.ChunkLayerManager().get_or_create_layer(c)
            if layer and layer.IsMuted():
                return None
            return layer

    def reset_state(self):
        """
        Clears DISABLED and HIDDEN states
        """
        self.unset_state(ChunkFlags.State.DISABLED | ChunkFlags.State.HIDDEN)

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "name":
            return re.sub(r"\W+", "", str(val))[0:100]

        if inspect.isclass(attr.type) and issubclass(attr.type, Enum):
            if val is not None and not isinstance(val, attr.type):
                try:
                    return attr.type(val)
                except TypeError:
                    return val

        return super().convert_attr(attr, val)

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        # Called when a attrs.field is modified. Should raise ValueError if the validation fails
        if attr.name in ["name"]:
            return self._validate_name(attr, val)
        if attr.name == "icon" and val:
            # Icon path should exist if. if bool(val) is False, then we can ignore the validation
            return self._validate_existing_path(attr, val)
        if attr.name in ["tooltip", "label"]:
            return self._validate_string(attr, val)

        return super().validate_attr(attr, val)

    def _validate_name(self, attribute, value) -> bool:
        self._validate_string(attribute, value)

        if " " in value:
            raise ValueError(f"{value} contains spaces")

        if value:
            expr = r"[_a-zA-Z0-9]+"
            found = re.fullmatch(expr, value)
            if found is None:
                raise ValueError(f"Name: '{value}' contains illegal characters. Legal characters = [a-z, A-Z, 0-9, _]")

        return True

    def _validate_path(self, attr: attrs.Attribute, value) -> bool:
        """Check if the given value is a valid os path to be used as an attribute.
        Can be called in _validate or _convert of subclasses when checking a path attribute.
        Use _validate_existing_path to check for existing paths. Currently this does nothing.
        Holding off with this as some string parameter may support Sdf.AssetPath or pathlib.Path
        TODO: implement

        Args:
            attr (Attrs.Attribute): _description_
            value (_type_): _description_

        Returns:
            bool: True if the value is a valid path
        """
        return True

    def _validate_existing_path(self, attr: attrs.Attribute, value) -> bool:
        """Check if the given value is a valid os path and that it exists

        Args:
            attr (attrs.Attribute): The attribute to validate
            value (str): The value to validate

        Returns:
            bool: True if succcess
        """
        if not self._validate_path(attr, value):
            return False

        if not os.path.exists(value):
            raise ValueError(f"path for attribute {attr.name} does not exist")

        return True

    def _validate_string(self, attr: attrs.Attribute, value) -> bool:
        if not isinstance(value, str):
            raise ValueError(f"attribute {attr.name} should be a string. Attempted to set {value}")
        return True

    def get_label(self) -> str:
        """
        Get the label of this chunk object. If no label is set, returns the name

        Returns:
            str: The label
        """
        return self.name if not self.label else self.label

    def set_flag(self, flag: IntFlag):
        """
        Enables a flag

        Args:
            flag (IntFlag): The flag to enable
        """
        self.flags |= flag

    def unset_flag(self, flag: IntFlag):
        """
        Disable a flag

        Args:
            flag (IntFlag): The flag to disable
        """
        self.flags &= ~flag

    def has_flag(self, flag: IntFlag) -> bool:
        """
        Check if a flag is enabled

        Args:
            flag (IntFlag): The flag to check

        Returns:
            bool: If the flag is enabled or not
        """
        # Compared as plain ints: IntFlag.__and__ constructs (and caches, and looks up) a
        # whole new flag member just to be thrown away by bool(). This is called millions of
        # times when walking a large graph -- get_chunks() alone asks twice per chunk -- and
        # the enum machinery was measurably the second-largest cost of building one.
        return bool(int(self.flags) & int(flag))

    def set_state(self, state: ChunkFlags.State):
        """
        Set a state

        Args:
            state (ChunkFlags.State): The state to set
        """
        self.state |= state

    def unset_state(self, state: ChunkFlags.State):
        """
        Unset a state

        Args:
            state (ChunkFlags.State): The state to unset
        """
        self.state &= ~state

    def has_state(self, state: ChunkFlags.State) -> bool:
        """
        Check if state is set

        Args:
            state (ChunkFlags.State): The state to check

        Returns:
            bool: True if the state is set, otherwise False
        """
        return bool(self.state & state)

    def set_selected(self, val: bool):
        if val:
            self.set_state(ChunkFlags.State.SELECTED)
        else:
            self.unset_state(ChunkFlags.State.SELECTED)

    def __hash__(self) -> int:
        return hash(self._hashable)

    def __eq__(self, value: object) -> bool:
        return self._hashable == value

    def get_stage(self) -> Usd.Stage | None:
        """Easy access to the current UsdStage.

        Returns:
            Usd.Stage: The stage held by the SceneSender of ChunkRegistry
        """
        return self.get_scene().get_stage() or chunk_notice._loading_stage

    def get_scene(self) -> "SceneSender":
        """Easy access to the current chunk scene.

        Returns:
            SceneSender: The SceneSender of ChunkRegistry
        """
        return chunk_registry.ChunkRegistry().scene

    def can_delete(self):
        return not bool(list(self.get_dag_node().get_outputs()))

    def request_delete(self):
        """Sends a notice for owners to delete this Chunk object. It's the owners responsibility to remove their reference to this object.
        If this parameter lies withing a ChunkContainerBase, then the containers' delete_param_or_group is called, with this instance as argument.

        In addition, all callbacks will be removed from this object, and the owner will be set to None.
        """

        # Retire the chunk from module execution before anything is dismantled. Teardown
        # detaches params and edits the layer stack, and both cascade as notices that can
        # re-enter the DAG solve and call this chunk's module.execute() again — against a
        # half-dismantled chunk whose get_param() now returns None, which is an AttributeError
        # in essentially every chunk script (none of them defend against their own params
        # vanishing mid-run, nor should they have to). Never cleared: a chunk is not reusable
        # after request_delete. Read by chunk_notice's execute dispatch.
        self._deleting = True

        # Make sure we're enabled when deleting as being disabled will block necessary notices from being sent
        self.unset_state(ChunkFlags.State.DISABLED)
        # Same for the 'blocked' flag (set by view-driven re-cook on out-of-view chunks): a blocked
        # chunk suppresses its own notices, so CHUNK_DELETED would never fire and the USD prim would
        # be left behind on the stage.
        self.set_blocked(False)

        if not self.can_delete():
            logger.warning(f"Deleting node: {self.name} with connected outputs. Stuff may break")
            self.get_dag_node().disconnect()

        owner = self.get_owner()
        if owner and isinstance(owner, ChunkContainerBase):
            # Owner is a group, so delete this param from the group
            owner.remove_param_or_group(self)
        elif owner and isinstance(owner, NameSyncedContainer):
            # Owner is a NameSyncedContainer, pop it from the contiainer. When successfully removed,
            # the container will send PARAM_REMOVED with it's owning ChunkContainerBase as sender.
            owner.pop(self)

        self._callbacks.clear()
        self.set_owner(None)
        self.send_self(chunk_notice.ChunkNoticeAlias.CHUNK_DELETED)


class ChunkUsdAttributeSupport(AttributesBase):
    """
    This inteface class can be subclassed for valued parameter classes in order to support connections
    to UsdAttribute objects. This enables a parameter to be generated from a UsdAttribute, and creating
    the NoticeConnections for managaging the value setting and getting between the param and the
    UsdAttribute
    """

    # The storage attribute path (/chunks/<name>/inputs:<param>) — the param's authoritative USD value.
    _param_attr_path: Optional[str] = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Real prim attr paths this storage attr drives via USD native connections.
    # Connections are re-authored whenever _param_attr_path is set.
    _driven_attr_paths: List[str] = attrs.field(
        factory=list,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    # Lifecycle connection — fires update_usd_prop when param value changes.
    _usd_prop_connection: Optional[chunk_notice.NoticeConnection] = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        my_notice = super().data_changed(notice)

        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if (
            notice
            and stage
            and self._param_attr_path
            and not self.is_blocked()
            and isinstance(notice.data, AttributeSender)
            and notice.data.name == "value"
            and hasattr(self, 'raw_value')
            # ChunkVectorBase's "value" bubbles up from a child's scalar AttributeSender —
            # notice.data.value below would be a single float, not the vector's tuple.
            # ChunkVectorBase.data_changed handles its own (correctly-typed) version of this.
            and not isinstance(self, ChunkVectorBase)
        ):
            raw = self.raw_value
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            val = notice.data.value
            if val is not None:
                chunk_notice._usd_write_active = True
                try:
                    if usd_attr := stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path)):
                        # self._param_attr_path is always the chunk-prim's own storage attr —
                        # bookkeeping, independent of the stage's ambient/Edit-Target-driven
                        # edit target (which _write_driven_attrs below correctly targets
                        # separately via get_edit_context()). It already exists (that's what
                        # having a _param_attr_path means), so update it wherever its spec
                        # already lives — not wherever Edit Target currently resolves to,
                        # which may have moved on since it was first authored.
                        home = chunk_utils.get_attr_home_layer(usd_attr) or stage.GetSessionLayer()
                        with Usd.EditContext(stage, home):
                            if not is_expr:
                                current = usd_attr.Get(0)
                                # Don't write a string to a typed (non-String) attr — an unresolved
                                # forwarded value can arrive as text and raise a type-mismatch.
                                bad_str = isinstance(val, str) and usd_attr.GetTypeName() != Sdf.ValueTypeNames.String
                                if not bad_str and current != val:
                                    usd_attr.Set(val)
                            if is_expr:
                                chunk_root = str(usd_attr.GetPrim().GetParent().GetPath())
                                conns = []
                                for node, attr_name in chunk_utils.parse_expression(raw):
                                    ref_prim = stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}"))
                                    # The referenced node may not exist (a stale/renamed expression
                                    # ref, or a lofted chain pointing at a prim not on this stage) —
                                    # GetAttribute on a null prim raises, so guard it and default to
                                    # the inputs: port. USD lets a connection target a not-yet-present
                                    # path, so the connection is still authored.
                                    use_out = (
                                        ref_prim.IsValid()
                                        and ref_prim.GetAttribute(f"outputs:{attr_name}").IsValid()
                                    )
                                    suffix = f"outputs:{attr_name}" if use_out else f"inputs:{attr_name}"
                                    conns.append(Sdf.Path(f"{chunk_root}/{node}.{suffix}"))
                                usd_attr.SetConnections(conns)
                                if usd_attr.GetTypeName() == Sdf.ValueTypeNames.String and usd_attr.Get(0) != raw:
                                    usd_attr.Set(raw)
                    self._write_driven_attrs(stage, val)
                finally:
                    chunk_notice._usd_write_active = False

        return my_notice

    def _write_driven_attrs(self, stage: Usd.Stage, val: Any) -> None:
        """Push val (already resolved) to every real prim attr in _driven_attr_paths.

        Uses get_edit_context() so the write lands in the chunk's own layer (anonymous
        for procedural chunks, file layer for interactive ones), keeping the user's
        edit-target layer free of value opinions. Connections between the driven attr
        and the storage attr are topology only — USD Get() on the driven attr doesn't
        follow them, so the resolved value must be copied explicitly for any GUI
        reading that attr directly to see it.
        """
        if val is None or not self._driven_attr_paths:
            return
        chunk_layer = self.get_edit_context()
        if not chunk_layer:
            return
        for path in self._driven_attr_paths:
            sdf_path = Sdf.Path(path)
            if driven_attr := stage.GetAttributeAtPath(sdf_path):
                if driven_attr.Get(0) != val:
                    with Usd.EditContext(stage, chunk_layer):
                        driven_attr.Set(val)

    @abstractmethod
    def update_usd_prop(self, notice: chunk_notice.AttributeChangeNotice, sender: AttributeSender):
        """
        A callback for setting the UsdAttribute on changes to the sender. Used by parameters when
        modifying the 'value' attribute, then this will be invoked with sender build the 'value'
        AttributeSender object, and the notice.data will be the UsdAttribute

        Args:
            notice (chunk_notice.AttributeChangeNotice): The notice holding the UsdAttribute
            sender (AttributeSender): The sender of the notice
        """
        # ChunkVectorBase.data_changed already handles this write path itself (correctly,
        # with outputs:-preferring connection routing) — this method's SetConnections
        # below hardcodes inputs: unconditionally, so letting both run fights over two
        # different connection targets on every value change.
        if isinstance(notice.data, Usd.Attribute) and not isinstance(self, ChunkVectorBase):
            raw = getattr(self, 'raw_value', None)
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            # Only String-typed attrs can hold the raw expression text itself (needed so
            # _patch_expressions can rewrite it on rename); other types (Float, Float3,
            # etc.) only ever store the resolved value — the expression is reconstructed
            # from SetConnections on reload (see _expression_from_connections).
            if is_expr and notice.data.GetTypeName() != Sdf.ValueTypeNames.String:
                store_val = sender.value
            else:
                store_val = raw if is_expr else sender.value
            chunk_notice._usd_write_active = True
            try:
                # notice.data here is always the chunk-prim's own storage attr (bound via
                # _connect_param_attr — connect_usd_attr's driven-content path is a
                # separate mechanism, _author_driven_connections, scoped via
                # get_edit_context()). It already exists (that's what being bound via
                # _connect_param_attr means), so update it wherever its spec already
                # lives, rather than wherever the stage's ambient/Edit-Target-driven
                # edit target currently points — that may have moved on since it was
                # first authored.
                stage = notice.data.GetStage()
                home = chunk_utils.get_attr_home_layer(notice.data) or stage.GetSessionLayer()
                with Usd.EditContext(stage, home):
                    # A non-String attr must never receive raw expression text: if an expression ref
                    # can't resolve (e.g. a lofted cross-context ref like @ChunkGroup.tx@), sender.value
                    # stays the expression STRING and Set() on a typed (float/etc) attr raises a
                    # type-mismatch. The connection authored below carries the topology, so skip the
                    # value write in that case.
                    bad_str = isinstance(store_val, str) and notice.data.GetTypeName() != Sdf.ValueTypeNames.String
                    if not bad_str and notice.data.Get(0) != store_val:
                        notice.data.Set(store_val)
                    if is_expr:
                        chunk_root = str(notice.data.GetPrim().GetParent().GetPath())
                        notice.data.SetConnections([
                            Sdf.Path(f"{chunk_root}/{node}.inputs:{attr_name}")
                            for node, attr_name in chunk_utils.parse_expression(raw)
                        ])
            finally:
                chunk_notice._usd_write_active = False

        elif isinstance(notice.data, Usd.Relationship) and sender.value is not None:
            # Currently just assume the string is a comma separated list of paths. This will likely
            # be changed in the future for a more robust approach
            paths = sender.value.split(",")
            if not all([Sdf.Path.IsValidPathString(x) for x in paths]):
                return
            targets = [Sdf.Path(x) for x in paths]
            notice.data.SetTargets(targets)

    @abstractmethod
    def usd_prop_changed(self):
        """
        A callback for when the UsdAttribute member was changed. This will update the value of
        the AttributeSender passed to set_usd_attr()..
        """
        if not self._usd_prop_connection:
            return
        if not isinstance(self._usd_prop_connection.sender, AttributeSender):
            return

        attr = self.get_usd_prop()
        if isinstance(attr, Usd.Attribute):
            self._usd_prop_connection.sender.value = attr.Get(0)
        elif isinstance(attr, Usd.Relationship):
            self._usd_prop_connection.sender.value = ",".join([str(x) for x in attr.GetForwardedTargets()])

    def set_usd_prop(self, prop_path: str | Usd.Property, sender: AttributeSender):
        """
        Bind this param to a specific USD property with an explicit sender (used for relationships).
        Prefer _connect_param_attr for standard attribute binding.
        """
        path = str(prop_path.GetPath()) if isinstance(prop_path, Usd.Property) else prop_path
        self._param_attr_path = path

        if self._usd_prop_connection:
            self._usd_prop_connection.disconnect()

        self._usd_prop_connection = chunk_notice.NoticeConnection(
            self.update_usd_prop, chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED, sender, owner=self
        )
        self.usd_prop_changed()

    def get_usd_prop(self) -> Usd.Property | None:
        """
        Return the storage attribute this param is bound to.

        Returns:
            Usd.Property | None: The bound storage USD property, or None.
        """
        stage = self.get_stage()
        if stage and self._param_attr_path:
            return stage.GetPropertyAtPath(self._param_attr_path)

    def _connect_param_attr(self, attr: Usd.Property) -> None:
        """
        Internal: bind this param to its storage attribute for UI↔USD sync.
        After binding, re-authors any pending USD connections to driven real attrs.
        Called by _load_wire_inputs; not part of the public chunk-module API.

        Args:
            attr (Usd.Property): The storage attribute (/chunks/<name>/inputs:<param>).
        """
        path = str(attr.GetPath())
        if self._param_attr_path == path:
            return
        self._param_attr_path = path
        if not self._usd_prop_connection:
            try:
                if sender := self.get_attribute_sender("value"):
                    self._usd_prop_connection = chunk_notice.NoticeConnection(
                        self.update_usd_prop,
                        chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                        sender,
                        owner=self,
                    )
            except AttributeError:
                pass
        self._author_driven_connections()
        if isinstance(attr, Usd.Attribute):
            val = attr.Get(0)
            connections = attr.GetConnections()
            if not connections:
                if val is not None:
                    self.value = val
            elif isinstance(val, str) and chunk_utils.has_expression(val):
                self.value = val
            else:
                # Non-string (e.g. Float) expression attrs only ever store the connection, never
                # the expression text, so reconstruct it here — but only from SIBLING refs. A loft
                # connection points UP to this param's own container (the group's interface attr);
                # it's driven by the loft forwarding callback, not the expression system. Rebuilding
                # @Container.attr@ from it makes an unresolvable sibling ref (the "Expression ref
                # not found" spam) and would overwrite the forwarded value with the expression text.
                my_prim = Sdf.Path(path).GetPrimPath()
                sibling_conns = []
                for c in connections:
                    target_prim = Sdf.Path(str(c)).GetPrimPath()
                    if my_prim != target_prim and my_prim.HasPrefix(target_prim):
                        continue  # ancestor target == loft (upward) connection
                    sibling_conns.append(c)
                if expr := self._expression_from_connections(sibling_conns):
                    self.value = expr

    @staticmethod
    def _expression_from_connections(connections) -> Optional[str]:
        tokens = []
        for conn in connections:
            path = Sdf.Path(str(conn))
            node_name = path.GetPrimPath().name
            param_name = path.name.rsplit(":", 1)[-1]
            tokens.append(f"@{node_name}.{param_name}@")
        return "".join(tokens) if tokens else None

    def _resolve_expression_ref(self, node_name: str, param_name: str) -> Optional["ChunkCommon"]:
        chunk = self.get_chunk()
        if not chunk:
            return None
        context = chunk.get_context()
        if context:
            return context.get_param(f"{node_name}.{param_name}")
        try:
            scene = chunk_registry.ChunkRegistry().get_scene()
            target = scene.get_chunk(node_name)
            return target.get_param(param_name) if target else None
        except Exception:
            return None

    def connect_usd_attr(self, attr: Usd.Property) -> None:
        """
        Drive a real prim attribute from this param's storage attribute via a USD native
        connection. The storage attr becomes the connection source; the real attr is the target.
        If the real attr does not exist yet it will be created in _author_driven_connections once
        the storage attr is known. If the storage attr is not yet known the whole operation is
        deferred until _connect_param_attr is called.

        Args:
            attr (Usd.Property): The real prim attribute to drive (e.g. /cameras/Cam.focalLength).
                                 May be an invalid/non-existent attribute — the path is stored and
                                 the attribute is created on demand.
        """
        path = str(attr.GetPath())
        if path in self._driven_attr_paths:
            return
        self._driven_attr_paths.append(path)
        if self._param_attr_path:
            self._author_driven_connections()

    def _author_driven_connections(self) -> None:
        """
        For every path in _driven_attr_paths: ensure the attribute exists on the real prim
        (creating it with the storage attr's type if absent), then author
        real_attr.SetConnections([storage_path]).

        Always writes into the chunk's own layer (anonymous for procedural, file for
        interactive) so these connections never land in the session layer and can be
        overridden in the same layer by wire_shader_input after connect_usd_attr.
        """
        if not self._param_attr_path:
            return
        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if not stage:
            return
        storage_attr = stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path))
        if not (storage_attr and storage_attr.IsValid()):
            return
        storage_path = Sdf.Path(self._param_attr_path)
        type_name = storage_attr.GetTypeName()
        chunk_layer = self.get_edit_context()

        for path in self._driven_attr_paths:
            sdf_path = Sdf.Path(path)
            real_attr = stage.GetAttributeAtPath(sdf_path)
            if not (real_attr and real_attr.IsValid()):
                prim = stage.GetPrimAtPath(sdf_path.GetPrimPath())
                if not (prim and prim.IsValid()):
                    continue
                real_attr = prim.CreateAttribute(sdf_path.name, type_name, custom=False)
            if real_attr and real_attr.IsValid():
                if chunk_layer:
                    with Usd.EditContext(stage, chunk_layer):
                        real_attr.SetConnections([storage_path])
                else:
                    real_attr.SetConnections([storage_path])

        # SetConnections only authors topology — Get() on the driven attr doesn't follow
        # it, so push the current resolved value too. Needed because connect_usd_attr is
        # (re-)called on every load/execute, e.g. after a roundtrip restores an expression
        # via _connect_param_attr before this chunk's own driven wiring runs.
        self._write_driven_attrs(stage, getattr(self, 'value', None))

    @classmethod
    def from_usd_attr(cls, attr: Usd.Attribute, *args, **kwargs) -> "ChunkUsdAttributeSupport":
        """
        Create a new parameter from a UsdAttribute and set up the connection for setting
        the attribute upon value changed on the param.

        Args:
            attr (Usd.Attribute): The attribute to represent as a param

        Returns:
            ChunkUsdAttributeSupport: A parameter that implements the ChunkUsdAttributeSupport interface
        """
        attr_name = attr.GetName()

        if (args and isinstance(args[0], str)) or kwargs.get("name"):
            param = cls(*args, **kwargs)
        else:
            kwargs["name"] = attr_name.split(":")[-1]
            param = cls(*args, **kwargs)

        if hasattr(param, "default_value"):
            if attr.HasFallbackValue():
                default_value = attr.GetMetadata("default")
            else:
                default_value = attr.Get(0)

            param.default_value = default_value  # type: ignore

        param._connect_param_attr(attr)

        return param


class ChunkParamBase(Generic[ValueT], ChunkCommon, ChunkUsdAttributeSupport):
    """
    Base class for valued parameters

    Attributes:
        layout (ChunkFlags.LayoutFlags): The layout of this parameter. Defaults to HORIZONTAL
        default_value (Optional[ValueT]): The default value of this parameter
        value (Optional[ValueT]): The value of this parameter
        custom_widget (Optional[ChunkParamWidget]): An optional custom widget. get_type() must resolve to ParamType.CUSTOM in order to use this
    """

    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.HORIZONTAL)

    default_value: Optional[ValueT] = attrs.field(default=None)

    # Value is not an argument to __init__. Will set to default_value
    value: ValueT = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED)
    )

    # If possible, will use this custom widget in the ui.
    # TODO: Currently, this only works with ChunkParamBase objects with type == CUSTOM
    #       This should also work on groups and chunks... But thats a bit more complicated
    custom_widget: Optional[ChunkParamWidgetBase] = attrs.field(default=None)

    _expression_connections: list = attrs.field(
        default=attrs.Factory(list), init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "value":
            if val is None:
                return self.default_value
            if isinstance(val, str) and chunk_utils.has_expression(val):
                return val  # Store expression strings as-is without type conversion

        if attr.name == "default_value":
            supported = ChunkFlags.ParamTypeToTypes(self.get_type())

            if not supported:
                return val

            type_default = next(iter(supported))

            if val is None:
                return type_default()

            if type(val) not in supported:
                # When the value does not match the type_default, we can try to convert it. If failed, use the type default
                try:
                    # Try casting the type_default to val
                    return type_default(val)
                except Exception:
                    # Casting failed, so lets just use the type_default
                    return type_default()

        return super().convert_attr(attr, val)

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "value" and val is not None:
            if isinstance(val, str) and chunk_utils.has_expression(val):
                return True  # Expression strings bypass type checking; resolved in value_changed
            expected_type = ChunkFlags.ParamTypeToTypes(self.get_type())

            t = type(val)
            if t is AttributeSender:
                if not val.value:
                    return True
                t = type(val.value)

            if t not in expected_type:
                raise ValueError(
                    f"Value of {val} is not supported for attribute {attr.name}, type is {t}. Expects {expected_type}"
                )
            return True

        if attr.name == "custom_widget":
            # Custom widget should be a subclass of ChunkParamWidget
            if val is not None and not isinstance(val, ChunkParamWidgetBase):
                raise ValueError(f"custom_widget {val} is not a subclass of ChunkParamWidget")
            return True
        return super().validate_attr(attr, val)

    @classmethod
    def get_type(cls) -> ChunkFlags.ParamType:
        """Get the ParamType for this parameter.
        If this returns a combination of ParamType enums, the subclass should also implement evaluate_type() to evaluate the runtime type.
        Required by values parameters

        Returns:
            ChunkFlags.ParamType: The type of this parameter. Defaults to NONE, but should be overwritten in subclasses
        """
        return ChunkFlags.ParamType.NONE

    def evaluate_type(self) -> ChunkFlags.ParamType:
        """Get the runtime ParamType for this parameter. If get_type() supports multiple types (FLOAT_SLIDER | INT_SLIDER), then this function should
        evaluate what type is currently used.

        Returns:
            ChunkFlags.ParamType: The runtime type of this parameter. Uses get_type() by default
        """
        return self.get_type()

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED

    def connect_value(self, other: "ChunkParamBase[ValueT]"):
        my_t = self.evaluate_type()
        other_t = other.evaluate_type()
        if my_t not in other_t:
            raise ValueError(f"Can't connect {other_t.name} to parameter {my_t}")

        idx = -1
        # If other already is connected to self, then we can use that index
        for i, node in self.get_dependencies(is_inst(type(other))):
            if node == other:
                idx = i
                break

        if idx == -1:
            # The nodes are not connected, so we add other as dependency
            idx = self.add_dependency(other)
        self.set_function("value", lambda x, _: x.get_dependency(idx, is_inst(type(other))).value)

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if attrname == "default_value" and hasattr(self, "value") and (self.value is None or self.value == old_value):
            self.value = value

        if attrname == "value":
            self._expression_connections = []
            if isinstance(value, str) and chunk_utils.has_expression(value):
                for node_name, param_name in chunk_utils.parse_expression(value):
                    ref = self._resolve_expression_ref(node_name, param_name)
                    if ref is None:
                        logger.warning(f"Expression ref @{node_name}.{param_name}@ not found")
                        continue
                    conn = chunk_notice.NoticeConnection(
                        method=self._on_ref_changed,
                        alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED,
                        sender=ref.get_attribute_sender("value"),
                    )
                    self._expression_connections.append(conn)

        return super().value_changed(attrname, old_value, value)

    def _on_ref_changed(self, notice=None, sender=None):
        attr_sender = self.get_attribute_sender("value")
        attr_sender.old_value = self.__dict__.get("value")
        attr_sender.execute()

    @property
    def raw_value(self) -> Any:
        """The stored value before expression evaluation. Use this for display purposes."""
        return self.__dict__.get("value")

    def __getattribute__(self, name: str) -> Any:
        if name == "value" and "value" in self.__dict__:
            raw = self.__dict__["value"]
            if isinstance(raw, str) and chunk_utils.has_expression(raw):
                return self._resolve_expression_string(raw)
            return raw
        return super().__getattribute__(name)

    def _resolve_expression_string(self, expr: str) -> Any:
        tokens = chunk_utils.parse_expression(expr)
        if len(tokens) == 1 and expr.strip() == f"@{tokens[0][0]}.{tokens[0][1]}@":
            ref = self._resolve_expression_ref(*tokens[0])
            return ref.value if ref is not None else self.__dict__.get("default_value")
        result = expr
        for node_name, param_name in tokens:
            ref = self._resolve_expression_ref(node_name, param_name)
            val = str(ref.value) if ref is not None and ref.value is not None else ""
            result = result.replace(f"@{node_name}.{param_name}@", val)
        return result


class ChunkContainerBase(ChunkCommon):
    """
    A base class for container type parameters such as Vectors, Groups and Chunks. The container can hold
    parameters (ChunkParamBase) or other ChunkContainerBase objects.

    Attributes:
        children (NameSyncedContainer): The underlying container owning the child parameters
        widget_factory (Optional[ChunkWidgetFactoryBase]): A factory that the ui implementation can use for
            changing the default ui implementation of the container and its children
    """

    children: NameSyncedContainer[ChunkCommon] = attrs.field(
        init=False,
        factory=NameSyncedContainer[ChunkCommon],
        metadata=AttributeMetadata(send_mode=SendMode.NO_SEND, copy_mode=CopyMode.COPY),
    )

    widget_factory: Optional[ChunkWidgetFactoryBase] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(ChunkWidgetFactoryBase)),
        metadata=AttributeMetadata(copy_mode=CopyMode.SHARE),
    )

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.children.set_owner(self)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        if isinstance(notice, chunk_notice.AttributeStateChangeNotice) and notice.data.get_owner() == self:
            # If this container holds nested chunks, and the DISABLED state was switched, we should propagate the state
            # to all nested chunks. This is done because the chunk layers are muted/unmuted based on the ENABLED state,
            # and if parent chunks are DISABLED and nested chunks are not, the nested chunk layer will not be muted
            old_state = ChunkFlags.State(notice.data.old_value)
            new_state = ChunkFlags.State(notice.data.value)
            added = new_state ^ (new_state & old_state)
            removed = old_state ^ (new_state & old_state)

            if ChunkFlags.State.DISABLED in (added | removed):

                def _pred(val) -> TypeGuard[ChunkBase]:
                    return isinstance(val, ChunkBase)

                for cc in self.get_params(_pred):
                    if ChunkFlags.State.DISABLED in added:
                        cc.set_state(ChunkFlags.State.DISABLED)
                    else:
                        cc.unset_state(ChunkFlags.State.DISABLED)

        return super().data_changed(notice)

    def remove_param_or_group(self, param: ChunkCommon):
        """
        Remove a child on this group

        Args:
            param (ChunkCommon): The child to remove
        """

        own_p = self.children.pop(param)
        if not own_p:
            return

        own_p.set_owner(None)

    def request_delete(self):
        """
        Request delete this container, and subsequently clear its children
        """

        # Retire from module execution up here, not in the super() call at the end: the child
        # loop below detaches this container's own params, and its PARAM_REMOVED notices can
        # re-enter the DAG solve long before super().request_delete() would set the flag. See
        # ChunkCommon.request_delete for what the flag protects against.
        self._deleting = True

        # Call request_delete on all children. This will send PARAM_REMOVED on the removed
        # children.
        #
        # get_my_params(), not get_params(): the latter walks the whole nested ChainMap, so a
        # context would delete a grandchild param (a sub-chunk's own Menu/Slider) directly,
        # leaving that sub-chunk alive but gutted — get_param() returning None on a chunk that
        # was never asked to delete, and so never marked _deleting. Anything the teardown then
        # cascades into (reconcile_edit_targets re-running an Edit Target, a refresh callback)
        # crashes on the missing param. Deleting only direct children keeps the order right:
        # each child container tears down its own subtree, marking itself first.
        for param in self.get_my_params():
            param.request_delete()

        self.children.clear()
        self.children.set_owner(None)

        super().request_delete()

    @overload
    def get_param(self, param: ChunkCommon | str | HashVal) -> Optional[ChunkCommon]: ...

    @overload
    def get_param(self, param: ChunkCommon | str | HashVal, predicate: Optional[Predicate[ValueT]]) -> ValueT: ...

    def get_param(
        self, param: ChunkCommon | str | HashVal, predicate: Optional[Predicate[ValueT]] = None
    ) -> Optional[ChunkCommon] | ValueT:
        """
        Get a parameter or group inside of this group. A string can be passed to get a parameter by name.
        The string can also be the explicit hierarchical identifier of a param by spacing the hiearchy levels with a dot

        ``` py
        myChunk.get_param("groupA.groupB.param")
        ```

        If just a name is passed, the first found param with said name will be returned. A ChunkGroupBase object enforce that
        it's own child params have unique names. However, the names must not be unique to nested parameters. For this reason,
        multiple params with the same name may exist in a hierarchy.

        A Predicate function can be used to narrow the result type and validates the result:
        ``` py
        p = myChunk.get_param("aSlider", chunk.is_ins(chunk.Slider))
        p.value = 10
        ```

        Args:
            param (ChunkParamBase | str): A parameter to find. If string is passed, look for param with name
            predicate (Optional[Predicate[ValueT]]): A predicate function used to check and narrow the type of the parameter

        Returns:
            Optional[ChunkCommon]: The found parameter or None if not found
            ValueT: The found parameter matching the predicate if supplied

        """

        if isinstance(param, (ChunkCommon, HashVal, ProxyParam)):
            return self.children.get_param(param, predicate)
        elif isinstance(param, str):
            return self.children.get_by_name(param, predicate)

    @overload
    def get_params(self) -> Tuple[ChunkCommon, ...]: ...

    @overload
    def get_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ChunkCommon, ...] | Tuple[ValueT, ...]:
        """Get all parameters in this group including nested

        Args:
            predicate (Optional[Predicate[ValueT]]): An optional predicate function to filter the result. Defaults to None.

        Returns:
            Tuple[ChunkCommon, ...]: All parameters in this group
            Tuple[ValueT, ...]: All parameters in this group matching the predicate, if supplied
        """
        return self.children.get_params(predicate)

    @overload
    def get_my_params(self) -> Tuple[ChunkCommon, ...]: ...

    @overload
    def get_my_params(self, predicate: Optional[Predicate[ValueT]]) -> Tuple[ValueT, ...]: ...

    def get_my_params(
        self, predicate: Optional[Predicate[ValueT]] = None
    ) -> Tuple[ChunkCommon, ...] | Tuple[ValueT, ...]:
        """Get all parameters added directly to this group

        Args:
            predicate (Optional[Predicate[ValueT]]): An optional predicate function to filter the result. Defaults to None.

        Returns:
            Tuple[ChunkCommon, ...]: All parameters owned by this group
            Tuple[ValueT, ...]: All parameters owned by this group matching the predicate, if supplied
        """
        return self.children.get_my_params(predicate)

    def check_hierarchy(self):
        """
        A debug function for checking the hierarchy structure of a group.

        Raises:
            TypeError: An owner is not set to a child of this group
            TypeError: The owner of a child is not a ChunkNoticePropagator
            ValueError: The owner of a child does not itself hold the child
            NameError: Duplicate chunk in the hierarchy
            NameError: Duplicate name in the hierarchy
        """
        failed_msg = f"check_hierarchy for {self.name} failed! \n"
        failed_msg += " Reason: {}"

        found_params: set[ChunkCommon] = set()

        # Names are checked among siblings, not across the whole hierarchy: a param is
        # addressed relative to its owner, so two groups may each hold a "General"
        # without either becoming unreachable. Renderer settings are the case that
        # proves it -- every installed renderer gets a group and every one of them ends
        # up with a General for its leftovers.
        #
        # Keyed by id() rather than by the owner itself, so this never depends on how a
        # param defines equality: two distinct groups that happen to compare equal must
        # still be two scopes.
        found_names: Dict[int, set[str]] = {}

        for p in self.get_params():
            if isinstance(p, ChunkBase):
                # Chunks (inherit from ChunkAttributes) do not need owners
                continue

            owner = p.get_owner()
            if isinstance(owner, NameSyncedContainer):
                # Get the owner of the container, i.e the group or chunk
                owner = owner.get_owner()

            if not owner:
                raise TypeError(
                    failed_msg.format(f"{p.name} does not have an owner. All params except Chunks should have an owner")
                )

            if not isinstance(owner, chunk_notice.ChunkNoticePropagator):
                raise TypeError(failed_msg.format(f"{p.name}'s owner is not a ChunkNoticePropagator object"))

            if isinstance(owner, ChunkContainerBase) and not isinstance(owner, ChunkListGroupBase):
                if p not in [x._param_ref if isinstance(x, ProxyParam) else x for x in owner.get_my_params()]:
                    raise ValueError(
                        failed_msg.format(f"{p.name} has owner: {owner.name}. Owner does not hold this param")
                    )

            if p in found_params:
                raise NameError(failed_msg.format(f"{p.name} is duplicated in the hierarchy"))
            found_params.add(p)

            direct_owner = p.get_owner()
            if isinstance(direct_owner, NameSyncedContainer):
                direct_owner = direct_owner.get_owner()

            siblings = found_names.setdefault(id(direct_owner), set())
            if p.name in siblings:
                owner_name = getattr(direct_owner, "name", "<no owner>")
                raise NameError(
                    failed_msg.format(f"param name: {p.name} is not unique under {owner_name}")
                )
            siblings.add(p.name)

    def print_hierarchy(self):
        """
        A debug function printing the hierarchy of the group and its children
        """

        def inset(i):
            return "." * i * 4

        max_inset = 15

        msg = f"\n{'-' * max_inset}* {self.name} hierarchy *{'-' * max_inset}\n\n"
        for indent, p in self.iter_hierarchy():
            cls_name = f"[{p.__class__.__name__}]"
            if len(cls_name) > max_inset:
                cls_name = f"[{p.__class__.__name__[0 : max_inset - 1]}]"

            offset = "." * (max_inset - len(cls_name))

            msg += f"{cls_name}{offset}{inset(indent)}{p.name}"
            if isinstance(p, ChunkParamBase):
                msg += f" -> {p.evaluate_type().name}"

            owner = p.get_owner()
            if isinstance(owner, NameSyncedContainer):
                # Get the owner of the container, i.e the group or chunk
                owner = owner.get_owner()

            if owner and isinstance(owner, ChunkContainerBase):
                msg += f". Owner = ({owner.name})"

            msg += f". Id = ({hex(id(p))})"
            msg += "\n"

        self.get_logger().info(msg)

    def iter_hierarchy(self, level=0) -> Generator[Tuple[int, ChunkCommon], None, None]:
        """
        Provides a generator for iterating over the hierarchy, starting from self,
        going to all children and recursively through nested groups.

        Args:
            level (int, optional): A value saying what level of the hierarchy the yielded value come from, starting at 0. Defaults to 0.

        Yields:
            Tuple[int, ChunkCommon]:
        """
        yield level, self
        for p in self.get_my_params():
            if isinstance(p, ChunkGroupBase):
                yield from p.iter_hierarchy(level + 1)
            else:
                yield level + 1, p


class ChunkGroupBase(ChunkContainerBase):
    """
    A base class for dynamic group objects. Chunk and Groups are dynamic in the sense that they can add an arbitrarily number of
    children of direfent types. Vectors on the other hand should not subclass from this class as they are not dynamic in the
    same sense.

    """

    def add_params(self, params: Iterable[ChunkT], force_unique: bool = True, clone: bool = False) -> List[ChunkT]:
        """Adds parameters to this group. Will call add_param on all params in the list

        Args:
            params (Iterable[ChunkCommon]): List of chunk objects to add to this gruo
            force_unique (bool, optional): Force unique param names. Defaults to True.
            clone (bool, optional): Clones all params. Defaults to True.

        Returns:
            List[ChunkCommon]: A list of the added parameters. Will differ from the input params if copies were made
        """
        new_params = []
        for param in params:
            if clone:
                p = self.add_param(param.clone(), force_unique)
            else:
                p = self.add_param(param, force_unique)
            new_params.append(p)
        return new_params

    def add_param(self, param: ChunkT, force_unique: bool = True) -> ChunkT:
        """
        Adds a parameter to this group. The new param will be added to the 'children' NameSyncedContainer attribute.
        Any modifications of the param will propagate to this group as they will share the same hierarchy. See the
        ChunkNoticePropagator for more information on how this is implemented. Adding mupliples of the same param,
        consider using:

        ``` py
        group.add_param(param.clone())
        ```
        or, for multiple params at once:
        ``` py
        group.add_params([param, param.clone(), param.clone()])
        ```

        Args:
            param (ChunkT): A parameter to add. May also be another ChunkContainer, which will nest the group
            force_unique (bool): If a param already exists in the hierarchy, we most often do not want a direct duplicate.
                This enables the check, and if a duplicate exists, raises a ValueError. However, if the hash is unique but
                the name is a duplicate, this will resolve the name conflict by incrementing a numerical suffix. If set to False,
                this check is never performed, instead any duplicate will simply replace the old parameter. Defaults to True

        Returns:
            ChunkT: The param

        Raises:
            ValueError: If the param is a direct duplicate (hash exists already)
        """

        new_param = None
        if force_unique:
            new_param = self.resolve_new_param(param)

        if new_param is None or new_param is param:
            new_param = param

        if isinstance(new_param, ChunkCommon):
            # Add the new param to self.children container. The owner/ownee relationship is handled
            # by the container, so should not modify owner here
            self.children.add(new_param)

            if isinstance(new_param, ChunkContainerBase):
                # Also append the children of the group, as this allows for fast lookups accross the layers
                self.children.add_nested(new_param.children)

        else:
            raise ValueError("Chunks only support params inherited from ChunkCommon")
        return new_param

    def resolve_new_param(self, param: ChunkT) -> Optional[ChunkT]:
        """
        Return a copy of a param or group that does not conflict with other child on this group. Raises an error
        if a param with the same hash already exists

        Args:
            param (ChunkParamBase): A ChunkParam or ChunkContainer object to copy

        Returns:
            ChunkT: If the parameter was valid, return the parameter but with posibly a new name as to not fix name conflicts

        Raises:
            ValueError: If a duplicate was found
        """
        if param in self.get_params():
            raise ValueError(f"A duplicate for param {param.name} was found on container {self.name}")

        new_param = param

        if not isinstance(param, ProxyParam):
            reserved_names = self.get_reserved_names()

            new_name = self.get_valid_child_name(param.name, reserved_names)
            new_param.name = new_name

            reserved_names.add(new_name)

            if isinstance(new_param, ChunkGroupBase):
                for p in new_param.get_params():
                    if not isinstance(p, ProxyParam):
                        new_name = self.get_valid_child_name(p.name, reserved_names)
                        reserved_names.add(new_name)
                        p.name = new_name

        return new_param

    def get_reserved_names(self) -> Set[str]:
        """Get a set of reserved names. These are the names of all parameters and groups under this group + this groups name.
        A new parameters name should not exist in the set in order to be valid.

        Returns:
            Set[str]: A set of reserved group and parameter names which new parameters should not be named
        """
        return set(self.children.get_my_names()) | set([self.name])

    @staticmethod
    def get_valid_child_name(orig_name: str, reserved_names: Set[str]) -> str:
        """Get a unique name from orig_name that does not intersect reserved_names by adding a numerical suffix to duplicate names

        Args:
            orig_name (str): The name to make valid
            reserved_names (Set[str]): A list of reserved names. This paramater can't have a name
                which exist in this list. The new, valid name will be appended to this list to nest a group inside an already created group

        Returns:
            str: a unique name
        """

        suffix = ""

        if m := re.match("(.*?)([0-9]+)$", orig_name):
            orig_name = m.group(1)
            suffix = m.group(2)

        idx = int(suffix) if suffix else 1
        max_iter = 1000

        name = orig_name + suffix

        while name in reserved_names:
            name = orig_name + str(idx)
            idx += 1
            if idx > max_iter:
                raise RuntimeError(f"Failed to resolve name, loop exceeded {max_iter} iterations")
        return name


class ChunkListGroupBase(ChunkGroupBase):
    """
    A base group for list style container. In addition to the containers children, the group also hold a list of groups that in turn holds
    proxies to the children. In a ui implementation, the 'children' should be hidden, and instead the proxy_params should be used.
    The proxy parameters are practically identical to their referenced parameters, with the exception of their 'value' attributes. If changing
    the name of the referenced parameter, all proxies will reflect this change and also get a new name. Getting and setting values of the
    proxies can be done similarly to a regular parameter, but access to said proxies should be done through get_indexed_param() rather than
    get_param().

    get_params() and get_my_params() are overridden to return the proxy parameters. get_param() is not. The destinction being that get_param()
    will return from the 'children' container. i.e the container which the proxies references. To modify all proxies simultaneously one can modify
    the referenced parameters with get_param() and similarly to add a parameter to all instances, use add_param().

    Attributes:
        size (int): The size of the list. This can be modified at runtime to extend / shrink the list
        proxy_params (List[ChunkGroupBase]): A list of groups holding the proxy parameters. This is managed internally, and should not be modified
            externally
    """

    size: int = attrs.field(default=0)
    proxy_params: List[ChunkGroupBase] = attrs.field(factory=list, init=False)

    @overload
    def get_indexed_param(self, param: ChunkCommon | str | HashVal, index: int) -> Optional[ChunkCommon]: ...
    @overload
    def get_indexed_param(
        self, param: ChunkCommon | str | HashVal, index: int, predicate: Optional[Predicate[ValueT]] = None
    ) -> ValueT: ...

    def get_indexed_param(
        self, param: ChunkCommon | str | HashVal, index: int, predicate: Optional[Predicate[ValueT]] = None
    ) -> Optional[ChunkCommon] | ValueT:
        """
        Get a param from the proxy list. The index should be an index of the proxy_params list. If found, The returned
        value is a ProxyParam referencing the param input, at index.

        Args:
            param (ChunkCommon | str | HashVal): The param to look for. If a string is supplied, the parameters name will be used for equality check
            index (int): The index in to proxy_params which to get the parameter from
            predicate (Optional[Predicate[ValueT]]): A predicate function to use for filtering the params. Defaults to None

        Returns:
            Optional[ChunkCommon]: The proxy parameter referencing param
            ValueT: The proxy parameter referencing param, matching the predicate if predicate is not None

        Raises:
            IndexError:
                If the index is out of bounds
        """

        if len(self.proxy_params) == 0 and not predicate:
            return

        if index >= len(self.proxy_params):
            raise IndexError("Index out of range")

        res = self.proxy_params[index].get_param(param)
        if not predicate or predicate(res):
            return res

        raise ValueError(f"Predicate {predicate} failed for get_indexed_param with index {index}")

    def post_serial_setattr(self, name, value):
        if name == "proxy_params":
            for p in value:
                p.set_owner(self)
        return super().post_serial_setattr(name, value)

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if old_value is None or attrname != "size":
            return super().value_changed(attrname, old_value, value)

        if value > old_value:
            for i in range(old_value, value):
                # No LABELED: the index string ("0", "1", ...) as a row header serves
                # little purpose and just costs vertical space — the row's own fields
                # already identify it (e.g. a name/label field, per-row). Inherit the
                # list's own layout (e.g. HORIZONTAL) so a list author can lay each row's
                # fields out side by side instead of always stacked.
                group = ChunkGroupBase(str(i), flags=ChunkFlags.GroupFlags.NONE, layout=self.layout)
                group.set_owner(self)
                self.proxy_params.append(group)

                NameSyncedContainer.create_proxy(self.children, group.children)
                self.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, group)

        elif value < old_value and len(self.proxy_params) > value:
            # Pop from the tail inward: popping ascending indices while iterating shifts
            # every later element down one, so proxy_params[i] would run off the end (and
            # delete the wrong rows). Removing the highest index first keeps the lower ones
            # stable. Clamp the top to the actual length in case it's already shorter.
            for i in range(min(old_value, len(self.proxy_params)) - 1, value - 1, -1):
                self.proxy_params[i].request_delete()
                self.proxy_params.pop(i)

        if value != old_value:
            for i, grp in enumerate(self.proxy_params):
                grp.name = str(i)

        return super().value_changed(attrname, old_value, value)

    @overload
    def get_params(self) -> Tuple[ChunkCommon, ...]: ...

    @overload
    def get_params(self, predicate: Optional[Predicate[ValueT]] = None) -> Tuple[ValueT, ...]: ...

    def get_params(self, predicate: Optional[Predicate[ValueT]] = None):
        res = []
        for container in self.proxy_params:
            res.extend(container.get_params(predicate))
        return tuple(res)

    def get_my_params(self, *args, **kwargs) -> Tuple[ChunkGroupBase, ...]:
        return tuple(self.proxy_params)

    def remove_index(self, index: int):
        """
        Remove an index from the proxy_params list. This will decrament the 'size' attribute by 1, and
        call request_delete() on the group at index. If The index is out of range, then print the error
        and return

        Args:
            index (int): The index to remove
        """
        try:
            param = self.proxy_params[index]
            param.get_dag_node().disconnect()

            param.request_delete()
            self.proxy_params.pop(index)

        except IndexError as e:
            self.get_logger().warning("Failed to remove index from list. Index out of range %s", e)
            return

        self.size -= 1

    def add_param(self, param: ChunkT, force_unique: bool = True) -> ChunkT:
        param = super().add_param(param, force_unique)

        for proxy_group in self.proxy_params:
            proxy_group.add_param(ProxyParam(param))  # type: ignore
        return param


class ChunkBase(ChunkGroupBase):
    """
    Base class for Chunks. Similar to a group but meant to be the top level owner of other params.
    """

    # The config is set from the chunk_registry.ChunkModule.load_config() function.
    # This is because the config can be loaded independently from the chunk, without
    # having to execute main() of the chunk module.
    _config: Optional["ChunkConfig"] = attrs.field(default=None, init=False, metadata=AttributeMetadata(serialize=True))

    # For CONTEXT chunks: the USD prim path this context maps to. Chunks within
    # the context are placed at {prim_path}/chunks/{chunk_name}. Falls back to
    # _CHUNKS_ROOT when None.
    prim_path: Optional[str] = attrs.field(default=None)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.add_callback(
            self.disable,
            notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE,
            send_on_create=False,
            private=True,
        )
        self.add_callback(
            self.enable,
            notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE,
            send_on_create=False,
            private=True,
        )
        self.add_callback(
            self.remove,
            notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE,
            send_on_create=False,
            private=True,
        )
        self.add_callback(
            self.trigger_on_create_callbacks,
            notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED,
            send_on_create=False,
            private=True,
        )

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        # Strip spaces so prim_path stays a valid USD path, but leave None alone — otherwise
        # str(None) makes prim_path the string "None", which is truthy and Sdf.Path("None")
        # collapses to an invalid empty path (breaks chunkgroups / _resolve_chunk_root).
        if attr.name == "prim_path" and val is not None:
            return str(val).replace(" ", "")
        return super().convert_attr(attr, val)

    def start_gizmo(self, notice=None, sender=None):
        self.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_START)

    def stop_gizmo(self, notice=None, sender=None):
        self.send_self(chunk_notice.ChunkNoticeAlias.GIZMO_STOP)

    def disable(self, notice=None, sender=None):
        self.set_state(ChunkFlags.State.DISABLED)

    def enable(self, notice=None, sender=None):
        self.unset_state(ChunkFlags.State.DISABLED)

    def remove(self, notice=None, sender=None):
        self.request_delete()

    def trigger_on_create_callbacks(self, notice=None, sender=None):
        """
        Call trigger() on all callbacks in the container from get_on_create_callbacks(). This function should be called once the chunk is loaded
        properly and added to the scene. This is a callback that is called when the chunk is loaded ( CHUNK_LOADED is sent ).
        """
        for _, child in self.iter_hierarchy():
            for cb in child.get_on_create_callbacks():
                if cb.alias == chunk_notice.ChunkNoticeAlias.CHUNK_LOADED:
                    continue
                cb.trigger()

    def get_config(self) -> Optional["ChunkConfig"]:
        return self._config

    def get_edit_context(self) -> "Sdf.Layer | None":
        if self._config and self._config.interactive:
            return None
        return super().get_edit_context()

    def get_chunk(self) -> Optional["ChunkBase"]:
        return self

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CHANGED

    def add_param(self, param: ChunkT, force_unique: bool = True, notify: bool = True) -> ChunkT:
        do_context_setup = notify and self.has_flag(ChunkFlags.GroupFlags.CONTEXT) and isinstance(param, ChunkBase)
        if do_context_setup:
            # Clear the chunk's edit layer to a fresh slate BEFORE adding — adding fires
            # PARAM_ADDED, which can trigger the chunk's execute() (e.g. via _wire_inputs);
            # clearing afterwards would wipe what execute just authored.
            lm = chunk_registry.ChunkLayerManager()
            if lm.initialized:
                if layer := lm.get_or_create_layer(param):
                    layer.Clear()
        res = super().add_param(param, force_unique)
        if do_context_setup:
            chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, param, data=param)
        return res

    def get_context(self) -> Optional["ChunkBase"]:
        owner = self
        while owner:
            if isinstance(owner, ChunkBase) and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                return owner
            owner = owner.get_owner()
        return None


class ChunkVectorBase(ChunkContainerBase, ChunkUsdAttributeSupport):
    """
    Base for vector style parameters. Function similar to a group but with fixed number of params. Interfacing
    with a Vector is similar to a regular valued parameter, where the 'value' attribute reference child params.
    These ops can be used:

    ``` py
    vec = chunk.Vector(
        "myVec", param=chunk.Slider(), length=3, suffix="xyz"
    )

    vec[0] = 10
    vec.x = 10
    vec.value = (0, 1, 2)
    ```

    Attributes:
        param (ChunkParamBase): The parameter to use as for the vector. Will clone it lenght times
        length (int): How many elements in the vector
        suffixes (Optional[Tuple[str, ...] | str]): A string or tuple of strings where len(suffixes) == length. These decide how the
            parameters should be nammed. If the param has a name already, the suffixes will be appended to the name
    """

    param: ChunkParamBase = attrs.field(default=None, repr=False, metadata=AttributeMetadata(serialize=False))
    length: int = attrs.field(default=0)
    suffixes: Optional[Sequence[str] | str] = attrs.field(default=None)
    _default_value: Optional[Sequence[Any]] = attrs.field(default=None, alias="default_value")

    # A Vector's own .flags is GroupFlags-typed (needed for widget construction — see
    # has_flag override below) and can't also carry ChunkFlags.ParamFlags.OUTPUT: GroupFlags
    # and ParamFlags are separate IntFlag classes with overlapping bit values, and has_flag()
    # does a strict same-class check, so combining them via `|` makes OUTPUT silently
    # undetectable (or, the other way round, makes GroupFlags checks crash). is_output is a
    # dedicated field so a Vector can be a computed output without touching .flags at all.
    is_output: bool = attrs.field(default=False)

    # Raw @Node.vecParam@ expression string driving this vector's value, when set.
    # Whole-vector references only — the referenced param must itself resolve to a
    # same-length sequence. None means the vector's value comes from its own children.
    _expr_value: Optional[str] = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )
    _expression_connections: list = attrs.field(
        default=attrs.Factory(list), init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

    # Set by _set_children_blocked while assigning children one at a time, so
    # data_changed can skip writing a (still partial) tuple to the storage attr
    # without suppressing each child's own notify (which widgets rely on to refresh).
    _suppress_storage_write: bool = attrs.field(
        default=False, init=False, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "suffixes" and val is not None:
            if len(val) < self.length:
                raise ValueError(
                    "Vector suffixes length is smaller than vector length. Set either as 'XYZ' or ('X', 'Y', 'Z') for a 3 length vector"
                )
        return super().validate_attr(attr, val)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._attr_senders["value"] = AttributeSender(self, "value", chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED)

        if not self.children.empty() or not self.param:
            # __attrs_post_init__ was called when copied, so no need to reinitialize the children as they
            # have already been initialized
            return

        if self.length and self._default_value and (self.length != len(self._default_value)):
            raise ValueError(f"Length and default_value of vector {self.name} does not match")

        if not self.length and self._default_value:
            self.length = len(self._default_value)
        elif not self.length and self.suffixes:
            self.length = len(self.suffixes)

        if not self.suffixes:
            self.suffixes = "abcdefghijklmnopqrstuvwxyz"[: self.length]

        for i in range(self.length):
            # Clone the self.param length times, and add the clones to the clildren container
            np = self.param.clone()

            if self.suffixes:
                np.name += self.suffixes[i]

            if self.is_output:
                # Children are plain scalar params (Slider etc.) — no GroupFlags/ParamFlags
                # collision for them, unlike the vector itself, so OUTPUT can go straight
                # on .flags to get the normal widget.set_enabled(False) treatment.
                np.set_flag(ChunkFlags.ParamFlags.OUTPUT)

            self.children.add(np)

        if self._default_value:
            self.default_value = self._default_value

    def __getitem__(self, key: str | int) -> ChunkParamBase:
        """Allows for Vector[0] or Vector['paramName'] access to the vector values

        Args:
            key (str | int): name or index of the requested value parameter

        Raises:
            KeyError: if key is not accessible

        Returns:
            Optional[ChunkCommon]: Param if found
        """

        if isinstance(key, str):
            param = self.get_param(key)
        elif isinstance(key, int):
            param = self.get_my_params()[key]
        if not param or not isinstance(param, ChunkParamBase):
            raise KeyError(f"Index {key} does not exist on vector")
        return param

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        my_notice = super().data_changed(notice)

        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if (
            notice
            and stage
            and isinstance(notice.data, AttributeSender)
            and notice.data.name == "value"
            and not self.is_blocked()
        ):
            # notice.data is typically a *child's* AttributeSender (bubbled up through
            # the DAG), whose .value is a single scalar — not usable here. This vector's
            # own self.value (tuple, already expression-aware) is what must be written.
            raw = self.raw_value
            is_expr = isinstance(raw, str) and chunk_utils.has_expression(raw)
            val = self.value
            # _set_children_blocked sets this while assigning each child in turn, to
            # avoid writing a partial tuple to the storage attr on the first (still
            # mid-assignment) bubble — that write would otherwise be the one that lazily
            # authors the attr and never gets corrected afterward. Notices still need to
            # propagate normally during the loop (this doesn't use is_blocked()) so each
            # child's own widget refreshes live as it's set.
            if val is not None and not self._suppress_storage_write:
                chunk_notice._usd_write_active = True
                try:
                    if self._param_attr_path and (usd_attr := stage.GetAttributeAtPath(Sdf.Path(self._param_attr_path))):
                        if not is_expr:
                            current = usd_attr.Get(0)
                            if current != val:
                                usd_attr.Set(val)
                        else:
                            chunk_root = str(usd_attr.GetPrim().GetParent().GetPath())
                            usd_attr.SetConnections([
                                Sdf.Path(
                                    f"{chunk_root}/{node}.outputs:{attr_name}"
                                    if stage.GetPrimAtPath(Sdf.Path(f"{chunk_root}/{node}")).GetAttribute(f"outputs:{attr_name}").IsValid()
                                    else f"{chunk_root}/{node}.inputs:{attr_name}"
                                )
                                for node, attr_name in chunk_utils.parse_expression(raw)
                            ])
                    self._write_driven_attrs(stage, val)
                finally:
                    chunk_notice._usd_write_active = False

            # Forward on this vector's own "value" sender so anything depending on the
            # whole vector (e.g. a downstream @Node.vecParam@ expression) reacts too.
            chunk_notice.send_notice(notice.data.get_notice_alias(), self.get_attribute_sender("value"), self.get_usd_prop())

        return my_notice

    def __setitem__(self, key: str | int, val: Any) -> Optional[ChunkParamBase]:
        param = self[key]
        if not isinstance(param, ChunkParamBase):
            raise ValueError("ChunkContainers does not support value assignment")

        param.value = val

    @property
    def raw_value(self) -> Any:
        """The stored expression string, or the tuple of children's raw values."""
        if self._expr_value is not None:
            return self._expr_value
        return tuple(x.raw_value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @property
    def value(self):
        if self._expr_value is not None and chunk_utils.has_expression(self._expr_value):
            return self._resolve_expression_tuple(self._expr_value)
        return tuple(x.value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @value.setter
    def value(self, val: Union[Sequence[Any], str]):
        if isinstance(val, str) and chunk_utils.has_expression(val):
            self._expr_value = val
            self._expression_connections = []
            for node_name, param_name in chunk_utils.parse_expression(val):
                ref = self._resolve_expression_ref(node_name, param_name)
                if ref is None:
                    logger.warning(f"Expression ref @{node_name}.{param_name}@ not found")
                    continue
                conn = chunk_notice.NoticeConnection(
                    method=self._on_ref_changed,
                    alias=chunk_notice.ChunkNoticeAlias.VALUE_CHANGED,
                    sender=ref.get_attribute_sender("value"),
                )
                self._expression_connections.append(conn)
            self._push_resolved_children()
            return

        self._expr_value = None
        self._expression_connections = []
        if not len(val) == self.length:
            raise ValueError(
                f"Trying to set value {val} on vector param {self.name} with wring size. Size should be {self.length}"
            )

        self._set_children_blocked(val)

    def _push_resolved_children(self) -> None:
        """Push the resolved tuple down into child params so their own widgets display
        real numbers rather than the parent's expression string."""
        resolved = self.value
        if resolved is None or len(resolved) != self.length:
            return
        self._set_children_blocked(resolved)

    def _set_children_blocked(self, val: Sequence[Any]) -> None:
        """Set every child from val, suppressing this vector's own storage-attr write
        until all children are set (data_changed checks _suppress_storage_write).
        Without this, the first (still mid-assignment) child change would be the one
        that lazily authors the chunk-prim storage attr, permanently baking in a
        stale value with only some components set.

        Deliberately does NOT use set_blocked(), which is transitive to children via
        their owner-chain is_blocked() check — that would also suppress each child's
        own notify, which their widgets rely on to refresh live as they're set."""
        self._suppress_storage_write = True
        try:
            for i in range(self.length):
                self[i] = val[i]
        finally:
            self._suppress_storage_write = False
        self.get_attribute_sender("value").execute()

    def _on_ref_changed(self, notice=None, sender=None):
        self._push_resolved_children()

    def _resolve_expression_tuple(self, expr: str) -> tuple:
        tokens = chunk_utils.parse_expression(expr)
        if len(tokens) == 1 and expr.strip() == f"@{tokens[0][0]}.{tokens[0][1]}@":
            ref = self._resolve_expression_ref(*tokens[0])
            if ref is not None:
                ref_val = ref.value
                if isinstance(ref_val, (tuple, list)) and len(ref_val) == self.length:
                    return tuple(ref_val)
        return self.default_value

    @property
    def default_value(self):
        return tuple(x.default_value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @default_value.setter
    def default_value(self, val: Sequence[Any]):
        if val is None:
            return

        if not len(val) == self.length:
            raise ValueError(
                f"Trying to set value {val} on vector param {self.name} with wring size. Size should be {self.length}"
            )

        for i in range(self.length):
            self[i].default_value = val[i]

    def get_type(self):
        return ChunkFlags.ParamType.VECTOR | self.param.get_type()

    def evaluate_type(self):
        return ChunkFlags.ParamType.VECTOR | self.param.evaluate_type()

    def has_flag(self, flag: IntFlag) -> bool:
        """ChunkFlags.ParamFlags.OUTPUT is tracked via is_output instead of .flags — see
        the field's docstring. Everything else defers to the normal GroupFlags check."""
        if flag == ChunkFlags.ParamFlags.OUTPUT:
            return self.is_output
        return super().has_flag(flag)


class ChunkConfig(AttributesBase):
    """
    Base class for Configs. A config sets some load time configurations for a chunk.
    """

    category: str = attrs.field(default="Chunks")
    description: str = attrs.field(default="Chunk description...")
    load_for_plugin: Optional[str] = attrs.field(default=None)
    requirements: list[str] = attrs.field(factory=list)
    required_modules: list[str] = attrs.field(factory=list)
    module: str = attrs.field(default="")
    interactive: bool = attrs.field(default=False)
    terminal: bool = attrs.field(default=False)  # no outputs:chunk authored; chunk terminates the exec chain
    outputs: list[tuple[str, str]] = attrs.field(factory=list)  # extra typed output ports: [(name, SdfTypeName), ...]
    variants: list[tuple[str, dict]] = attrs.field(factory=list)  # extra menu entries: [(label, {param: value}), ...]

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CONFIG_CHANGED


class SceneSender(chunk_notice.ChunkNoticePropagator):
    """
    Abstract class for implementing scene access for different DCC's. Should have access to the UsdStage and send notices for scene changes.
    See implementation in plugins/usdview/__init__.py for an example implementation.

    The class is not purely abstract, as most functions dealing with chunks are implemented. However, a subclass should implement all relevant
    @abstractmethod functions.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.set_chunks(NameSyncedContainer())

        self._modified = False
        self._scene_file = Path()
        self._current_context: Optional[weakref.ReferenceType[ChunkBase]] = None
        self._selected_chunks: weakref.WeakSet[ChunkBase] = weakref.WeakSet()
        self._stage_replaced_listeners: list = []

    def add_stage_replaced_listener(self, callback) -> None:
        """Register a callback invoked when the DCC opens a new stage.
        Called by chunk_usd.initialize so that reload_for_stage runs on file open."""
        self._stage_replaced_listeners.append(callback)

    def notify_stage_replaced(self, stage) -> None:
        """DCC implementations call this once a new stage is fully loaded."""
        for cb in self._stage_replaced_listeners:
            cb(stage)

    def serialize(self, glb):
        # Ensure that the scene is not serialized. For now, we only serialize the chunks NameSyncedContainer
        return None

    def export_scene(self, path: str | Path):
        """
        Export the current scene. The result will be written to path, if path is a chunk_serial.FileExtensions.SCENE file path.
        The file will contain information about all the loaded chunks and modules, and when loaded
        should recrete the scene 1:1

        Args:
            path (str | Path): A target file path which to write to
        """
        p = Path(path)
        if p.suffix != chunk_serial.FileExtensions.SCENE:
            return

        self.set_scene_file(path)

        layers = {}
        layer_data = []
        lreg = chunk_registry.ChunkLayerManager()

        if lreg.initialized:
            for c in self.get_chunks():
                if layer := lreg.get_layer(c):
                    lname = lreg.get_layer_file_name(layer)
                    layers[lname] = c.get_hash()
                    layer_data.append((lname, lreg.get_layer_usda(layer)))

        data = chunk_serial.Scene(
            modules=chunk_registry.ChunkRegistry().get_chunk_modules(True), chunks=self.chunks, layers=layers
        )
        chunk_serial.save_scene(path, data, layer_data)

    def save_scene(self):
        """
        Calls export_scene with the current scene file as target, meaning this will override the currently loaded scene.
        If no scene is set, this will print a warning and do nothing.
        """
        if not (f := self.get_scene_file()):
            logger.warning("No scene file set")
            return

        self.export_scene(f)
        self._modified = False

    def get_scene_file(self):
        if self._scene_file == Path():
            return None
        return self._scene_file

    def set_scene_file(self, path: str | Path):
        """
        Set the current scene file. The file extension of path should be chunk_serial.FileExtensions.SCENE. This will not
        load the content of the file. To load the content of the file, use open_scene() instead.

        Args:
            path (str | Path): A path to a scene file.
        """
        if (p := Path(path)) and p.suffix != chunk_serial.FileExtensions.SCENE:
            logger.warning(f"Unsupported scene file extension: {path}")
            return

        self._scene_file = Path(path)

    def open_scene(self, path: str | Path):
        """
        Open a scene file (.chsc) and import its content. The chunks member will be replaced with the chunks
        imported from the file. All layers will be replaced.

        Args:
            path (str | Path): _description_
        """
        p = Path(path)
        if not p.exists() or p.suffix != chunk_serial.FileExtensions.SCENE:
            return

        if self.is_modified():
            if not self.request_save():
                return

        # Clear the current scene
        self.clear()

        # Deserialize json files
        serial = chunk_serial.load_file(path)
        if not serial or not (data := chunk_serial.deserialize_scene(serial)):
            return

        self.set_scene_file(p)

        # Construct the modules
        for mod_cnstr in data.get("modules", []):
            mod = mod_cnstr.construct(False)

            if not isinstance(mod, chunk_registry.ChunkModule):
                logger.error(f"Return value from serial is not a ChunkModule: {mod}")
                continue

            if not mod._pre_load():
                logger.error(f"Failed to preload: {mod}")
                continue

            mod = mod_cnstr.post_constuct()
            chunk_registry.ChunkRegistry().register_module(mod)

        # Construct the chunks
        if chunks := data.get("chunks"):
            if nsc := chunks.construct(False):
                nsc: NameSyncedContainer
                nsc.get_dag_node()._part_of_graph = False
                chunks.post_constuct()

                self.set_chunks(nsc)

        self._modified = False

        # Construct the layers
        lreg = chunk_registry.ChunkLayerManager()
        if not lreg.initialized:
            return

        for layer_name, cref in data.get("layers", {}).items():
            c = cref.construct()

            if not isinstance(c, ChunkCommon):
                logger.warning(f"Could not load layer {layer_name}. The referenced chunk was not found")
                continue

            try:
                layer_str = chunk_serial.load_scene_data(p, layer_name)
            except Exception:
                continue

            if not layer_str:
                logger.warning(f"Could not load layer {layer_name}")
                continue

            layer = Sdf.Layer.CreateAnonymous()
            layer.ImportFromString(layer_str.decode())

            c_layer = lreg.get_or_create_layer(c)
            if not c_layer:
                logger.warning(f"Could not load layer for chunk {c.name}")
                continue
            lreg.stitch_layers(c_layer, layer)

    def remove_chunk(self, chunk: str | ChunkHash):
        """
        Unload this module by deleting the associated chunk object
        """

        if not (c := self.get_chunk(chunk)):
            return

        c.request_delete()
        self._modified = True

    def add_context(self, name: str, prim_path: Optional[str] = None) -> ChunkBase | None:
        """
        Adds a context chunk to the scene. Concrete chunks can be added to the context using add_param and similar. The returned
        ChunkBase object is just a ChunkBase with the CONTEXT flag. This can be used as a organisatorial chunk that contains other chunks
        under a shared layer

        Args:
            name (str): The name of the context
            prim_path (str, optional): USD prim path this context maps to. Chunks within it are
                placed at ``{prim_path}/chunks/{chunk_name}``. Defaults to the global /chunks root.

        Returns:
            ChunkBase | None: The context chunk that was added to the scene
        """
        path = prim_path if prim_path else "/" + name
        return self.add_chunk(ChunkBase(name, flags=ChunkFlags.GroupFlags.CONTEXT, prim_path=path))

    def set_chunks(self, chunks: NameSyncedContainer[ChunkBase]):
        """
        Sets the container storing all the chunks. Will override all currently loaded chunks

        Args:
            chunks (NameSyncedContainer[ChunkBase]): The container
        """
        self.chunks = chunks
        self.chunks.get_dag_node()._part_of_graph = False
        self.get_dag_node()._part_of_graph = False
        self.chunks.set_owner(self)

        for c in self.chunks.get_my_params():
            try:
                c.check_hierarchy()
            except Exception as e:
                logger.exception(e)
                c.print_hierarchy()
                continue

            # TODO: The chunk may have been loaded before, so dumb to send CHUNK_LOADED here perhaps? CHUNK_LOADED should
            # just be called when a new chunk instance is created, but after initialization
            chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, c, data=c)

            if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                for i in c.get_my_params(is_inst(ChunkBase)):
                    chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, i, data=i)

        self._modified = True

    def add_chunk(self, c: ChunkBase, notify: bool = True) -> ChunkBase | None:
        """
        Adds a chunk to the root of the scene.

        Args:
            c (ChunkBase): The chunk to add. Ideally this should be a context chunk, and nested chunks should live underneath
            notify (bool): Whether to fire CHUNK_LOADED after adding. Pass False to defer notification (e.g. to apply stored values first).

        Returns:
            ChunkBase | None: The added chunk
        """

        names = self.chunks.get_my_names()
        if c.name in names:
            c.name = c.get_valid_child_name(c.name, set(names))
        try:
            c.check_hierarchy()
        except Exception as e:
            logger.exception(e)
            return

        if c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            c._parent_context = self._current_context
            self._current_context = weakref.ref(c)
            # User-created group chunks (have a module config) need PARAM_ADDED to fire
            # so ChunkStageSync can create their USD prim. Tab contexts skip this.
            send_added = bool(c.get_config() and c.get_config().module)
            self.chunks.add(c, send_added)
            if send_added:
                # Restore parent context so subsequent chunks don't fall into the group.
                self._current_context = c._parent_context
            if notify:
                chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_LOADED, c, data=c)

        elif self._current_context and (ctx := self._current_context()):
            ctx.add_param(c, notify=notify)
        # The default catch-all context must map to the global chunks root ("/chunks",
        # i.e. _CHUNKS_ROOT in the plugins layer) so chunks with no tab land there. Without
        # an explicit path, add_context would give it "/Chunks" and break _resolve_chunk_root.
        elif ctx := self.add_context("Chunks", prim_path="/chunks"):
            ctx.add_param(c, notify=notify)
        else:
            logger.error(f"Failed to add chunk {c}")
            return

        self._modified = True
        return c

    def add_chunks(self, chunks: Sequence[ChunkBase]):
        """
        Add all chunks in the sequence

        Args:
            chunks (Sequence[ChunkBase]): The chunks to add to the scene
        """
        for c in chunks:
            self.add_chunk(c)

    def clear(self):
        """
        Clear the chunk container. This may not delete the chunks if string references to the chunks
        are maintained elsewhere
        """
        self.chunks.clear()
        self._modified = True

    def holds_chunk(self, chunk: str | ChunkHash) -> bool:
        """
        Check if the scene holds a chunk with name or hash

        Args:
            chunk (str | ChunkHash): The name or hash of the chunk to find

        Returns:
            bool: True if the scene holds the chunk, False otherwise
        """
        return self.get_chunk(chunk) is not None

    def get_chunk(self, chunk: str | ChunkHash) -> ChunkBase | None:
        """
        Return a single chunk held by the scene. If no chunk is found, None is returned.
        Contrary to get_chunks, this may return context chunks as well

        Args:
            chunk (str | ChunkHash): The name or hash of the chunk to find

        Returns:
            ChunkBase | None: The found chunk or None
        """
        res = self.chunks.get_param(chunk)
        return res if isinstance(res, ChunkBase) else None

    def get_context_for_prim_path(self, path: str) -> "ChunkBase | None":
        """Return the CONTEXT chunk whose prim_path matches path, or None."""
        for c in self.get_chunks():
            if isinstance(c, ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
                if getattr(c, "prim_path", None) == path:
                    return c
        return None

    def get_chunks(self) -> Tuple[ChunkBase, ...]:
        seen: set = set()
        res: list = []
        stack: list = list(self.chunks.get_my_params())
        while stack:
            i = stack.pop()
            if not isinstance(i, ChunkBase) or i in seen:
                continue
            seen.add(i)
            cfg = i.get_config()
            is_group_chunk = i.has_flag(ChunkFlags.GroupFlags.CONTEXT) and cfg and cfg.module
            if i.has_flag(ChunkFlags.GroupFlags.CONTEXT) and not is_group_chunk:
                # Plain tab context: don't include it, but visit its children.
                stack.extend(i.get_my_params())
            else:
                res.append(i)
                if is_group_chunk:
                    # ChunkGroup: include it AND recurse so sub-chunks are reachable.
                    stack.extend(i.get_my_params())
        return tuple(res)

    def clear_chunk_selection(self):
        """
        Clear the chunk selection
        """
        for i in self.get_selected_chunks():
            i.set_selected(False)

    def get_selected_chunks(self) -> Tuple[ChunkBase, ...]:
        """
        get the currently selected chunks

        Returns:
            Tuple[ChunkBase, ...]: The selected chunks
        """
        return tuple(self._selected_chunks)

    def set_selected_chunks(self, selection: Sequence[ChunkBase]):
        """
        Deselects the currently selected chunks and sets the selection to the sequence of
        chunks passed as selection

        Args:
            selection (Sequence[ChunkBase]): The new selection to set
        """
        old_selection = self.get_selected_chunks()
        for c in old_selection:
            if c not in selection:
                c.set_selected(False)

        for c in selection:
            if c not in old_selection:
                c.set_selected(True)

    def is_modified(self) -> bool:
        """
        Returns whether the scene has been modified since created

        Returns:
            bool: True if the scene has been modified, False otherwise
        """
        return self._modified

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        if (
            notice
            and chunk_notice.is_notice_type(notice, chunk_notice.ChunkNoticeAlias.STATE_CHANGED)
            and isinstance(notice.data.get_owner(), ChunkBase)
        ):
            # Toggle the selected chunk in self._selected_chunks
            old_state = ChunkFlags.State(notice.data.old_value)
            new_state = ChunkFlags.State(notice.data.value)
            added = new_state ^ (new_state & old_state)
            removed = old_state ^ (new_state & old_state)
            if ChunkFlags.State.SELECTED in (added | removed):
                self._selected_chunks ^= set((notice.data.get_owner(),))
                # Return early to skip changing self._modified
                return super().data_changed(notice)

        self._modified = True
        return super().data_changed(notice)

    def send_self(
        self, alias: chunk_notice.ChunkNoticeAlias | None = None, data: Any = None
    ) -> chunk_notice.ChunkNotice | None:
        self._modified = True
        return super().send_self(alias, data)

    @abstractmethod
    def should_install_package(self, package_name: str) -> bool:
        """
        When a modules config has requirements to external pip packages, the packages may be installed automatically by the ChunkDependencyManager.
        Before any package is installed though, the user should have the option to opt out of this. This method is called when packages can be installed,
        and should return True if the package may be installed. Any installed package is put in the

        Args:
            package_name (str): The name of the package to install

        Returns:
            bool: If the package can be installed
        """
        return False

    @abstractmethod
    def request_save(self):
        """
        Request a save of the scene. Should be implemented by a plugin

        Raises:
            NotImplementedError: If not implemented by a plugin
        """
        raise NotImplementedError

    @abstractmethod
    def get_stage(self) -> Usd.Stage | None:
        """
        Get the current scene stage. Should be implemented by a plugin

        Returns:
            Usd.Stage | None: The stage if available
        """
        return None

    @abstractmethod
    def get_selected_prims(self) -> Sequence[Usd.Prim]:
        """
        Get the selected prims in the scene. Should be implemented by a plugin

        Returns:
            Sequence[Usd.Prim]: Selected prims
        """
        return []

    @abstractmethod
    def get_selected_property(self) -> Usd.Property | None:
        """
        Get the selected property in the scene. Should be implemented by a plugin

        Returns:
            Usd.Property: Selected property
        """
        return None

    @abstractmethod
    def get_active_camera(self) -> Gf.Camera | None:
        """Return the current viewport camera as a Gf.Camera, or None if unsupported."""
        return None

    @abstractmethod
    def get_current_frame(self) -> float:
        """
        Get the current

        Returns:
            float: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def get_end_frame(self) -> float:
        """
        Get the current end frame

        Returns:
            float: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def get_start_frame(self) -> float:
        """
        Get the current start frame

        Returns:
            float: _description_
        """
        raise NotImplementedError

    @abstractmethod
    def set_current_frame(self, frame: float):
        """
        In the plugin implementation, set the current frame

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    @abstractmethod
    def set_end_frame(self, frame: float):
        """
        In the plugin implementation, set the end frame

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    @abstractmethod
    def set_start_frame(self, frame: float):
        """
        In the plugin implementation, set the start frame

        Args:
            frame (float): The frame to set
        """
        raise NotImplementedError

    def set_viewport_camera(self, prim: UsdGeom.Camera) -> None:
        """Set the viewport to look through the given camera prim."""
        pass

    def primSelectionChangedCallback(self, *args, **kwargs):
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.PRIM_SELECTION_CHANGED, sender=self, data=self.get_selected_prims()
        )

    def propSelectionChangedCallback(self, *args, **kwargs):
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.PROP_SELECTION_CHANGED, sender=self, data=self.get_selected_property()
        )

    def activeCameraChangedCallback(self, *args, **kwargs):
        chunk_notice.send_notice(
            chunk_notice.ChunkNoticeAlias.CAMERA_CHANGED, sender=self, data=self.get_active_camera()
        )

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.NONE

    # Optional DCC capability with a no-op default so hosts that don't support
    # it still work; usdview overrides it (used by the RBD solver after baking).
    def refresh_timeline(self, start: float, end: float) -> None:
        """Update the host timeline range."""


def is_inst(cls: type[ValueT]) -> Predicate[ValueT]:
    """
    Created a predicate function which can be used for type narrowing the input parameter v

    Args:
        cls (type[ValueT]): The type which to check

    Returns:
        Predicate[ValueT]: The predicate function
    """

    def _fn(v: Any) -> TypeGuard[ValueT]:
        return isinstance(v, cls)

    return _fn
