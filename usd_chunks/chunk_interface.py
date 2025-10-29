import copy
from itertools import chain
from typing import (
    Callable,
    Generator,
    List,
    Any,
    Dict,
    Protocol,
    Tuple,
    Type,
    TypeVar,
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
            self.param_changed_callback,
            chunk_notice.ChunkNoticeAlias.ATTRIBUTE_VALUE_CHANGED,
            self.param,
            chunk_item=self.param,
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
        self.name = name
        self.alias = alias
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
                continue

            # Storing the connections on the AttributeSender rather than on self, as _copy_connections() would
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
                new_conn = chunk_notice.NoticeConnection(conn.method, conn.alias, sender, conn.globally, self)
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
        copied.set_owner(self.get_owner())

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
        if old_value == value:
            # No change was made, we should not send the notice
            return

        if not attrname.startswith("_"):
            # Send a TfNotice for the changed attribute
            attr_sender = self._attr_senders.get(attrname)

            if not attr_sender:
                self.send_self()
            else:
                attr_sender.send_self()

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


class GroupAttributes(AttributesBase):
    """Holds attributes specific to groups"""

    _params: List["ChunkParamBase"] = attrs.field(
        init=False, factory=list, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY)
    )
    _groups: List["ChunkContainerBase"] = attrs.field(
        init=False, factory=list, metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY)
    )

    widget_factory: Optional[ChunkWidgetFactoryBase] = attrs.field(
        default=None,
        validator=attrs.validators.optional(attrs.validators.instance_of(ChunkWidgetFactoryBase)),
        metadata=AttributeMetadata(copy_mode=CopyMode.SHARE),
    )


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
        print("DELETE: ", self.name, owner.name if owner else None)
        # if owner and isinstance(owner, ChunkContainerBase):
        #     owner.delete_param_or_group(self)
        # else:
        #     chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_DELETE, self)


class ChunkParamBase(ParamAttributes[ValueT], ChunkCommon):
    layout: ChunkFlags.LayoutFlags = attrs.field(default=ChunkFlags.LayoutFlags.HORIZONTAL)

    def get_notice_alias(self):
        return chunk_notice.ChunkNoticeAlias.PARAM_CHANGED


class ChunkContainerBase(GroupAttributes, ChunkCommon):
    def delete_param_or_group(self, param: ChunkCommon):
        if isinstance(param, ChunkContainerBase):
            try:
                idx = self._groups.index(param)
            except IndexError:
                return
            self._groups.pop(idx)

        elif isinstance(param, ChunkParamBase):
            try:
                idx = self._params.index(param)
            except IndexError:
                return
            self._params.pop(idx)
        else:
            return

        param.set_owner(None)

        chunk_notice.send_notice(chunk_notice.ChunkNoticeAlias.CHUNK_DELETE, param)

    def get_param(self, param: ChunkParamBase | str) -> Optional[ChunkParamBase]:
        """Get a parameter or group inside of this group

        Args:
            param (ChunkParamBase | str): A parameter to find. If string is passed, look for param with name

        Returns:
            ChunkParam: The found parameter or None if not found
        """

        if isinstance(param, ChunkParamBase):
            return next((i for i in self.get_params() if i == param), None)
        if isinstance(param, str):
            return next((i for i in self.get_params() if i.name == param), None)

    def find_param(self, param: ChunkParamBase | str) -> Optional[ChunkParamBase]:
        """Similar to get_param, but searches in nested groups

        Args:
            param (ChunkParam | str): A parameter or group to find. If string is passed, look for params with name

        Returns:
            ChunkParam: The found parameter or None if not found
        """
        if isinstance(param, ChunkParamBase):
            return next((i for i in self.iter_all_params() if i == param), None)
        if isinstance(param, str):
            return next((i for i in self.iter_all_params() if i.name == param), None)

    def get_params(self) -> List[ChunkParamBase]:
        """Get all parameters in this group

        Returns:
            List[ChunkParamBase]: All parameters in this group
        """
        return self._params

    def iter_all_params(self) -> Generator[ChunkParamBase, None, None]:
        """Get all parameters in this group and in nested groups

        Yields:
            Generator[ChunkParam]: A Generator of all parameters, including from nested groups, contained within this group
        """
        for p in self.get_params():
            yield p

        for group in self._groups:
            yield from group.iter_all_params()

    def get_group(self, group: Union["ChunkContainerBase", str]) -> Optional["ChunkContainerBase"]:
        """Get a group inside of this group

        Args:
            group (ChunkContainer | str): A group to find. If string is passed, look for group with name

        Returns:
            ChunkContainer: The found group or None if not found
        """
        if isinstance(group, ChunkContainerBase):
            return next((i for i in self.get_groups() if i == group), None)
        if isinstance(group, str):
            return next((i for i in self.get_groups() if i.name == group), None)

    def find_group(self, group: Union["ChunkContainerBase", str]) -> Optional["ChunkContainerBase"]:
        """Similar to get_group, but also searches in nested groups

        Args:
            group (ChunkContainer | str): A group to find. If string is passed, look for group with name

        Returns:
            ChunkContainer: The found group or None if not found
        """

        if isinstance(group, ChunkContainerBase):
            return next((i for i in self.iter_all_groups() if i == group), None)
        if isinstance(group, str):
            return next((i for i in self.iter_all_groups() if i.name == group), None)

    def get_groups(self) -> List["ChunkContainerBase"]:
        """Get all groups on this group, excluding nested

        Returns:
            List[ChunkContainer]: Groups contained in this group. Not including nested groups
        """
        return self._groups

    def iter_all_groups(self) -> Generator["ChunkContainerBase", None, None]:
        """Get all groups in this group including nested groups

        Yields:
            Generator[ChunkContainer]: All groups, including from nested groups, in this group
        """

        for g in self.get_groups():
            yield g
            yield from g.iter_all_groups()

    def get_last_param_change(self) -> Tuple[Tf.Notice | None, ChunkParamBase | None]:
        """Retreive the last changed parameter in this group. Will look inside nested groups as well to find parameter changes

        Returns:
            Tuple[Tf.Notice, ChunkParam]: Last changed ChunkParam and the notice associated with the change on this group or in nested group
        """
        notice, sender = self.get_last_change()
        while isinstance(sender, ChunkGroupBase):
            notice, sender = sender.get_last_change()
        return notice, sender

    def get_reserved_param_names(self) -> List[str]:
        """Get a list of parameter names on this and nested groups. Any new parameter should not have a duplicate name

        Returns:
            List[str]: The reserved parameter names on this group
        """
        return [x.name for x in self.iter_all_params()]

    def get_reserved_group_names(self):
        """Get a list of group names on this and nested groups. Any new group should not have a duplicate name

        Returns:
            List[str]: The reserved group names on this group
        """
        return [self.name] + [x.name for x in self.iter_all_groups()]

    def get_reserved_names(self):
        """A combination of reserved param and group names

        Returns:
            List[str]: A list of reserved group and parameter names which new parameters should not be named
        """
        return list(chain(self.get_reserved_group_names(), self.get_reserved_param_names()))

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
            if not owner:
                raise TypeError(
                    failed_msg.format(f"{p.name} does not have an owner. All params except Chunks should have an owner")
                )

            if not isinstance(owner, ChunkContainerBase):
                raise TypeError(failed_msg.format(f"{p.name}'s owner is not a ChunkContainerBase object"))

            owner_has_child = p in chain(owner.get_params(), owner.get_groups())
            if not owner_has_child:
                raise ValueError(failed_msg.format(f"{p.name} has owner: {owner.name}. Owner does not hold this param"))

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
        for p in self.get_params():
            yield indent + 1, p

        for g in self.get_groups():
            yield from g.iter_hierarchy(indent + 1)


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

        if isinstance(new_param, ChunkContainerBase):
            self._groups.append(new_param)
        elif isinstance(new_param, ChunkParamBase):
            self._params.append(new_param)
        else:
            raise NotImplementedError("Chunks only support params of type ChunkContainer or ChunkParam")

        new_param.set_owner(self)
        return new_param

    # def _resolve_new_param_or_group(self, param: ChunkT) -> ChunkT:
    #     """Before adding a new param or group, need to validate that the same instance of the param is not already
    #     added to this group. We also need to make sure there are no duplicate names.
    #     If a duplicate instance is found, this function will copy or swap the paramaeter/group with
    #     a valid instance. The valid instance will hava a new name and uuid

    #     Args:
    #         param (ChunkParamBase): A parameter or group that should be added to this group

    #     Returns:
    #         ChunkParamBase: The originally passed param if no duplication was found, otherwise an altered param
    #     """

    #     # TODO: Currently kinda nasty lookups to achieve this - consider caching in the future, or
    #     # rethink some of this... A parameter could technically only care for sibling parameters with same name
    #     # and thus simply always just copy the new parameter, as the group holds the list of params.
    #     # But in order to solve conflicts accross the entire hierarchy, we also need to resolve nested
    #     # parameters on this and any new group

    #     # old_owner = param.get_owner()
    #     # if old_owner:
    #     return self.copy_group_or_param(param)

    #     # reserved_names = self.get_reserved_names()
    #     # all_params = list(self.iter_all_params())

    #     # # Need to resolve duplicate params and groups differently, since a group will likely have nested params,
    #     # # We need to search for those as well when adding a group
    #     # if isinstance(param, ChunkParamBase):
    #     #     if param in all_params:
    #     #         # Param exists on this group already, return a copy of the new param
    #     #         return self.copy_group_or_param(param)

    #     # elif isinstance(param, ChunkContainerBase):
    #     #     all_groups = set(self.iter_all_groups())

    #     #     if param in all_groups:
    #     #         # This is a duplicate group. Copy the entire group to resolve conflicts
    #     #         return self.copy_group_or_param(param)

    #     #     # Need to resolve any nested parameters inside the new group
    #     #     for p in param.iter_all_params():
    #     #         if p in all_params:
    #     #             parent = p.get_owner()
    #     #             if parent and isinstance(parent, ChunkGroupBase):
    #     #                 new_p = self.copy_group_or_param(p, reserved_names)
    #     #                 parent.replace_param(p, new_p)

    #     #         elif p.name in reserved_names:
    #     #             # Did not find param with the same hash, but duplicate names. Only resolve the name
    #     #             p.name = self.get_valid_child_name(p, reserved_names)

    #     #     # Also resolve nested groups
    #     #     for g in param.iter_all_groups():
    #     #         if g in all_groups:
    #     #             parent = g.get_owner()
    #     #             if parent and isinstance(parent, ChunkGroupBase):
    #     #                 new_g = self.copy_group_or_param(g, reserved_names)
    #     #                 parent.replace_group(g, new_g)

    #     #         elif g.name in reserved_names:
    #     #             # Did not find param with the same hash, but duplicate names. Only resolve the name
    #     #             g.name = self.get_valid_child_name(g, reserved_names)

    #     # if param.name in reserved_names:
    #     #     # The new group is not a duplicate but shares name with an already added group
    #     #     param.name = self.get_valid_child_name(param, reserved_names)

    #     # return param

    def copy_group_or_param(self, param: ChunkT, reserved_names: Optional[List[str]] = None) -> ChunkT:
        """Return a copy of a param or group that does not conflict with other parameters or groups on this group

        Args:
            param (ChunkParamBase): A ChunkParam or ChunkContainer object to copy
            reserved_names (List[str], optional): A list of reserved names which the copy will respect. Defaults to None.

        Returns:
            ChunkParamBase: The copy of param with new name and updated connections
        """

        if reserved_names is None:
            reserved_names = self.get_reserved_names()

        new_name = self.get_valid_child_name(param, reserved_names)
        new_param = copy.copy(param)
        new_param.name = new_name

        if isinstance(param, ChunkGroupBase):
            # Recorsively copy everything inside the group
            for p in chain(param.get_groups(), param.get_params()):
                p_copy = self.copy_group_or_param(p, reserved_names)

                # Since the copying of a group will not copy any nested params or groups, it's safe to add them back here
                new_param.add_param(p_copy)  # type: ignore

        return new_param

    def replace_param(self, old_param: ChunkParamBase, new_param: ChunkParamBase):
        """Replace a parameter on this group with another parameter

        Args:
            old_param (ChunkParam): The old param to replace
            new_param (ChunkParam): The new param to replace old_param with
        """
        try:
            idx = self._params.index(old_param)
        except ValueError:
            print("Trying to swap a parameter that does not exist on this group")
            return

        # Set self as parent
        new_param.set_owner(self)

        # Do the swap
        self._params[idx] = new_param

        # Need to update connections
        conns = self.find_connections(sender=old_param)
        for conn in conns:
            conn.connect(sender=self._params[idx])

    def replace_group(self, old_group: "ChunkContainerBase", new_group: "ChunkContainerBase"):
        """Replace a nested group on this group with a new group

        Args:
            old_group (ChunkContainer): The old group to replace
            new_group (ChunkContainer): The new group to replace old_group with
        """
        try:
            idx = self._groups.index(old_group)
        except ValueError:
            print("Trying to swap a group that does not exist on this group")
            return

        # Set self as parent
        new_group.set_owner(self)

        # Do the swap
        self._groups[idx] = new_group

        # # Need to update connections
        conns = self.find_connections(sender=old_group)
        for conn in conns:
            conn.connect(sender=self._groups[idx])

    def get_valid_child_name(self, param: ChunkCommon, reserved_names: List[str]) -> str:
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

        while orig_name and orig_name[-1].isdigit():
            suffix = suffix + orig_name[-1]
            orig_name = orig_name[:-1]

        idx = int(suffix) if suffix else 1
        max_iter = 100

        name = orig_name + suffix

        while name in reserved_names:
            name = orig_name + str(idx)
            idx += 1
            if idx > max_iter:
                raise RuntimeError(f"Failed to resolve name, loop exceeded {max_iter} iterations")

        reserved_names.append(name)
        return name


class ChunkVectorBase(ChunkContainerBase, VectorAttributes):
    """Base for vector style parameters. Function similar to a group but with fixed number of params"""

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        for i in range(self.length):
            np = copy.copy(self.param)
            np.set_owner(self)
            if self.suffixes:
                np.name = self.suffixes[i]

            if isinstance(np, ChunkContainerBase):
                self._groups.append(np)
            else:
                self._params.append(np)

    def __getitem__(self, key: str | int) -> Optional[ChunkCommon]:
        """Allows for Vector[0] or Vector['paramName'] access to the vector values

        Args:
            key (str | int): name or index of the requested value parameter

        Raises:
            KeyError: if key is not accessible

        Returns:
            Optional[ChunkCommon]: Param if found
        """
        if isinstance(self.param, ChunkContainerBase):
            if isinstance(key, str):
                param = self.get_group(key)
            if isinstance(key, int):
                param = self._groups[key]
        else:
            if isinstance(key, str):
                param = self.get_param(key)
            if isinstance(key, int):
                param = self._params[key]

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
