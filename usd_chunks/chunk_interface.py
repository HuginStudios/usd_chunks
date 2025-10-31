import copy
from itertools import chain, islice
from typing import (
    Callable,
    Generator,
    List,
    Sequence,
    Any,
    Dict,
    Protocol,
    Tuple,
    Type,
    TypeVar,
    TypeAlias,
    Optional,
    Union,
    Iterable,
    Generic,
    TypedDict,
    get_origin,
)

from typing_extensions import dataclass_transform

from abc import abstractmethod
import attrs
from attr import _make as attrMake
from collections import ChainMap
from enum import IntEnum, IntFlag

import re
import uuid

from . import chunk_notice, chunk_registry
from .chunk import ChunkFlags

import os
from pxr import Tf, Usd, Usdviewq

################################
# BASE CLASSES FOR UI ELEMENTS #
################################

ValueT = TypeVar("ValueT")
ParamT = TypeVar("ParamT", bound="ChunkParamBase")
GroupT = TypeVar("GroupT", bound="ChunkContainerBase")
ChunkT = TypeVar("ChunkT", bound="ChunkCommon")


class ChunkParamWidget(Generic[ParamT]):
    """Base class for a chunk param widget. Creating a custom widget should subclass
    the relevant Qt widget as well as this class, such as:
    ```
        class myWidget(QtWidgets.QPushButton, ChunkParamWidget)
    ```
    The subclass should override the relevant methods from this class
    """

    def __init__(self, param):
        super().__init__()
        self.param: ParamT = param

    def param_changed_callback(self, chunkItem: "ChunkCommon", notice: Tf.Notice, param: ParamT):
        if param.value is not None:
            self.set_value(param.value)

    def setup_chunk_widget(self):
        value = self.param.value if self.param.value is not None else self.param.default_value
        label = self.param.label if self.param.label else self.param.name

        self.set_value(value)
        self.set_label(label)

        if self.param.icon:
            self.set_icon(self.param.icon)

        self.set_param_layout(self.param.layout)

        self._connection = chunk_notice.NoticeConnection(
            self.param_changed_callback, chunk_notice.ChunkNoticeAlias.ATTRIBUTE_VALUE_CHANGED, self.param
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
    def set_param_layout(self, layout_flags: ChunkFlags.LayoutFlags):
        pass

    def value_changed(self, value: Any):
        if self.param is not None:
            self.param.value = value


class ChunkWidgetFactoryBase(Protocol):
    def __init__(self) -> None:
        self.widget_map: Dict[ChunkFlags.ParamType, Any]
        super().__init__()

    def get_map(self):
        return self.widget_map

    def add_mapping(self, key: ChunkFlags.ParamType, widget_type: Type[ChunkParamWidget]):
        self.widget_map[key] = widget_type

    def has_mapping(self, key: ChunkFlags.ParamType):
        return key in self.get_map()

    @abstractmethod
    def create_widget(self, param: "ChunkParamBase") -> Any:
        raise NotImplementedError


#####################
# FIELD TRANSFORMER #
#####################


def add_validate_and_convert(cls: type["AttributesBase"], fields: List[attrs.Attribute]):
    """Used as attrs.field_transformer for chunk attributes.
    Adds cls._validate as a validation function to all attrs.fields if hasattr(cls, "_validate").
    Adds cls._convert to the conversion pipe of all attrs.fields if hasattr(cls, "_convert")"""

    results = []
    for f in fields:
        if hasattr(cls, "_convert"):
            conv = attrs.Converter(
                lambda val, self_, attr: self_._convert(attr, val),  # type: ignore
                takes_self=True,
                takes_field=True,
            )

            if f.converter:
                conv = attrs.converters.pipe(conv, f.converter)

        else:
            conv = f.converter

        if hasattr(cls, "_validate"):
            valid = cls._validate
            if f.validator:
                valid = attrs.validators.and_(valid, f.validator)
        else:
            valid = f.validator

        results.append(f.evolve(converter=conv, validator=valid))

    return results


@dataclass_transform(field_specifiers=(attrs.Attribute, attrs.field), eq_default=False)
class AttributesMetaclass(type):
    """Metaclass used to wrap Chunk attributes classes around attrs. Using it through metaclass instead of decorating all
    classes with @attrs.define order to have proper default behaviour of all chunk classes

    Provides:
    calls value_changed() on subclasses after __setattr__
    add _validate() and _convert() as default validator and conversion functions to all attrs.fields where applicable
    """

    def __new__(cls, name, bases, attributes):
        # Construct new class and wrap it around attrs.define
        res = super().__new__(cls, name, bases, attributes)
        res = attrs.define(res, slots=False, eq=False, field_transformer=add_validate_and_convert)
        return res

    def __setattr__(cls, name: str, value: Any) -> None:
        """Overrides the default __setattr__ for all cubclasses. This is to inject a custom callback AFTER attrs validation and conversion.
        The injected callback is used to send attribute changed notices. This should happen after any value changes, which is otherwise
        not supported by attrs when using os_setattr hooks (validation, convert).

        Will look for value_changed functions on the cls, and if it exists, add a call to it after the attrs __setattr__ function was called

        Args:
            name (str): _description_
            value (Any): _description_
        """

        if name == "__setattr__" and hasattr(cls, "value_changed"):

            def _setattr_wrapper(func: Callable, func1: Callable):
                def _wrapped_setattr(inst, name, value):
                    try:
                        old_value = getattr(inst, name)
                    except AttributeError:
                        old_value = None

                    # First call original attrs __setattr__
                    func(inst, name, value)
                    # Next call value_changed
                    func1(inst, name, old_value, value)

                return _wrapped_setattr

            return super().__setattr__(name, _setattr_wrapper(value, getattr(cls, "value_changed")))

        return super().__setattr__(name, value)


class AttributeSender(chunk_notice.ChunkNoticePropagator):
    """An AttributeSender gets sent when an attribute changes on a AttributesBase instance, as attrs.Attribute
    is not able to send through the Tf.Notice system. The sender holds:
    name: The name of the attribute that was sent
    owner: The owning objcet of the attribute
    alias: The ChunkNoticeAlias that is sent when the attribute changed

    """

    def __init__(self, owner: "AttributesBase", name: str, alias: chunk_notice.ChunkNoticeAlias):
        super().__init__()
        self.name: str = name
        self.alias: chunk_notice.ChunkNoticeAlias = alias
        self.old_value: Any = None

        self.set_owner(owner)

    @property
    def value(self) -> Any:
        if not self.get_owner():
            return None
        return getattr(self.get_owner(), self.name)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return self.alias


class CopyMode(IntEnum):
    COPY = 0  # copy.copy() attribute. This is default if nothing else is specified
    DEEPCOPY = 1  # copy.copy() attribute. This is default if nothing else is specified
    NO_COPY = 2  # Does not copy attribute, sets default instead
    SHARE = 3  # Share the the attribute with the source


class SendMode(IntEnum):
    NO_SEND = 0
    SEND = 1


class AttributeMetadata(TypedDict, total=False):
    """Use in field(metadata=AttributeMetadata(...)).

    - copy_mode
    Decides how the attribute should be treated when copying. If not set, use a COPY
    - send_mode
    Decides if the attribute should send a notice when modified. If not set, use SEND
    - sender_alias
    What ChunkNoticeAlias to use when sending. If not set, will use ATTRIBUTE_CHANGED

    """

    copy_mode: CopyMode
    send_mode: SendMode
    sender_alias: chunk_notice.ChunkNoticeAlias


class AttributesBase(chunk_notice.ChunkNoticePropagator, metaclass=AttributesMetaclass):
    """This class is used as base for Chunk dataclasses. Sends modification notices and validates/converts attribute values

    Any modifications to an attribute with metadata["send_mode"]==SendMode.SEND, will send a notice of type metadata["sender_alias"] when modified.
    This is achieved by using the AttributeSender class, which holds a handle to the owner (AttributesBase), name of the attribute it represents,
    and the alias for which notice to send.

    Instances of this class will automatically create and listen to their own attributes notices, which creates a ladder-like flow of notices:

    AttributeSender A sends A.get_notice_alias()
        AttributesBase B listens to A.get_notice_alias() -> sends B.get_notice_alias()

    """

    # Store NoticeConnection objects, these hold information about a connection to a callback function.
    _attr_senders: Dict[str, AttributeSender] = attrs.field(
        factory=dict, init=False, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )

    def __attrs_post_init__(self):
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
            sender.add_callback(self.data_changed)

            self._attr_senders[name] = sender

        super().__init__()  # initialise ChunkNoticePropagator

    def _copy_connection(self, conn: chunk_notice.NoticeConnection, src: chunk_notice.ChunkNoticePropagator):
        if isinstance(conn.sender, AttributeSender) and conn.sender.get_owner() is src:
            # Someone is listening on one of src's attributes. When copying src, we should also connect the listener to
            # the copied attribute
            sender = self.get_attribute_sender(conn.sender.name)
            if sender:
                new_conn = chunk_notice.NoticeConnection(conn.method, conn.alias, sender, conn.globally)
                self.add_connection(new_conn)
                return

        super()._copy_connection(conn, src)

    def __copy__(self) -> "AttributesBase":
        """Create a new instance of self.__class__ and set member variabled appropriately. A fields metadata["copy_mode"] sets how the field should transfer to the copy.
        If a non-attrs class exists in the hierarchy, its members will be ignored. The exception being ChunkNoticePropagator, which is initialized in AttributesBase.__attrs_post_init__().

        No __init__ is called on the copied class. Instead, the attrs pre and post init methods are called, if available
        __attrs_pre_init__ is called prior to copying the fields
        __attrs_post_init__ is called after copying the fields.
        Any instantiation of base classes should be done inside either of these functions. For example, ChunkNoticePropagaor is initialized inside of AttributesBase.__attrs_post_init__()

        Returns:
            ChunkParamBase: Return a copy of self, with updated uuid and rewired connections
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

            if copy_mode is CopyMode.DEEPCOPY:
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

        copied.__dict__.update(data)

        # Call the post-init if available. If not explicitly overwritten, this should initialize the base ChunkNoticePropagator
        post_init = getattr(copied, "__attrs_post_init__", None)
        if callable(post_init):
            post_init()

        if len(copied.__dict__) != len(self.__dict__):
            raise copy.Error(
                f"Failed to copy {type(self).__name__}, the source and copy has mismatch in number of attributes"
            )
        # copied.set_owner(self.get_owner())

        # Copy any connections to self over to the copy
        copied._copy_connections(self)
        return copied

    @classmethod
    def inspect_class(cls) -> attrs.ClassProps:
        """Wrapper around attrs.inspect

        Returns:
            attrs.ClassProps: ClassProps of this class
        """
        return attrs.inspect(cls)

    def get_attribute_sender(self, attribname: str) -> AttributeSender | None:
        """Get a sender for an attribute on this instance. The AttributeSender can be used to
        subscribe to specific attribute changes in callbacks, either by:
            attributeSender.add_callback(some_method)
        or:
            chunkParamBase.add_callback(some_method, sender=attributeSender)

        A gotcha with the first method is that AttributeSenders are not preserved in copies, so:
            c = chunk.Checkbox("someName")
            c.get_attribute_sender("value").add_callback(someFunc)
            myChunk.add_params([C, C])
        would mean that only the first instance of C will call someFunc when its value attribute changes

        Args:
            attribname (str): _description_

        Returns:
            AttributeSender | None: _description_
        """
        return self._attr_senders.get(attribname, None)

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        """Called after any attr __setattr__ was called.
        This function should not mutate self, only send notices of the modified attributes when applicable.
        If an attribute exists in self._attr_senders (added in __attrs_post_init__), the mapped AttributeSender is used
        as sender for the notice. Otherwise, use self as sender

        This method is explicitly injected to __setattr__ by AttributesMetaclass

        Args:
            attrname (str): The name of the attribute that was changed
            value (Any): The new value
        """
        if old_value == value or attrname.startswith("_"):
            # No change was made, we should not send the notice
            return

        # Send a TfNotice for the changed attribute. Assuming
        attr_sender = self._attr_senders.get(attrname)
        if attr_sender:
            attr_sender.old_value = old_value
            attr_sender.send_self()
        else:
            self.send_self()

    def _convert(self, attr: attrs.Attribute, val: Any) -> Any:
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

    def set_validators_enabled(self, enabled: bool):
        # TODO: Implement
        pass

    def _validate(self, attr: attrs.Attribute, val: Any) -> bool:
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

    def _validate_path(self, attr: attrs.Attribute, value) -> bool:
        """Check if the given value is a valid os path to be used as an attribute.
        Can be called in _validate or _convert of subclasses when checking a path attribute. Returns True if the
        value is a valid path. Currently, this just mirrors _validate_string. Use _validate_existing_path to check for existing paths

        Args:
            attr (Attrs.Attribute): _description_
            value (_type_): _description_

        Returns:
            bool: _description_
        """
        if not self._validate_string(attr, value):
            return False

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


class CommonAttributes(AttributesBase):
    """Holds common attributes for Chunk objects. Used by ChunkCommon"""

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
        default=0,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.FLAGS_CHANGED),
    )  # type: ignore

    def _convert(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "name":
            return re.sub(r"\W+", "", str(val))

        return super()._convert(attr, val)

    def _validate(self, attr: attrs.Attribute, val: Any) -> bool:
        # Called when a attrs.field is modified. Should raise ValueError if the validation fails
        if attr.name in ["name"]:
            return self._validate_name(attr, val)
        if attr.name == "icon" and val:
            # Icon path should exist if. if bool(val) is False, then we can ignore the validation
            return self._validate_existing_path(attr, val)
        if attr.name in ["tooltip", "label"]:
            return self._validate_string(attr, val)

        return super()._validate(attr, val)

    def _validate_name(self, attribute, value) -> bool:
        self._validate_string(attribute, value)

        if " " in value:
            raise ValueError(f"{value} contains spaces")

        if len(value) > 20:
            raise ValueError(f"name is too long: {value}")

        return True

    def get_label(self) -> str:
        return self.name if not self.label else self.label

    def set_flag(self, flag: IntFlag):
        self.flags |= flag

    def unset_flag(self, flag: IntFlag):
        self.flags &= ~flag

    def has_flag(self, flag: IntFlag) -> bool:
        return flag in self.flags

    def set_state(self, state: ChunkFlags.State):
        self.state |= state

    def unset_state(self, state: ChunkFlags.State):
        self.state &= ~state

    def has_state(self, state: ChunkFlags.State) -> bool:
        return state in self.state


class ChunkHash(Protocol):
    def __hash__(self) -> int:
        ...
    def __eq__(self, value: object) -> bool:
        ...

class NameSyncedContainer(chunk_notice.ChunkNoticePropagator):
    """Container for holding ChunkCommon objects. The container holds two maps: params & names

    params maps from a uuid to a ChunkCommon object. ChunkCommon objects hash by their uuid, so accessing a param
    can be done either by the ChunkCommon object as params[obj], or the uuid: params[obj._hashable]. Where a typehint
    suggests a HashVal argument for lookup (get_param(), get_my_param()), passing a ChunkCommon obj should 
    return same as passing obj._hashable.
    TODO: This is a bit un-intuituve, and should perhaps be reconsidered.

    names map betweeen the current (mutable) name of a param and the uuid. This exists in order to quicker acccess
    params by their name. This container need to be maintained and updated whenever a param name is changed, which is currently
    achieved by subclassing ChunkNoticePropagator, in order to insert itself in hierarchy of objects. 
    
    Any objects added though 
        NameSyncedContainer.add()
    will have the NameSyncedContainer as owner. Any concrete chunk object who holds an instance of this class should therefore not
    manage ownership of params, instead, it should own the NameSyncedContainer only. This class will not send any notices though, only propagate
    notices from children.
    """

    def __init__(self):
        super().__init__()

        self.params: ChainMap[ChunkHash, ChunkCommon] = ChainMap()
        self.names: ChainMap[str, ChunkHash] = ChainMap()

    def add(self, obj: "ChunkCommon"):
        """Add a chunk object to this container. This will make the container the owner of the param, and store the
        object in self.params, and obj.name in self.names. Access to the object can thus be done through
            self.get_param(), self.get_by_name()
        or
            self.params.maps[0].get(self.names.maps[0].get("some_name"))
        since the container manages the name<->obj relationship, some care need to be taken in order for this relationship to be maintained.
        For example, do not add params to self.params or self.names excplicitly, instead, use this function to get proper behaviour.

        If the added param is an instance of ChunkContainerBase, the children of the new param will be appended to the self.params and self.names
        ChainMaps, which allows for fast access of nested params.

        Args:
            obj (ChunkCommon): The param to add. Nothing is done to avoid duplication across layers.
        """

        # Append child the object + name to the first layer of the ChainMaps
        self.params.maps[0][obj._hashable] = obj
        self.names.maps[0][obj.name] = obj._hashable

        # Mark self as the owner of the added obj as we need to be notified when the object changes
        obj.set_owner(self)

        if isinstance(obj, ChunkContainerBase):
            # Also append the children of the group, as this allows for fast lookups accross the layers
            self.params.maps.append(obj.children.params)
            self.names.maps.append(obj.children.names)

    def data_changed(
        self, notice: Tf.Notice, sender: Any, propagated: Optional[chunk_notice.ChunkNoticePropagator] = None
    ) -> Optional[Tf.Notice]:
        # Override to reflect any changes on the children in this ds. Used to update the names when a name changes

        if (
            propagated
            and isinstance(propagated, AttributeSender)
            and propagated.name == "name"
            and propagated.old_value in self.names.maps[0]
        ):
            self.name_changed(propagated.old_value)

        return super().data_changed(notice, sender, propagated)

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

            c = Chunk.Slider("A")
            g = Chunk.Group("group")
            g.add_param(c)

            p = g.get_param("A")
            asset(p != None)

            p.name = "B"
            p = g.get_param("B")
            asset(p != None && g.get_param("A") == None)

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

    def get_param(self, key: Union[ChunkHash, str], default: Any = None) -> Optional["ChunkCommon"]:
        """Retrieve a param from this datastructure. Key can be either a ChunkCommon (It hashes to the HashVal), an explicit HashVal (ChunkObject._hashable) or a name.

        Returns:
            ChunkCommon | None: The found Chunk object if found
        """
        if isinstance(key, str):
            return self.get_by_name(key, default)
        return self.params.get(key, default)

    def get_by_name(self, name: str, default: Any = None) -> Optional["ChunkCommon"]:
        """Excplicitly retrieve a param by its name

        Returns:
            ChunkCommon | None: The found Chunk object if found
        """
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

    def __delitem__(self, key: ChunkHash):
        if obj := self.get_param(key):
            del self.names.maps[0][obj.name]
            del self.params.maps[0][key]

    def pop(self, key: ChunkHash):
        obj = self.get_param(key, None)
        if obj:
            del self.names.maps[0][obj.name]
            del self.params.maps[0][key]

            if obj.get_owner() == self:
                obj.set_owner(None)

        return obj


class GroupAttributes(AttributesBase):
    """Holds attributes specific to groups"""

    children: NameSyncedContainer = attrs.field(
        init=False,
        factory=NameSyncedContainer,
        metadata=AttributeMetadata(send_mode=SendMode.NO_SEND, copy_mode=CopyMode.NO_COPY),
    )

    widget_factory: Optional[ChunkWidgetFactoryBase] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(ChunkWidgetFactoryBase)),
        metadata=AttributeMetadata(copy_mode=CopyMode.SHARE),
    )

    def __attrs_post_init__(self):
        self.children.set_owner(self)
        return super().__attrs_post_init__()

    # def data_changed(
    #     self, notice: Tf.Notice, sender: Any, propagated: Optional[chunk_notice.ChunkNoticePropagator] = None
    # ) -> Tf.Notice:
    #     if propagated and isinstance(propagated, AttributeSender) and propagated.name == "name":
    #         try:
    #             self.children.name_changed(propagated.old_value)
    #         except ValueError:
    #             pass

    #     return super().data_changed(notice, sender, propagated)


class VectorAttributes(AttributesBase):
    """Attributes for Vector containers. This works similarly to a group, but with one specific param, which will be duplicated 'length' times."""

    param: "ChunkParamBase" = attrs.field()
    length: int = attrs.field(default=3)
    suffixes: Optional[tuple[str, ...] | str] = attrs.field(default=None)

    def _validate(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "suffixes" and val is not None:
            if len(val) != self.length:
                raise ValueError(
                    "Vector suffixes length does not match vector length. Set either as 'XYZ' or ('X', 'Y', 'Z') for a 3 length vector"
                )
        return super()._validate(attr, val)


class ChunkAttributes(AttributesBase):
    """Holds attributes specific to chunks. A chunk is a subclass of group, which in turn inherits GroupAttrs.Attributes, hence we should keep these separate here"""

    category: str = attrs.field(default="Chunks")

    def _convert(self, attr: attrs.Attribute, val: Any) -> Any:
        if attr.name == "category" and not isinstance(val, str):
            return attr.default

        return super()._convert(attr, val)


class ParamAttributes(Generic[ValueT], AttributesBase):
    """Holds attributes for valued parameters. The type of param is decided based on the param_type when generating ui"""

    default_value: Optional[ValueT] = attrs.field(default=None)
    value: Optional[ValueT] = attrs.field(
        default=None,
        init=False,
        metadata=AttributeMetadata(sender_alias=chunk_notice.ChunkNoticeAlias.ATTRIBUTE_VALUE_CHANGED),
    )

    # If possible, will use this custom widget in the ui.
    # TODO: Currently, this only works with ChunkParamBase objects with type == CUSTOM
    #       This should also work on groups and chunks... But thats a bit more complicated
    custom_widget: Optional[ChunkParamWidget] = attrs.field(default=None)

    def _convert(self, attr: attrs.Attribute, val: Any) -> Any:
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

        return super()._convert(attr, val)

    def _validate(self, attr: attrs.Attribute, val: Any) -> bool:
        if attr.name == "value" and val is not None:
            if type(val) not in ChunkFlags.ParamTypeToTypes(self.get_type()):
                raise ValueError(f"Value of {val} is not supported for attribute {attr.name}")
            return True

        if attr.name == "custom_widget":
            # Custom widget should be a subclass of ChunkParamWidget
            if val is not None and not isinstance(val, ChunkParamWidget):
                raise ValueError(f"custom_widget {val} is not a subclass of ChunkParamWidget")
            return True
        return super()._validate(attr, val)

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


######################
# CHUNK BASE CLASSES #
######################


@attrs.frozen
class HashVal:
    @staticmethod
    def generate_key() -> int:
        """Generate a new uuid value used for hasing. Should only be used internally"""
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


class ChunkCommon(CommonAttributes):
    """Base class for all Chunk objects. Should not be instantiated directly"""

    # Please do not mutate!
    _hashable: HashVal = attrs.field(
        init=False, factory=HashVal, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND)
    )

    def __hash__(self) -> int:
        return hash(self._hashable)

    def __eq__(self, value: object) -> bool:
        return self._hashable == value

    @staticmethod
    def get_usdview_api() -> Usdviewq.usdviewApi.UsdviewApi:
        """Rettrieve the usdviewAPI handle of the current session

        Returns:
            Usdviewq.usdviewApi: The usdviewAPI instance
        """
        return chunk_registry.ChunkRegistry().usdview_api

    def get_stage(self) -> Usd.Stage | None:
        """Easy access to the current UsdStage.
        Currently retrieves it from the usdviewAPI. Return None if the usdviewAPI is not available

        Returns:
            Usd.Stage: The found stage, or None if not available
        """
        api = self.get_usdview_api()
        return None if not api else api.stage  # type: ignore

    def request_delete(self):
        """Sends a notice for owners to delete this Chunk object. It's the owners responsibility to remove their reference to this object.
        If this parameter lies withing a ChunkContainerBase, then the containers' delete_param_or_group is called, with this instance as argument
        """

        owner = self.get_owner()
        if owner and isinstance(owner, ChunkContainerBase):
            owner.delete_param_or_group(self)
        else:
            chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_DELETE, self)


class ChunkParamBase(ParamAttributes[ValueT], ChunkCommon):
    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.HORIZONTAL)

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED


class ChunkContainerBase(GroupAttributes, ChunkCommon):
    def delete_param_or_group(self, param: ChunkCommon):
        own_p = self.children.pop(param)

        if not own_p:
            return

        own_p.set_owner(None)
        chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_DELETE, own_p)

    def get_param(self, param: ChunkCommon | str) -> Optional[ChunkCommon]:
        """Get a parameter or group inside of this group

        Args:
            param (ChunkParamBase | str): A parameter to find. If string is passed, look for param with name

        Returns:
            ChunkParam: The found parameter or None if not found
        """

        if isinstance(param, ChunkCommon):
            return self.children.get_param(param)
        elif isinstance(param, str):
            return self.children.get_by_name(param)

    def get_params(self) -> Sequence[ChunkCommon]:
        """Get all parameters in this group

        Returns:
            List[ChunkParamBase]: All parameters in this group
        """
        return list(self.children.get_params())

    def get_my_params(self) -> Sequence[ChunkCommon]:
        """Get all parameters in this group

        Returns:
            List[ChunkParamBase]: All parameters in this group
        """
        return list(self.children.get_my_params())

    def get_last_param_change(self) -> Tuple[Tf.Notice | None, chunk_notice.ChunkNoticePropagator | None]:
        """Retreive the last changed parameter in this group. Will look inside nested groups as well to find parameter changes

        Returns:
            Tuple[Tf.Notice, ChunkParam]: Last changed ChunkParam and the notice associated with the change on this group or in nested group
        """
        return list(self.iter_changes())[-1]

    def check_hierarchy(self):
        failed_msg = f"check_hierarchy for {self.name} failed! \n"
        failed_msg += " Reason: {}"

        _, hierarchy = list(zip(*self.iter_hierarchy()))

        found_params: set[ChunkCommon] = set()
        found_names: set[str] = set()

        for p in hierarchy:
            if isinstance(p, ChunkAttributes):
                # Chunks (inherit from ChunkAttributes) do not need owners
                continue

            owner = p.get_owner()
            if isinstance(owner, NameSyncedContainer):
                owner = owner.get_owner()

            if not owner:
                raise TypeError(
                    failed_msg.format(f"{p.name} does not have an owner. All params except Chunks should have an owner")
                )

            if not isinstance(owner, chunk_notice.ChunkNoticePropagator):
                raise TypeError(failed_msg.format(f"{p.name}'s owner is not a ChunkNoticePropagator object"))

            if isinstance(owner, ChunkContainerBase):
                owner_has_child = p in owner.get_my_params()
                if not owner_has_child:

                    raise ValueError(
                        failed_msg.format(f"{p.name} has owner: {owner.name}. Owner does not hold this param")
                    )

            if p in found_params:
                raise ValueError(failed_msg.format(f"{p.name} is duplicated in the hierarchy"))
            found_params.add(p)

            if p.name in found_names:
                raise ValueError(failed_msg.format(f"param name: {p.name} is not unique in the hierarchy"))
            found_names.add(p.name)

        print(f"Hierarchy for Chunk {self.name} checked successfully")

    def print_hierarchy(self):
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
            if owner and isinstance(owner, ChunkContainerBase):
                msg += f". Owner = ({owner.name})"
            msg += "\n"

        print(msg)

    def iter_hierarchy(self, indent=0):
        yield indent, self
        for p in self.children.get_my_params():
            if isinstance(p, ChunkContainerBase):
                yield from p.iter_hierarchy(indent + 1)
            else:
                yield indent + 1, p


class ChunkGroupBase(ChunkContainerBase):
    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.GROUP_CHANGED

    def add_params(self, params: Iterable[ChunkT], force_unique: bool = True) -> List[ChunkT]:
        """Adds parameters to this group. Will call add_param on all params in the list

        Args:
            params (Iterable[ChunkCommon]): List of chunk objects to add to this gruo
            force_unique (bool, optional): Force unique params. Change with caution. Defaults to True.

        Returns:
            List[ChunkCommon]: A list of the added parameters. Will differ from the input params if copies were made
        """
        new_params = []
        for param in params:
            new_params.append(self.add_param(param, force_unique))
        return new_params

    def add_param(self, param: ChunkT, force_unique: bool = True) -> ChunkT:
        """Adds a parameter to this group. Connects the parameter change notices to also emit this group changed notice

        Args:
            param (ChunkParamBase): A parameter to add. Can also be another ChunkContainer, which will nest the group
        """

        new_param = None
        if force_unique:
            # Check and resolve duplicate params
            # new_param = self._resolve_new_param_or_group(param)
            new_param = self.copy_group_or_param(param)

        if new_param is None or new_param is param:
            # Param was not a duplicate, so can use the one passed by caller.
            # If the param was duplicated, callbacks are copied and resolved during resolve_new_param_or_group,
            # so we only need to set the default child->parent callback once
            new_param = param

        if isinstance(new_param, ChunkCommon):
            # self.params[new_param.name] = new_param
            # self.children.update_names()
            self.children.add(new_param)

        else:
            raise ValueError("Chunks only support params inherited from ChunkCommon")

        # new_param.set_owner(self)
        return new_param

    def copy_group_or_param(self, param: ChunkT, reserved_names: Optional[set[str]] = None) -> ChunkT:
        """Return a copy of a param or group that does not conflict with other child on this group

        Args:
            param (ChunkParamBase): A ChunkParam or ChunkContainer object to copy
            reserved_names (List[str], optional): A list of reserved names which the copy will respect. Defaults to None.

        Returns:
            ChunkParamBase: The copy of param with new name and updated connections
        """

        if not reserved_names:
            reserved_names = self.get_reserved_names()

        new_name = self.get_valid_child_name(param, reserved_names)
        reserved_names.add(new_name)

        new_param = copy.copy(param)
        new_param.name = new_name

        if isinstance(param, ChunkGroupBase):
            # Recorsively copy everything inside the group
            for p in param.children.get_my_params():
                p_copy = self.copy_group_or_param(p, reserved_names)

                # Since the copying of a group will not copy any nested params or groups, it's safe to add them back here
                new_param.add_param(p_copy)  # type: ignore

        return new_param

    def get_reserved_names(self) -> set[str]:
        """A combination of reserved param and group names

        Returns:
            List[str]: A list of reserved group and parameter names which new parameters should not be named
        """
        return set(self.children.get_names()) | set([self.name])

    def get_valid_child_name(self, param: ChunkCommon, reserved_names: set[str]) -> str:
        """Get a unique name for param or group to be added on this group

        Args:
            param (ChunkParam): a name that might not be unique
            reserved_names (List[str]): A list of reserved names. This paramater can't have a name which exist in this list. The new, valid name will be appended to this list
            to nest a group inside an already created group

        Returns:
            str: a unique name
        """

        orig_name = param.name

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


class ChunkVectorBase(ChunkContainerBase, VectorAttributes):
    """Base for vector style parameters. Function similar to a group but with fixed number of params"""

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        for i in range(self.length):
            np = copy.copy(self.param)
            # np.set_owner(self)
            if self.suffixes:
                np.name = self.suffixes[i]

            if isinstance(np, ChunkContainerBase):
                self.children.add(np)

    def __getitem__(self, key: str | int) -> Optional[ChunkCommon]:
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
        if isinstance(key, int):
            param = next(islice(self.children.get_params(), key, None), None)
            # param = self.params[key]

        if not param:
            raise KeyError(f"Index {key} does not exist on vector")

        return param

    def __setitem__(self, key: str | int, val: Any) -> Optional[ChunkCommon]:
        param = self[key]
        if not isinstance(param, ChunkParamBase):
            raise ValueError("ChunkContainers does not support value assignment")

        param.value = val

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED
