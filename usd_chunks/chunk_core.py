"""
Defines all base classes which the chunks and associated classes should derive from. These classes should not
be instatiated, instead use the implementations from usd_chunks.chunk.

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
from typing import (
    Callable,
    List,
    Set,
    Any,
    Dict,
    Protocol,
    Tuple,
    TypeVar,
    Optional,
    Union,
    Iterable,
    Generic,
    TypedDict,
    Generator,
    get_origin,
)

from typing_extensions import dataclass_transform, Self
from abc import abstractmethod
import attrs

from attr import _make as attrMake
from collections import ChainMap
from enum import IntEnum, IntFlag

import re
import uuid
import inspect

from usd_chunks import chunk_notice, chunk_registry, ATTRS_VERSION
from usd_chunks.chunk_flags import ChunkFlags
import weakref

import os
from pxr import Tf, Usd, Sdf

_ATTRS_SUPPORT_CONVERTER = ATTRS_VERSION >= (24, 1)  # type: ignore
_ATTRS_SUPPORT_INSPECT = ATTRS_VERSION >= (25, 4)  # type: ignore

_T = TypeVar("_T")
ValueT = TypeVar("ValueT")
ParamT = TypeVar("ParamT", bound="ChunkParamBase")
GroupT = TypeVar("GroupT", bound="ChunkContainerBase")
ChunkT = TypeVar("ChunkT", bound="ChunkCommon")

################################
# BASE CLASSES FOR UI ELEMENTS #
################################


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
            self.handle_param_changed, param, notice_alias=chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED
        )

    @property
    def param(self) -> ParamT:
        if param := self._param():
            return param
        raise ReferenceError("reference to param is dead! :'(")

    def handle_param_changed(self, notice: chunk_notice.ChunkNotice, sender: "ChunkParamBase"):
        """
        callback for handling changes to the parameter. Currently implements changes to
        value, name, label and icon attributes

        Args:
            notice (chunk_notice.ChunkNotice): The notice sent for the change
            sender (ChunkParamBase): The sender
        """
        attrname = notice.data.name
        if attrname == "value":
            self._blocked = True
            self.set_value(notice.data.value)

        elif attrname in ["name", "label"]:
            self.param.get_label()

        elif attrname == "icon" and sender.icon:  #
            self.set_icon(sender.icon)

    def setup_chunk_widget(self):
        """
        Called when initializing the widget. Sets the value and label on this object to represent the values of the chunk object
        """

        value = self.param.value
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

    def value_changed(self, value: Optional[Any] = None):
        """
        Called whenever the value of the widget changed, and forwards the new value to self.set_value.
        This will also update param.value

        Args:
            value (Optional[Any], optional): The new value set on the widget. Defaults to None.
        """

        blocked = self._blocked
        self._blocked = False
        if blocked:
            return

        if self.param is not None:
            if value is None:
                value = self.get_value()
            try:
                self.param.value = value
            except ValueError:
                self.set_value(self.param.value)


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

    def get_map(self):
        """
        Return the widget_map of the factory

        Returns:
            ict[ChunkFlags.ParamType, type[ChunkParamWidget]]: The widget_map
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

        self._group = weakref.ref(group)

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

        # Associate ChunkParamGroup objects with group widgets
        self.group_widgets: dict[ChunkHash, ChunkContainerWidgetBase] = {}

        # Container for all widgets in this chunk. Dict will map between ChunkParam -> widget
        # for each widget created from the same ChunkParam instance
        self.param_widgets: dict[ChunkHash, ChunkParamWidgetBase] = {}

        # Initialize everything
        self.factory = self.get_widget_factory()
        self.build_collapsable()
        self._build_param_widgets(self.group)
        self.update_from_flags()
        self.update_from_state()

    @property
    def group(self) -> "ChunkContainerBase":
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
    def update_from_state(self):
        """
        Called when state of the group has changed
        """
        self.set_hidden(self.group.has_state(ChunkFlags.State.HIDDEN))
        self.set_enabled(not self.group.has_state(ChunkFlags.State.DISABLED))

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
            param (chunk_core.ChunkCommon): The param that was changed
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

    def _group_died(self, ref):
        self.delete_self()

    def _delete_self(self, notice, sender):
        if sender is not self.group:
            return

        self.group_widgets.clear()
        self.param_widgets.clear()

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
                self.param_widgets.pop(h)

        elif isinstance(h, ChunkContainerBase):
            if wid := self.group_widgets.get(h):
                self.remove_widget(wid)
                self.group_widgets.pop(h)

    def _state_changed(self, notice, sender):
        self.update_from_state()

    def _add_param_widget(self, param: "ChunkParamBase", group: Optional["ChunkContainerBase"] = None, list_index=0):
        t = param.evaluate_type()
        if t == ChunkFlags.ParamType.NONE:
            return

        widget: ChunkParamWidgetBase | None
        # Custom widget is created and managed externally
        if t == ChunkFlags.ParamType.CUSTOM and param.custom_widget and self.supports_widget(param.custom_widget):
            widget = param.custom_widget
        else:
            if not self.factory.has_mapping(t):
                print("Missing mapping in factory for creating widget", param.name)
                return

            # Create a widget from the factory based on the param.get_type()
            widget = self.factory.create_widget(param)

        if not widget:
            return

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
    """Used as attrs.field_transformer for chunk attributes.
    Adds cls.validate_attr as a validation function to all attrs.fields if hasattr(cls, "validate_attr").
    Adds cls._safe_convert to the conversion pipe of all attrs.fields if hasattr(cls, "_safe_convert")"""

    sort_keys = {}
    results = []
    for i, f in enumerate(fields):
        m = f.metadata

        validator = None
        converter = None
        if not _ATTRS_SUPPORT_CONVERTER and m.get("convert", True):
            # from functools import partial
            # The attrs.Converter is unavailable, so we can't bind it to _safe_convert. For this reason, we skip the
            # converter and validator here, and instead invoke _safe_convert() and validate_attr() in our on_setattr
            # hook
            converter = attrs.converters.default_if_none(f.default)

        elif m.get("convert", True) and hasattr(cls, "_safe_convert"):
            # Converters was added for attrs 24.1.0. Houdini use 22.2. If the Converters are available, we should use them.
            # create a converter that binds to the class instance's _safe_convert method
            converter = attrs.Converter(  # type: ignore
                lambda val, self_, attr: self_._safe_convert(attr, val),  # type: ignore
                takes_self=True,
                takes_field=True,
            )

        # Add the validate_attr function as a validator
        if m.get("validate", True) and hasattr(cls, "validate_attr"):
            validator = cls.validate_attr

        if f.converter and converter:
            converter = attrs.converters.pipe(converter, f.converter)
        elif f.converter:
            converter = f.converter

        if f.validator and validator:
            validator = attrs.validators.and_(validator, f.validator)
        elif f.validator:
            validator = f.validator

        # Find the first occurance of the attribute in the inheritance hierarchy, and use the index of the
        # first declaration. This is to match the constructor arguments of pylance
        index = get_attr_index(cls, f.name)
        if index is not None:
            sort_keys[f.name] = index
        else:
            sort_keys[f.name] = i

        results.append(f.evolve(converter=converter, validator=validator))

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

            resval = value

            # try:
            #     for name, func in inst._attrib_functions.items():
            #         attr_sender = inst._attr_senders.get(name)
            #         if not attr_sender:
            #             continue
            #         resval = func(inst, attr_sender)
            #         print("SOLVE ATTRIBUTE", name, "=", res)
            #         # setattr(self, name, res)

            # except AttributeError:
            #     pass

            # Run conversion
            if attrib and attrib.metadata.get("convert", True):
                resval = inst._safe_convert(attrib, value)

            if attrib and attrib.metadata.get("validate", True):
                inst.validate_attr(attrib, resval)

            old_value = None
            try:
                old_value = getattr(inst, name)
            except AttributeError:
                old_value = value

            super(res, inst).__setattr__(name, resval)

            try:
                inst.value_changed(name, old_value, resval)
            except AttributeError as e:
                print(e)
                pass

        res.__setattr__ = _wrapped_setattr  # type: ignore

        res = attrs.define(
            res, slots=False, eq=False, field_transformer=add_validate_and_convert, on_setattr=setattr_override
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

    def __repr__(self) -> str:
        return f"AttributeSender({self.name} = {self.value})"

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
            print(f"Failed to set attribute {self.name} from AttributeSender")
            return

        setattr(self.get_owner(), self.name, val)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return self.alias

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
            self.add_connection(new_conn)


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


class AttributesBase(chunk_notice.ChunkNoticePropagator, metaclass=AttributesMetaclass):
    """This class is used as base for Chunk dataclasses. Sends modification notices and validates/converts attribute values

    Any modifications to an attribute with `metadata["send_mode"]==SendMode.SEND`, will send a notice of type metadata["sender_alias"] when modified.
    This is achieved by using the AttributeSender class, which holds a handle to the owner (AttributesBase), name of the attribute it represents,
    and the alias for which notice to send. It also holds the old_value (the value of the attribute prior to the change) when available.

    Instances of this class will automatically set self as owner on all AttributeSenders it manages, which allows the attribute to call data_changed
    on this class when modified.

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
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
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
            raise copy.Error(
                f"Failed to copy {type(self).__name__}, the source and copy has mismatch in number of attributes"
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
                chunk_notice.ChunkNoticeAlias.INPUT_CHANGED,
            ]:
                for name, func in self._attrib_functions.items():
                    # name = notice.data.name
                    attr_sender = self._attr_senders.get(name)
                    # func = self._attrib_functions.get(name)

                    if attr_sender and func:
                        self._solving_attributes = True
                        res = func(self, attr_sender)
                        # print("SOLVE ATTRIBUTE", self.name, name, "=", res)
                        setattr(self, name, res)
                    # return None

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
            for attr in failed_attrs:
                setattr(self, attr, self._failed_converters[attr])

            count += 1
            if count > max_count:
                print("Failed to resolve failed converters. Tests exceeded max count")
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
        if val is None and attr.default:
            return attrs.converters.default_if_none(attr.default)(val)
        return val

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
    def __eq__(self, value: object) -> bool: ...


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
        self.__hashable = HashVal()
        self.__param_ref = weakref.ref(param, self._param_died)

        if isinstance(param, ChunkParamBase):
            self._value = param.value
            self.__value_sender = AttributeSender(self, "value", param.get_attribute_sender("value").alias)
        else:
            self._value = None
            self.__value_sender = None

        super().__init__(*args, **kwargs)
        self._children = NameSyncedContainer()

        if isinstance(param, ChunkContainerBase):
            self._children.set_owner(self)
            self._children = NameSyncedContainer.create_proxy(param.children, self._children)

            self.add_callback(self._ref_child_added, param, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_ADDED)
            self.add_callback(self._ref_child_removed, param, notice_alias=chunk_notice.ChunkNoticeAlias.PARAM_REMOVED)

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

    def __instancecheck__(self, instance: Any) -> bool:
        if issubclass(instance, ChunkCommon):
            return True
        if isinstance(self._param_ref, ChunkContainerBase) and isinstance(instance, ChunkContainerBase):
            return True
        if isinstance(self._param_ref, ChunkParamBase) and isinstance(instance, ChunkParamBase):
            return True
        return super().__instancecheck__(instance)  # type: ignore

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

    def send_self(
        self, alias: chunk_notice.ChunkNoticeAlias | None = None, data: Any = None
    ) -> chunk_notice.ChunkNotice | None:

        res = super().send_self(alias, data)

        if not res:
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
        if name == "value":
            # Get the value stored on the proxy
            return self._value
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

        if not name.startswith("__") and hasattr(super(), name) or name in ["_blocked", "_callbacks"]:
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

        if not name.startswith("__") and hasattr(super(), name) or name in ["_dag_node", "_blocked", "_callbacks"]:
            return super().__setattr__(name, value)

        if name == "children":
            return super().__setattr__("_children", value)

        if name == "value" and self.__value_sender:
            # Instead of setting the value on the param, we intercept and store the value on the
            # proxy itself. This will propagate the notices through the correct channel instead
            # of through the original param ownership channel.
            self.__value_sender.old_value = self._value
            self._value = value
            self.__value_sender.execute()
            return

        try:
            if not name.endswith("_param_ref") and hasattr(self._param_ref, name):
                return setattr(self._param_ref, name, value)
        except (ReferenceError, AttributeError):
            pass

        return super().__setattr__(name, value)


class NameSyncedContainer(chunk_notice.ChunkNoticePropagator):
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

    Attributes:
        last_added: The last item that was added to the container
        las_removed: The last item that was removed from the container

        params (ChainMap[ChunkHash, ChunkCommon]): A mapping with access to nested children
        names (ChainMap[str, ChunkHash]): A mapping from parameter names to the params map. A param with
            name "myParam" may be accessed as params[names["myParam"]]
    """

    def __init__(self):
        super().__init__()

        self.last_added: Optional[HashVal] = None
        self.last_removed: Optional[HashVal] = None

        self.params: ChainMap[ChunkHash, ChunkCommon] = ChainMap()
        self.names: ChainMap[str, ChunkHash] = ChainMap()

    def __bool__(self):
        return bool(self.params)

    def __copy__(self):
        copied = NameSyncedContainer()

        for param in self.params.maps[0].values():
            copied.add(copy.copy(param))

        copied._copy_callbacks(self)
        return copied

    def get_notice_data(self) -> Self:
        return self

    def clear(self):
        """
        Clear all children from this container. This will not delete the children explicitly as references to the children may
        be held elsewhere. However, it can be expected that the children are implicitly deleted when calling this function, as
        other objects should preferably only store weakrefs to the params
        """
        self.names.clear()
        self.params.clear()

    def add(self, obj: "ChunkCommon | ProxyParam", send=True):
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
        self.params.maps[0][obj._hashable] = obj  # type: ignore
        self.names.maps[0][obj.name] = obj._hashable

        # Mark self as the owner of the added obj as we need to be notified when the object changes
        obj.set_owner(self)

        if isinstance(obj, ChunkGroupBase):
            # Also append the children of the group, as this allows for fast lookups accross the layers.
            # This ignores the children of vectors, as access to the vector elements should be accessed on
            # the vector itself, and not as children
            self.params.maps.append(obj.children.params)
            self.names.maps.append(obj.children.names)

        self.send_added(obj)

    @staticmethod
    def create_proxy(source: "NameSyncedContainer", target: "NameSyncedContainer"):
        for param in source.get_my_params():
            proxy = ProxyParam(param)
            target.add(proxy)
        return target

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:

        # Override to reflect any changes on the children in this ds. Used to update the names when a name changes
        if notice and (p := notice.data):
            if isinstance(p, AttributeSender) and p.name == "name" and p.old_value in self.names.maps[0]:
                self.name_changed(notice.old_value)  # type: ignore

        return super().data_changed(notice)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        # We are only interested in hijacking data_changed, and should not send any signals.
        return chunk_notice.ChunkNoticeAlias.NONE

    def update_names(self):
        """Update the names ChainMap for all layers to reflect the names of the params. Can be used as brute-force
        to get correct mapping
        """
        invalid = []
        for i, mapping in enumerate(self.names.maps):
            for name, idx in mapping.items():
                obj = self.get_param(idx)

                if obj and obj.name != name:
                    invalid.append((i, name, obj.name))

        for i, old_name, new_name in invalid:
            idx = self.names.maps[i].pop(old_name)
            self.names.maps[i][new_name] = idx

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
        mapping = self.names.maps[0]
        if old_name in mapping:
            idx = mapping.pop(old_name)
            if obj := self.get_param(idx):
                mapping[obj.name] = idx
        else:
            raise ValueError(f"Failed to update old name {old_name} in container")

    def get_param(
        self, key: Union[ChunkHash, str], default: Any = None, proxy_index: Optional[int] = None
    ) -> Optional["ChunkCommon"]:
        """Retrieve a param from this datastructure. Key can be either a ChunkCommon (It hashes to the HashVal),
        an explicit HashVal (ChunkObject._hashable) or a name.

        Returns:
            ChunkCommon | None: The found Chunk object if found
        """
        if isinstance(key, str):
            return self.get_by_name(key, default)
        return self.params.get(key, default)

    def get_by_name(self, name: str, default: Any = None, proxy_index: Optional[int] = None) -> Optional["ChunkCommon"]:
        """
        Excplicitly retrieve a param by its name. In order to find nested children, one can
        pass the hierarchy to the 'name' argument by adding a dot (.) for each level in the hierarchy:

        ``` py
        myChunk.get_param("groupA.groupB.slider")
        ```

        Returns:
            ChunkCommon | None: The found Chunk object if found
        """
        if "." in name:
            hierarchy = name.split(".")
            cm: ChainMap = self.names

            idx = 0
            g = hierarchy[0]

            while idx < len(hierarchy) - 1:
                if g not in cm:
                    return None

                if g in cm.maps[0]:
                    # Found g in first layer, so can look for next entry in the query
                    idx += 1

                cm = cm.maps[1]  # type: ignore
                g = hierarchy[idx]

            if key := cm.get(g, default):
                return self.params.get(key, default)

        if key := self.names.get(name, default):
            return self.get_param(key, default)

    def get_names(self) -> Tuple[str, ...]:
        """Get all names of this layer and all child layers

        Returns:
            tuple[str, ...]: All param names on this layer and under
        """
        return tuple(self.names.keys())

    def get_my_names(self) -> Tuple[str, ...]:
        """Get all names on this layer only

        Returns:
            tuple[str, ...]: The param names of the first layer
        """
        return tuple(self.names.maps[0].keys())

    def get_params(self) -> Tuple["ChunkCommon", ...]:
        """Get all params on this layer and under

        Returns:
            Tuple[ChunkCommon, ...]: The params
        """
        return tuple(self.params.values())

    def get_my_params(self) -> Tuple["ChunkCommon", ...]:
        """Get all params on this layer only

        Returns:
            Tuple[ChunkCommon, ...]: The params
        """
        return tuple(self.params.maps[0].values())

    def send_added(self, obj: "ChunkCommon | ProxyParam"):
        """
        Sends a PARAM_ADDED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the new parameter
        """
        self.last_added = obj._hashable

        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, obj)

    def send_removed(self, obj: "ChunkCommon | ProxyParam"):
        """
        Sends a PARAM_REMOVED notice with the new obj as notice data and owner as the sender.

        Args:
            obj (ChunkCommon | ProxyParam): the removed parameter
        """
        self.last_removed = obj._hashable
        if owner := self.get_owner():
            owner.send_self(chunk_notice.ChunkNoticeAlias.PARAM_REMOVED, obj)

    def __delitem__(self, key: ChunkHash):
        if obj := self.get_param(key):
            # send_removed uses obj.send_self(). For this reason, we need to call send_remove before set_owner(None),
            # as send_self() will propagate the notice to the owners
            self.send_removed(obj)

            del self.names.maps[0][obj.name]
            del self.params.maps[0][key]

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
        obj = self.get_param(key, None)
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
    user_data: Dict[str, Any] = attrs.field(factory=dict)

    # Orientation of the widget, respected if relevant
    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.VERTICAL)

    # State flags of the param - hidden, collapsed, disabled
    state: ChunkFlags.State = attrs.field(
        default=ChunkFlags.State.NONE,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.STATE_CHANGED),
    )

    # Type specific flags, can be used for setting custom flags.
    flags: IntFlag = attrs.field(
        default=ChunkFlags.ParamFlags(0),
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED),
    )  # type: ignore

    # Hold a weakref to the owning chunk for faster access to it, without having to traverse the
    # hierarchy
    _chunk_parent: Optional[weakref.ReferenceType] = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(send_mode=SendMode.NO_SEND)
    )

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
        Implements the ChunkUsdLayerSupport protocol

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

    def is_blocked(self) -> bool:
        v = self.has_state(ChunkFlags.State.DISABLED)
        return v or super().is_blocked()

    def get_hash(self) -> ChunkHash:
        """
        Implements the ChunkUsdLayerSupport protocol

        Returns:
            ChunkHash: The hashval of this object
        """
        return self._hashable

    def get_edit_context(self) -> Sdf.Layer | None:
        """
        Implements the ChunkUsdLayerSupport protocol.

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
            try:
                layer = chunk_registry.ChunkLayerManager().get_or_create_layer(c)
            except AttributeError:
                return

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
        return flag in self.flags

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
        return state in self.state

    def __hash__(self) -> int:
        return hash(self._hashable)

    def __eq__(self, value: object) -> bool:
        return self._hashable == value

    def get_stage(self) -> Usd.Stage | None:
        """Easy access to the current UsdStage.
        Returns:
            Usd.Stage: The stage held by the SceneSender of ChunkRegistry
        """
        return chunk_registry.ChunkRegistry().scene.get_stage()

    def can_delete(self):
        return not bool(list(self.get_dag_node().get_outputs()))

    def request_delete(self):
        """Sends a notice for owners to delete this Chunk object. It's the owners responsibility to remove their reference to this object.
        If this parameter lies withing a ChunkContainerBase, then the containers' delete_param_or_group is called, with this instance as argument.
        """

        if not self.can_delete():
            print(f"Deleting node: {self.name} with connected outputs. Stuff may break")
            self.get_dag_node()._connections.inputs.clear()
            self.get_dag_node()._connections.outputs.clear()

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
    UsdAttribute. A subclass will need to implement the ChunkUsdLayerSupport protocol
    """

    # link to UsdAttribute if needed. Hidden in __init__ as the field would be added as the first argument
    # by the concrete classes
    _usd_prop: Optional[Usd.Property] = attrs.field(default=None, init=False)

    # Hold a connection to the Usd.Attribute of usd_attr.
    _usd_prop_connection: Optional[chunk_notice.NoticeConnection] = attrs.field(
        default=None, init=False, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )

    def data_changed(self, notice: Optional[chunk_notice.ChunkNotice] = None) -> Optional[chunk_notice.ChunkNotice]:
        my_notice = super().data_changed(notice)

        stage = chunk_registry.ChunkRegistry().scene.get_stage()
        if notice and stage and (usd_attr := self.get_usd_prop()) and not self.is_blocked():
            chunk_notice.send_notice(notice.alias, notice.data, usd_attr)

        return my_notice

    def get_stage(self) -> Usd.Stage | None:
        raise NotImplementedError

    def get_layer_identifier(self) -> str:
        raise NotImplementedError

    def get_hash(self) -> ChunkHash:
        raise NotImplementedError

    def get_edit_context(self) -> Sdf.Layer | None:
        raise NotImplementedError

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
        if isinstance(notice.data, Usd.Attribute):
            if notice.data.Get(0) == sender.value:
                return
            notice.data.Set(sender.value)

        elif isinstance(notice.data, Usd.Relationship) and sender.value is not None:
            # Currently just assume the string is a comma separated list of paths. This will likely
            # be changed in the future for a more robust approach
            paths = sender.value.split(",")
            if not all([Sdf.Path.IsValidPathString(x) for x in paths]):
                return
            targets = [Sdf.Path(x) for x in paths]
            notice.data.SetTargets(targets)

    @abstractmethod
    def usd_prop_changed(self, attr: Usd.Property):
        """
        A callback for when the UsdAttribute member was changed. This will update the value of
        the AttributeSender passed to set_usd_attr().

        Args:
            attr (Usd.Attribute): The updated UsdAttribute.
        """
        if not self._usd_prop_connection:
            return
        if not isinstance(self._usd_prop_connection.sender, AttributeSender):
            return

        if isinstance(attr, Usd.Attribute):
            self._usd_prop_connection.sender.value = attr.Get(0)
        elif isinstance(attr, Usd.Relationship):
            self._usd_prop_connection.sender.value = ",".join([str(x) for x in attr.GetForwardedTargets()])

    def set_usd_prop(self, attr: Usd.Property, sender: AttributeSender):
        """
        Set the references UsdAttribute object. Any changes to the param will
        update the attribute

        Args:
            attr (Usd.Attribute): The UsdAttribute to refrence
            sender (AttributeSender): A sender object that should connect to the UsdAttribute.
                When a signal with sender and ATTRIBUTE_CHANGED notice is sent, the method
                update_usd_attrib() of this class is called
        """
        self._usd_prop = attr

        if self._usd_prop_connection:
            self._usd_prop_connection.disconnect()

        self._usd_prop_connection = chunk_notice.NoticeConnection(
            self.update_usd_prop, chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED, sender, owner=self
        )
        self.usd_prop_changed(attr)

    def get_usd_prop(self) -> Usd.Property | None:
        """
        Get the usd attribute references by this object

        Returns:
            Usd.Attribute | None: The referenced usd attribute
        """
        return self._usd_prop

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

        try:
            if sender := param.get_attribute_sender("value"):
                param.set_usd_prop(attr, sender)
        except AttributeError:
            pass

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
    value: ValueT = attrs.field(default=None, init=False)

    # If possible, will use this custom widget in the ui.
    # TODO: Currently, this only works with ChunkParamBase objects with type == CUSTOM
    #       This should also work on groups and chunks... But thats a bit more complicated
    custom_widget: Optional[ChunkParamWidgetBase] = attrs.field(default=None)

    def convert_attr(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "value":
            if val is None:
                return self.default_value

        if attr.name == "default_value":
            supported = ChunkFlags.ParamTypeToTypes(self.get_type())

            if not supported:
                return val

            type_default = next(iter(supported))

            if val is None:
                return type_default()

            if type(val) is not type_default:
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


class ChunkContainerBase(ChunkCommon):
    """
    A base class for container type parameters such as Vectors, Groups and Chunks. The container can hold
    parameters (ChunkParamBase) or other ChunkContainerBase objects.

    Attributes:
        children (NameSyncedContainer): The underlying container owning the child parameters
        widget_factory (Optional[ChunkWidgetFactoryBase]): A factory that the ui implementation can use for
            changing the default ui implementation of the container and its children
    """

    children: NameSyncedContainer = attrs.field(
        init=False,
        factory=NameSyncedContainer,
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
        # if not self.can_delete():
        #     # print("Cant delete", self.name)
        #     return

        self.children.clear()
        self.children.set_owner(None)

        super().request_delete()

    def get_param(self, param: ChunkCommon | str | HashVal) -> Optional[ChunkCommon]:
        """
        Get a parameter or group inside of this group. A string can be passed to get a parameter by name.
        The string can also be the explicit hierarchy of a param by spacing the hiearchy levels with a dot

        ``` py
        myChunk.get_param("groupA.groupB.param")
        ```

        Args:
            param (ChunkParamBase | str): A parameter to find. If string is passed, look for param with name

        Returns:
            ChunkParam: The found parameter or None if not found
        """

        if isinstance(param, (ChunkCommon, HashVal, ProxyParam)):
            return self.children.get_param(param)
        elif isinstance(param, str):
            return self.children.get_by_name(param)

    def get_params(self) -> Tuple[ChunkCommon, ...]:
        """Get all parameters in this group including nested

        Returns:
            Tuple[ChunkParamBase]: All parameters in this group
        """
        return self.children.get_params()

    def get_my_params(self) -> Tuple[ChunkCommon, ...]:
        """Get all parameters added directly to this group

        Returns:
            Tuple[ChunkParamBase]: All parameters owned by this group
        """
        return self.children.get_my_params()

    # def get_last_param_change(
    #     self,
    # ) -> Tuple[Tf.Notice | None, chunk_notice.ChunkNoticePropagator | None]:
    #     """Retreive the last changed parameter in this group. Will look inside nested groups as well to find parameter changes

    #     Returns:
    #         Tuple[Tf.Notice, ChunkParam]: Last changed ChunkParam and the notice associated with the change on this group or in nested group
    #     """
    #     return list(self.iter_changes())[-1]

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
        found_names: set[str] = set()

        for p in self.children.params.values():
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
                owner_has_child = p in owner.get_my_params()
                if not owner_has_child:
                    raise ValueError(
                        failed_msg.format(f"{p.name} has owner: {owner.name}. Owner does not hold this param")
                    )

            if p in found_params:
                raise NameError(failed_msg.format(f"{p.name} is duplicated in the hierarchy"))
            found_params.add(p)

            if p.name in found_names:
                raise NameError(failed_msg.format(f"param name: {p.name} is not unique in the hierarchy"))
            found_names.add(p.name)

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
            msg += "\n"

        print(msg)

    def iter_hierarchy(self, level=0) -> Generator[Tuple[int, ChunkCommon], None, None]:
        """
        Provides a generator for iterating over the hierarchy, starting from self,
        going to all children and recursively through nested groups.

        Args:
            level (int, optional): A value saying what level of the hierarchy the yielded value come from, starting at 0. Defaults to 0.

        Yields:
            Tuple[int, ChunkCommon]: _description_
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

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED

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
        """A combination of reserved param and group names

        Returns:
            Set[str]: A set of reserved group and parameter names which new parameters should not be named
        """
        return set(self.children.get_names()) | set([self.name])

    @staticmethod
    def get_valid_child_name(orig_name: str, reserved_names: Set[str]) -> str:
        """Get a unique name for param or group to be added on this group by adding a numerical suffix to duplicate names

        Args:
            param (ChunkParam): a name that might not be unique
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

    Args:
        size (int): The size of the list. This can be modified at runtime to extend / shrink the list
    """

    size: int = attrs.field(default=0)
    proxy_params: List[ChunkGroupBase] = attrs.field(factory=list, init=False)

    def get_indexed_param(self, param: ChunkCommon | str | HashVal, index: int) -> ChunkCommon | None:
        """
        Get a param from the proxy list. The index should be an index of the proxy_params list. If found, The returned
        value is a ProxyParam referencing the param input, at index.

        Args:
            param (ChunkCommon | str | HashVal): The param to look for. If a string is supplied, the parameters name will be used for equality check
            index (int): The index in to proxy_params which to get the parameter from

        Returns:
            ChunkCommon | None: The proxy parameter referencing param

        Raises:
            IndexError:
                If the index is out of bounds

        """
        if len(self.proxy_params) == 0:
            return

        if index > len(self.proxy_params):
            raise IndexError("Index out of range")
        return self.proxy_params[index].get_param(param)

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        if attrname == "size":
            if value > old_value:
                for i in range(old_value, value):
                    group = ChunkGroupBase(str(i), flags=ChunkFlags.GroupFlags.LABELED)
                    group.set_owner(self)

                    proxy = NameSyncedContainer()
                    proxy.set_owner(group)
                    self.proxy_params.append(group)

                    group.children = proxy
                    NameSyncedContainer.create_proxy(self.children, proxy)
                    self.send_self(chunk_notice.ChunkNoticeAlias.PARAM_ADDED, group)

            elif value < old_value and len(self.proxy_params) > value:
                for i in range(value, old_value):
                    self.proxy_params[i].request_delete()
                    self.proxy_params.pop(i)

            for i, grp in enumerate(self.proxy_params):
                grp.name = str(i)

            if value != old_value:
                self.send_self(chunk_notice.ChunkNoticeAlias.PARAM_CHANGED)

        return super().value_changed(attrname, old_value, value)

    def get_params(self) -> Tuple[ChunkCommon, ...]:
        res = []
        for container in self.proxy_params:
            for param in container.get_params():
                res.append(param)
        return tuple(res)

    def get_my_params(self) -> Tuple[ChunkCommon, ...]:
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
            self.proxy_params[index].request_delete()
            self.proxy_params.pop(index)
        except IndexError as e:
            print("Failed to remove index from list. Index out of range", e)
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
    _config: Optional["ChunkConfig"] = attrs.field(default=None, init=False)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

        self.add_callback(self.disable, notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE)
        self.add_callback(self.enable, notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE)
        self.add_callback(self.remove, notice_alias=chunk_notice.ChunkNoticeAlias.MENU_ACTION_NOTICE)

    def disable(self, notice, sender):
        self.set_state(ChunkFlags.State.DISABLED)

    def enable(self, notice, sender):
        self.unset_state(ChunkFlags.State.DISABLED)

    def remove(self, notice, sender):
        self.request_delete()

    def get_config(self) -> Optional["ChunkConfig"]:
        return self._config

    def get_chunk(self) -> Optional["ChunkBase"]:
        return self


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

    param: ChunkParamBase = attrs.field(default=None)
    length: int = attrs.field(default=3)
    suffixes: Optional[tuple[str, ...] | str] = attrs.field(default=None)

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

        if self.children:
            # __attrs_post_init__ was called when copied, so no need to reinitialize the children as they
            # have already been initialized
            return

        for i in range(self.length):
            # Clone the self.param length times, and add the clones to the clildren container
            np = self.param.clone()

            if self.suffixes:
                np.name += self.suffixes[i]

            self.children.add(np)

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

        if notice and (p := notice.data):
            if isinstance(p, AttributeSender) and p.name == "value" and not self.is_blocked():
                if usd_attr := self.get_usd_prop():
                    chunk_notice.send_notice(p.get_notice_alias(), self.get_attribute_sender("value"), usd_attr)

        return my_notice

    def __setitem__(self, key: str | int, val: Any) -> Optional[ChunkParamBase]:
        param = self[key]
        if not isinstance(param, ChunkParamBase):
            raise ValueError("ChunkContainers does not support value assignment")

        param.value = val

    @property
    def value(self):
        return tuple(x.value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @value.setter
    def value(self, val: Tuple[Any, ...]):
        if not len(val) == self.length:
            raise ValueError(
                f"Trying to set value {val} on vector param {self.name} with wring size. Size should be {self.length}"
            )

        for i in range(self.length):
            self[i] = val[i]

    @property
    def default_value(self):
        return tuple(x.default_value for x in self.get_my_params() if isinstance(x, ChunkParamBase))

    @default_value.setter
    def default_value(self, val: Tuple[Any, ...]):
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


class ChunkConfig(AttributesBase):
    """
    Base class for Configs. A config sets some load time configurations for a chunk.
    """

    category: str = attrs.field(default="Chunks")
    description: str = attrs.field(default="Chunk description...")
    load: bool = attrs.field(default=True)
    load_for_plugin: Optional[str] = attrs.field(default=None)
    requirements: list[str] = attrs.field(factory=list)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.CHUNK_CONFIG_CHANGED
