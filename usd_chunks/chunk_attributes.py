"""The attribute layer: declared attributes, their metadata, and change notification.

`AttributesBase` and its metaclass give a chunk object attributes that announce their own
changes -- which is what makes a param a param rather than a value in a dict. `AttributeSender`
is the notification half, `AttributeMetadata` describes one attribute, and CopyMode/SendMode
say what happens when it is copied or when it fires.

Lifted out of `chunk_core` because it is the mechanism, not the model: nothing here knows
what a chunk, a param or a group is. The classes that DO -- ChunkCommon, ChunkParamBase,
ChunkUsdAttributeSupport -- import from here, and the dependency runs one way.

`ChunkUsdAttributeSupport` deliberately stayed behind despite being attribute machinery. It
reaches for ChunkVectorBase, so moving it would only relocate the coupling, the same way
ChunkContainerWidgetBase had to stay with the model it presents.
"""
from __future__ import annotations

import copy
import inspect
import logging
from typing import (Any, Callable, Dict, Generic, List, Optional,
                    TypeVar, TypedDict, Union, dataclass_transform, get_origin)

import attrs
from attr import _make as attrMake

from pxr import Usd
from typing_extensions import Self

from usd_chunks import ATTRS_VERSION, chunk_notice
from usd_chunks.chunk_flags import ChunkFlags

from enum import IntEnum

# The same tuple comparison chunk_core uses. A string compare would rank
# "25.10" below "25.4", which is exactly the version where this starts to matter.
_ATTRS_SUPPORT_INSPECT = ATTRS_VERSION >= (25, 4)  # type: ignore

logger = logging.getLogger(__name__)

ValueT = TypeVar("ValueT")


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
        # Find the first occurrence of the attribute in the inheritance hierarchy, and use the index of the
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

    This metaclass also injects some functions in to the default attrs __setattr__ in order to use out custom validation and conversion functions:
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

    A valued parameter stores the value of the attributes itself, this class is a handle to those attributes but with the
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
        The notice data for AttributeSender's is simply self. A receiver of a notice may use this
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

    def is_blocked(self) -> bool:
        """Only blocked if blocked itself -- never through its owner.

        This notice is the record of a change: it is what renames the USD attribute, writes
        a value into it and refreshes the widget showing it. Blocking a chunk is about it not
        reacting -- not cooking, not running its callbacks -- and that is still enforced one
        step up, where the owner sends its own notice (a blocked param sends no
        PARAM_CHANGED). Inheriting the owner's block here dropped the record as well.
        """
        return self._blocked

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
            # A param can declare that nothing downstream computes from its value. Rare, and
            # it has to be earned: the value must genuinely not reach the chunk's cook.
            #
            # The Scatter Brush is the case that asked for it. Its radius, strength and mode
            # steer a *stroke*, which happens outside any cook -- `execute` never reads them,
            # and what downstream sees is the painted field, not the settings that produced
            # it. Dirtying the chain for them made dragging Radius recook the scatter on every
            # tick, reported as the brush being unusably sticky.
            #
            # The change still solves and still sends its notice, so callbacks and the UI are
            # unaffected; only the dirtying of readers is skipped.
            if self.name == "value" and getattr(self.get_owner(), "presentation_only", False):
                return True
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
        # Imported here, not at the top: ChunkUsdAttributeSupport lives in chunk_core,
        # which imports this module. A module-level import would be a cycle. This is the
        # only place the attribute layer reaches back into the model -- everything else in
        # this file knows nothing about chunks, params or groups.
        from usd_chunks.chunk_core import ChunkUsdAttributeSupport

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
    parameter. The default is to create a copy, but if the parameters should be references (share resources),
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
        # get_notice_alias_for_attr in order to have a separate sender for each attribute
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

        # No blocked check here. A blocked chunk (outside the active view's tree) must not
        # react to a change, but the change itself still has to be recorded: dropping it at
        # this point would lose renames and values for good -- the UI would keep the old
        # name, the prim the old attribute and value, and the next save would write them
        # back. Whether
        # anything reacts is decided further on, by the owner's own notice (see
        # AttributeSender.is_blocked).

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
        # Attempt to cast to the type hint where applicable
        if attr.type:
            t = get_origin(attr.type)

            if t and (t is Generic or t is Union or t is Optional):
                return True

            if t and not isinstance(val, t):
                raise ValueError(f"Validation failed. Tried to set value of type: {type(val)} to parameter {attr.name}")
        return True
