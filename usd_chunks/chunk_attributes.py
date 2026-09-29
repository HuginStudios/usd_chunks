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
import logging
import sys
import types
import weakref
from collections.abc import Callable
from typing import (Any, Generic, Optional,
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

_field_types: weakref.WeakKeyDictionary[type, dict[str, Any]] = weakref.WeakKeyDictionary()


def field_type(cls: type, attr: attrs.Attribute) -> Any:
    """The runtime type of an attrs field on `cls`, with a string annotation resolved.

    Every module here uses `from __future__ import annotations`, so attrs records each
    annotation as a string. Validation and enum conversion need the real type, so the
    string is evaluated in the module of the class that declared the field. Resolved once
    per class and cached.

    Args:
        cls: The class the field is read from.
        attr: The field.

    Returns:
        The type, or None when the annotation cannot be evaluated at runtime (a name
        imported only under TYPE_CHECKING), which callers treat as nothing to check.
    """
    resolved = _field_types.get(cls)
    if resolved is None:
        resolved = {f.name: _resolve_annotation(cls, f.name, f.type) for f in attrs.fields(cls)}
        _field_types[cls] = resolved
    return resolved.get(attr.name, None if isinstance(attr.type, str) else attr.type)


def _resolve_annotation(cls: type, name: str, annotation: Any) -> Any:
    """Evaluate one string annotation in the namespace of the class that declared it."""
    if not isinstance(annotation, str):
        return annotation
    for klass in cls.__mro__:
        if name in klass.__dict__.get("__annotations__", {}):
            module = sys.modules.get(klass.__module__)
            namespace = {**(vars(module) if module else {}), klass.__name__: klass}
            try:
                return eval(annotation, namespace)
            except Exception:
                return None
    return None


#: Per class: field name -> (attrs.Attribute, convert?, validate?), for __setattr__.
_setattr_fields: weakref.WeakKeyDictionary[type, dict[str, tuple]] = weakref.WeakKeyDictionary()

#: Per class: field name -> the notice alias its AttributeSender sends, for every field
#: that has one. See AttributesBase._attribute_sender.
_sender_aliases: weakref.WeakKeyDictionary[type, dict] = weakref.WeakKeyDictionary()


def _fields_for_setattr(cls: type) -> dict[str, tuple]:
    """What __setattr__ needs to know about each field of cls, worked out once per class.

    Every attribute set used to find its attrs field and read two metadata flags from
    scratch -- a third of a million times while opening one network.
    """
    found = _setattr_fields.get(cls)
    if found is None:
        found = {
            f.name: (f, f.metadata.get("convert", True), f.metadata.get("validate", True))
            for f in attrs.fields(cls)
        }
        _setattr_fields[cls] = found
    return found


def _sending_fields(cls: type) -> dict:
    """The fields of cls that send a notice when they change, with the alias each sends."""
    found = _sender_aliases.get(cls)
    if found is None:
        found = {}
        for name, attr in attrs.fields_dict(cls).items():
            if attr.metadata.get("send_mode", SendMode.SEND) is SendMode.NO_SEND:
                continue
            alias = attr.metadata.get("sender_alias", chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
            if alias:  # ChunkNoticeAlias.NONE sends nothing
                found[name] = alias
        _sender_aliases[cls] = found
    return found


def get_attr_index(cls, data):
    """The position `data` was first declared at in `cls`'s attrs fields, searching bases too.

    Args:
        cls: An attrs class.
        data: A field name.

    Returns:
        The field's index in the earliest base that declares it, or None.
    """
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


def add_validate_and_convert(cls: type[AttributesBase], fields: list[attrs.Attribute]):
    """An attrs field_transformer that orders __init__ arguments by first declaration.

    A field redeclared in a subclass keeps the position of its first declaration, so the
    generated __init__ matches the signature Pylance infers.

    Args:
        cls: The class being built.
        fields: Its attrs fields.

    Returns:
        The fields, sorted.
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
    """Metaclass that makes every chunk attributes class an attrs class.

    A metaclass rather than @attrs.define on each class, so all chunk classes share the
    same default behaviour.

    It also injects into attrs' __setattr__ so that every field runs _convert() and
    _validate(), and value_changed() is called after the attribute is set.
    """

    def __new__(cls, name, bases, attributes):
        """Build the class, wrap its __setattr__, and pass it through attrs.define."""
        res = super().__new__(cls, name, bases, attributes)

        # we use a custom __setattr__ on all subclasses for a couple of reasons:
        #   - We need the 'self' argument to the converter methods, which the old-style (pre attrs 24.1) converters does not support.
        #   - the on_setattr method is not called when first setting the attributes, meaning if we were to hook this to
        #     on_setattr, the _safe_convert() function is not called when first initializing the values
        #   - value_changed() should be called *after* __setattr__, which is seemingly not possible with on_setattr.

        setattr_override = attrs.setters.NO_OP

        def _wrapped_setattr(inst: AttributesBase, name, value):
            nonlocal res

            if name not in attributes:
                return super(res, inst).__setattr__(name, value)

            resval = value

            # First run converters and validators, with the field as res declares it -- the
            # class whose body named it, as attrs.fields(res) found it before this was cached.
            entry = _fields_for_setattr(res).get(name)
            if entry is None:
                return super(res, inst).__setattr__(name, resval)
            attrib, convert, validate = entry

            # Run conversion
            if convert:
                resval = inst._safe_convert(attrib, value)

            if validate:
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
    """A handle to one attribute on an AttributesBase, which sends a notice when it changes.

    attrs.Attribute cannot send through the Tf.Notice system. This class can: it is a
    ChunkNoticePropagator, so it can join a propagator hierarchy and send ATTRIBUTE_CHANGED
    whenever the value is modified. The value itself stays on the owner; see
    AttributesBase.value_changed.

    Args:
        owner (AttributesBase): The object owning the attribute
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
        self.old_value: ValueT | None = None

        # Joining does not dirty the owner: a sender is a handle on data the owner already
        # has, not new data. It is created on first use (AttributesBase._attribute_sender),
        # which is often the very change it is about to send -- dirtying here made a
        # presentation change, such as hiding a param, read as a real one.
        self.set_owner(owner, dirty=False)

    def get_notice_data(self) -> Self:
        """The notice data for an AttributeSender: the sender itself.

        A receiver can read everything the sender holds from notice.data::

            def callback(owner, notice, sender):
                val = notice.data.value
                old_val = notice.data.old_value

        Returns:
            Self: This sender
        """
        return self

    @property
    def value(self) -> ValueT | None:
        """The attribute's current value on the owner, or None once the owner is gone."""
        if not self.get_owner():
            return None
        return getattr(self.get_owner(), self.name)

    @value.setter
    def value(self, val: ValueT):
        """Set the attribute on the owner; a no-op once the owner is gone."""
        if not self.get_owner():
            return
        if not hasattr(self.get_owner(), self.name):
            logger.error("Failed to set attribute %s from AttributeSender", self.name)
            return

        setattr(self.get_owner(), self.name, val)

    def get_notice_alias(self) -> chunk_notice.ChunkNoticeAlias:
        """The alias this attribute's notices go out under."""
        return self.alias

    def is_blocked(self) -> bool:
        """Only blocked if blocked itself -- never through its owner.

        This notice is the record of a change: it is what renames the USD attribute, writes
        a value into it and refreshes the widget showing it. Blocking a chunk is about it not
        reacting -- not cooking, not running its callbacks -- and that is still enforced one
        step up, where the owner sends its own notice (a blocked param sends no
        PARAM_CHANGED). Inheriting the owner's block here would drop the record as well.

        Returns:
            Whether this sender itself is blocked.
        """
        return self._blocked

    def is_presentation_change(self) -> bool:
        """A change to the state attribute is presentation only unless it moved DISABLED.

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
            # The Scatter Brush is the example. Its radius, strength and mode steer a
            # *stroke*, which happens outside any cook -- `execute` never reads them, and what
            # downstream sees is the painted field, not the settings that produced it.
            # Dirtying the chain for them would make dragging Radius recook the scatter on
            # every tick.
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
        """Retrieve the UsdProperty that this attribute represents.

        Returns:
            Usd.Property | None: The UsdProperty of the attribute if it is bound, else None
        """
        # Asked of the owner rather than of a chunk_core type: chunk_core imports this
        # module, so naming ChunkUsdAttributeSupport here would be a cycle. AttributesBase
        # answers None and ChunkUsdAttributeSupport overrides it, so this file still knows
        # nothing about chunks, params or groups.
        if isinstance(owner := self.get_owner(), AttributesBase):
            return owner.get_usd_prop()
        return None


class CopyMode(IntEnum):
    """How a parameter's attributes are carried over when it is copied.

    The default is to create a copy; SHARE makes the copy reference the same resources.
    """

    COPY = 0  # copy.copy() attribute. This is default if nothing else is specified
    DEEPCOPY = 1  # copy.copy() attribute. This is default if nothing else is specified
    NO_COPY = 2  # Does not copy attribute, sets default instead
    SHARE = 3  # Share the the attribute with the source


class SendMode(IntEnum):
    """Whether an attribute sends a notice when it changes."""

    NO_SEND = 0
    SEND = 1


class AttributeMetadata(TypedDict, total=False):
    """Per-field settings, used as field(metadata=AttributeMetadata(...)).

    Attributes:
        copy_mode (CopyMode): How the attribute is treated when copying. Defaults to COPY.
        send_mode (SendMode): Whether the attribute sends a notice when modified. Defaults
            to SEND.
        sender_alias (chunk_notice.ChunkNoticeAlias): The alias to send the notice under.
            Defaults to ATTRIBUTE_CHANGED.
        validate (bool): Whether to validate this attribute's value when changed.
        convert (bool): Whether to convert this attribute's value to a valid value when
            changed.
        documentation (str): The documentation for this parameter, added to the
            parameter's description in the API docs.
    """

    copy_mode: CopyMode
    send_mode: SendMode
    sender_alias: chunk_notice.ChunkNoticeAlias
    validate: bool
    convert: bool
    documentation: str
    serialize: bool


class AttributesBase(chunk_notice.ChunkNoticePropagator, metaclass=AttributesMetaclass):
    """Base class for chunk dataclasses: validates and converts values, and sends change notices.

    Any modification to an attribute with `metadata["send_mode"] == SendMode.SEND` sends a
    notice of type metadata["sender_alias"]. That is done through AttributeSender, which
    holds a handle to the owner, the name of the attribute it represents, the alias of the
    notice to send, and the old value when available. Instances set themselves as owner on
    every AttributeSender they manage.

    AttributesMetaclass hooks every subclass into attrs, so each is treated as if it had
    been created with attrs.define. All fields use _validate() and _convert() as validator
    and converter, and any change made through __setattr__ calls value_changed() once the
    original __setattr__ has finished.
    """

    _failed_converters: dict[str, Any] = attrs.field(
        factory=dict,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False
        ),
    )

    _attr_senders: dict[str, AttributeSender] = attrs.field(
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

    _attrib_functions: dict[str, Callable[[Self, AttributeSender], Any]] = attrs.field(
        factory=dict,
        init=False,
        metadata=AttributeMetadata(
            copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND, validate=False, convert=False, serialize=True
        ),
    )
    _logger: logging.Logger | None = attrs.field(
        init=False,
        default=None,
        metadata=AttributeMetadata(copy_mode=CopyMode.NO_COPY, send_mode=SendMode.NO_SEND),
    )

    def __attrs_post_init__(self):
        """Initialize the propagator.

        Each notifying field gets its own AttributeSender -- a separate sender per attribute
        rather than one for the object -- but only when something first needs it: see
        _attribute_sender.
        """
        super().__init__()
        self._resolve_failed_converters()

    def _attribute_sender(self, name: str) -> AttributeSender | None:
        """The sender for field `name`, created the first time it is asked for.

        A sender is needed for two things only: something subscribing to the field
        (get_attribute_sender) and the field changing (value_changed). Creating one for
        every field of every param up front made about thirteen per param, each a full
        propagator with a node in the dependency graph -- the largest cost of opening a
        network, for fields that almost never change after they are built.

        Returns:
            The sender, or None for a field that sends nothing.
        """
        sender = self._attr_senders.get(name)
        if sender is None and (alias := _sending_fields(type(self)).get(name)) is not None:
            sender = AttributeSender(self, name, alias)
            self._attr_senders[name] = sender
        return sender

    def get_logger(self) -> logging.Logger:
        """A logger named after this class, created on first use."""
        if not self._logger:
            self._logger = logging.getLogger(f"usd_chunks.{self.__class__.__name__}")
        return self._logger

    def get_usd_prop(self) -> Usd.Property | None:
        """The USD property this object is bound to. Nothing, unless a subclass binds one.

        Returns:
            Usd.Property | None: Always None here; ChunkUsdAttributeSupport overrides it.
        """
        return None

    def clone(self) -> Self:
        """Create and return a copy of self.

        Most attributes are copied, but a field's metadata copy_mode (CopyMode) can change
        that. NoticeConnections are not copied, so in the following example only the first
        param ("slider1") triggers the callback to my_func::

            param = chunk.Slider("slider1")
            param.add_callback(my_func)

            my_chunk = chunk.Chunk("my_chunk")
            my_chunk.add_params([param, param.clone()])

        Returns:
            Self: A copy of self
        """
        return copy.copy(self)

    def __copy__(self) -> Self:
        """Create a new instance of this class and carry each field over by its copy mode.

        A field's metadata["copy_mode"] sets how it transfers to the copy. Members of a
        non-attrs class in the hierarchy are ignored, except ChunkNoticePropagator, which is
        initialized in AttributesBase.__attrs_post_init__().

        No __init__ is called on the copy. Instead the attrs hooks run, if present:
        __attrs_pre_init__ before the fields are copied and __attrs_post_init__ after. Any
        initialization of base classes belongs in one of those; ChunkNoticePropagator, for
        example, is initialized in AttributesBase.__attrs_post_init__().

        Returns:
            Self: A copy of self, with an updated uuid and rewired connections
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

    def get_attribute_sender(self, attribname: str) -> AttributeSender:
        """Get the sender for one attribute on this instance.

        The AttributeSender can be used to subscribe to changes of that attribute, either with
        ``attribute_sender.add_callback(some_method)`` or with
        ``chunk_param.add_callback(some_method, sender=attribute_sender)``.

        AttributeSenders are not preserved in copies, so with the first form only the first of
        these two params calls some_func when its value changes::

            c = chunk.Checkbox("some_name")
            c.get_attribute_sender("value").add_callback(some_func)
            my_chunk.add_params([c, c.clone()])

        Args:
            attribname (str): Name of the attribute

        Returns:
            AttributeSender: The AttributeSender mapped by attribname

        Raises:
            AttributeError: If the attribute sender was not found
        """
        if sender := self._attribute_sender(attribname):
            return sender
        raise AttributeError(f"Attribute sender not found {attribname}")

    def value_changed(self, attrname: str, old_value: Any, value: Any):
        """Send a notice for an attribute that changed; called after every attrs __setattr__.

        This must not mutate self, to avoid infinite recursion; its purpose is only to send
        notices for changed attributes. If the attribute sends notices, its AttributeSender
        (see _attribute_sender) sends the notice; otherwise self does. AttributesMetaclass injects the call into __setattr__.

        Args:
            attrname (str): The name of the attribute that was changed
            old_value (Any): The value before the change
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

        if sender := self._attribute_sender(attrname):
            sender.old_value = old_value
            sender.execute()
        else:
            self.execute()

    def set_function(self, attrname: str, method: Callable[[Self, AttributeSender], Any]):
        """Set a function that computes an attribute's value.

        The function's result becomes the attribute's new value. For example, a param's
        'value' attribute may evaluate to the product of two inputs::

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

        The slider's value then updates whenever a_param or b_param updates.

        Args:
            attrname (str): The name of the attribute that should use the function when evaluating
            method (Callable[[Self, AttributeSender], Any]): The function to run when evaluating
                the attribute. Its return value becomes the attribute's new value.
        """
        self._attrib_functions[attrname] = method

    def data_changed(self, notice: chunk_notice.ChunkNotice | None = None) -> chunk_notice.ChunkNotice | None:
        """Re-evaluate any attribute functions when an attribute, value or input changes.

        Args:
            notice: The notice being handled.

        Returns:
            The notice to propagate.
        """
        if not self._solving_attributes:
            # Run any attribute functions before continuing. Since calling __setattr__ calls value_changed, we need to disable
            # the notices to not get recursion. Only the first call to __setattr__ should send notices.
            if notice and notice.alias in [
                chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
                chunk_notice.ChunkNoticeAlias.VALUE_CHANGED,
                chunk_notice.ChunkNoticeAlias.INPUT_CHANGED,
            ]:
                for name, func in self._attrib_functions.items():
                    attr_sender = self._attribute_sender(name)

                    if attr_sender and func:
                        self._solving_attributes = True
                        res = func(self, attr_sender)
                        setattr(self, name, res)
                self._solving_attributes = False

        return super().data_changed(notice)

    def _safe_convert(self, attr: attrs.Attribute, val: Any) -> Any:
        """Wrap self.convert_attr, deferring any conversion that raises AttributeError.

        A failed conversion is kept in self._failed_converters and retried after __init__, so a
        client's custom convert_attr() need not worry about hasattr().

        Args:
            attr (attrs.Attribute): The attribute to check conversion for
            val (Any): The value to convert

        Returns:
            Any: The converted value if successful, otherwise the input val
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
        """Retry every deferred conversion by setting the attributes again.

        A conversion is deferred when convert_attr raises AttributeError because an attribute
        it depends on did not exist yet. chunk.Menu is an example: its value must convert to an
        entry of menu_items, but 'value' and 'default_value' are initialized before menu_items.
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
        """Convert a value being assigned to an attribute.

        Used like an attrs converter, but for every attribute of the instance, which makes it
        easy for a subclass to change how attributes convert. For example, with a field
        ``b: bool``, assigning ``inst.b = 2`` should convert 2 to True.

        Args:
            attr (attrs.Attribute): The attribute that was modified. Use attr.name to check the name of the modified attribute
            val (Any): The value which was set to the attribute. This should be the base of the conversion

        Returns:
            Any: A valid, possibly converted, attribute value.
        """
        return attrs.converters.default_if_none(attr.default)(val)

    def validate_attr(self, attr: attrs.Attribute, val: Any) -> bool:
        """Check that val is a supported value for attr before it is set.

        Args:
            attr (attrs.Attribute): The attribute being set
            val (Any): A value to check

        Returns:
            bool: True if the value is supported

        Raises:
            ValueError: The value's type does not match the field's generic type.
        """
        # Attempt to cast to the type hint where applicable
        if ftype := field_type(type(self), attr):
            t = get_origin(ftype)

            if t and (t is Generic or t is Union or t is Optional or t is types.UnionType):
                return True

            if t and not isinstance(val, t):
                raise ValueError(f"Validation failed. Tried to set value of type: {type(val)} to parameter {attr.name}")
        return True
