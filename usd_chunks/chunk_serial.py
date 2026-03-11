import codecs
import importlib
import inspect
import json
import logging
import pickle
import sys
import types
import weakref
import zlib
import os
from functools import partial
from importlib import util
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, overload

import attrs

logger = logging.getLogger(__name__)

## These are placeholders waiting for a better implementation
COMPRESS = False
PICKLE_ENABLED = False
SAVE_MODULES = True


@attrs.define
class SerializedObject:
    pass


@attrs.define
class SerializedClass(SerializedObject):
    module: str = attrs.field()
    name: str = attrs.field()
    hashval: int = attrs.field()
    attributes: Dict[str, Any] = attrs.field(factory=dict)
    kwargs: Dict[str, Any] = attrs.field(factory=dict)

    def __hash__(self) -> int:
        return self.hashval

    @classmethod
    def from_class(cls, obj, glb):
        if obj is None:
            return None

        res = SerializedClass(obj.__module__, type(obj).__qualname__, hash(obj))

        fields = {}
        try:
            fields = attrs.fields_dict(type(obj))
        except attrs.exceptions.NotAnAttrsClassError:
            pass

        # Attributes of the object will be stored on the SerializedClass
        if hasattr(obj, "__dict__"):
            attributes = obj.__dict__.copy()
        else:
            attributes = {x: getattr(obj, x) for x in dir(obj) if hasattr(obj, x)}

        # In order to recreate the class, we must find the __init__ signature. These go in to the res.kwargs
        # member in order for the decoder to call __init__ on the class
        for i in inspect.signature(obj.__init__).parameters.values():
            if not i.default or i.default is inspect._empty:
                if i.kind in [i.POSITIONAL_ONLY, i.POSITIONAL_OR_KEYWORD]:
                    res.kwargs[i.name] = None

            if i.default is inspect._empty:
                continue

            val = i.default
            if isinstance(obj, Serializeable):
                val = obj.serialize_kwarg(i.name, val)
            else:
                val = default_serialize_kwarg(i.name, val)

            if serial := serialize_Reference(val, glb):
                val = attrs.asdict(serial, recurse=False)
            res.kwargs[i.name] = val

        for name, val in filter(lambda x: not (inspect.ismethod(x[1]) or inspect.isfunction(x[1])), attributes.items()):
            # Loop over all attributes and set any necessary res.kwargs or res.attributes. All dunder methods are ignored.
            # Attributes will be checked by 'default_should_serialize_attribute' if class is not a subclass of Serializeable;
            # else, the attribute is checked in 'obj.should_serialize_attribute'. By default, all attributes are serialized, however, a
            # subclass of Serializeable may override what attributes should be serialized.
            if name.startswith("__"):
                continue

            if isinstance(obj, Serializeable):
                sa, val = obj.should_serialize_attribute(name, val)
            else:
                sa, val = default_should_serialize_attribute(name, val)

            if field := fields.get(name):
                # For attrs classes, we can get the field and check if the field metadata excplicitly sets 'serialize'. This can be used as
                # a nicer way to specify what attributes should be serialized, instead of overriding the 'should_serialize_attribute'
                sa = field.metadata.get("serialize", sa)

            if not sa:
                continue

            if isinstance(val, weakref.ReferenceType):
                val = val()

            if serial := serialize_Reference(val, glb):
                # if the value itself point to an object that is already added to glb, we store the value as a SerializedReference, rather
                # than a duplication of the object
                val = attrs.asdict(serial, recurse=False)

            # For attrs classes, where the attribute has a field specified - the attribute is instead passed to the SerializedClass's kwargs dict
            # in order to pass the correct value when decoding and calling __init__. In the case of a non-attrs class, this may become troublesome.
            # For example, in this case, the res.kwargs will be {'bar': None}, and res.attributes will be {'bar': 'hello', 'baz': 10}:
            #
            #    class Foo:
            #        def __init__(self, bar):
            #            self.bar = "hello"
            #            self.baz = bar
            #            self.do_something(bar)

            #         def do_something(self, val):
            #            print(val)
            #
            #    b = Foo(10)
            #
            # The decoder will then call b = Foo(None); setattr(b, 'bar', "hello"); setattr(b, 'baz', 10), ... on all attributes. But the call to do_something()
            # will not be as expected, as Foo is called with None.
            #
            # One could decide to instead 'eval' the class's repr, which could solve this specific problem, but would lead to to complications elsewhere.
            # For now, the better solution is to subclass Serializeable, and override serialize_kwarg() to retrun self.baz when evaluating the 'bar' parameter:
            #
            #    class Foo(Serializeable):
            #        def __init__(self, bar):
            #            self.bar = "hello"
            #            self.baz = bar
            #            self.do_someting(bar)
            #         ...
            #
            #         def serialize_kwarg(self, name: str, default: Any):
            #             if name == "bar":
            #                 return self.baz
            #             return super().serialize_kwarg(name, default)
            #
            # This will result in Foo(10), when decoding the class

            if field and field.init:
                res.kwargs[name] = val
            else:
                res.attributes[name] = val
        return res


@attrs.define
class PickledObject(SerializedObject):
    stream: str = attrs.field()


@attrs.define
class SerializedReference(SerializedObject):
    reference: int = attrs.field()


class RuntimeModule(types.ModuleType):
    def __init__(self, code: str, name: str, doc: str | None = None) -> None:
        super().__init__(name, doc)
        self._code = code


@attrs.define
class SerializedModule(SerializedObject):
    code: str = attrs.field()
    module_name: str = attrs.field()

    def create(self):
        return RuntimeModule(self.code, self.module_name)


@attrs.define
class SerializedMethod(SerializedObject):
    reference: int = attrs.field()
    name: str = attrs.field()


@attrs.define
class SerializedFunction(SerializedObject):
    module: str = attrs.field()
    name: str = attrs.field()


@attrs.define
class SerializedFailed(SerializedObject):
    cnstr: Optional[str] = attrs.field(default=None)
    failed: bool = attrs.field(default=True)


class Serializeable:
    """
    A base class for serialization. It is not required for classes to subclass this in order to be serialized,
    however, this provides some functionality that can be used to serialize an object properly
    """

    __slots__ = ()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def post_serial(self, owner: Any):
        """
        Called after an object was created as an attribute by an owning object. By default, this is noop

        Args:
            owner (Any): The object that was responsible for creating this object when decoding
        """
        pass

    def post_serial_setattr(self, name: str, value: Any) -> None:
        """
        Called on the decoded object for setting an attribute. This can be used when the attribute need to be
        evaluatated or otherwise modified before setattr. It may also be useful for itermediate attributes, where
        the SerializedClass holds an attribute that should not be set excplicitly. For example, a ChunkNoticePropagator
        serialize function sets the 'dependencies' attribute, but interprets it by calling set_dependency rather than
        setattr()

        Args:
            name (str): The name of the attribute from the SerializedClass
            value (Any): The decoded value to set
        """
        default_post_serial_setattr(self, name, value)

    def serialize_kwarg(self, name: str, default: Any) -> Any:
        """
        Before serializeation, the encoder may serialize this argument to __init__. The returned value
        is the value that will be passed to __init__ when decoding.

        Args:
            name (str): The name of the argument
            default (Any): The input default value to the argument

        Returns:
            Any: A serializeable value that will be decoded and passed to __init__ when decoding this class
        """
        return default_serialize_kwarg(name, default)

    def should_serialize_attribute(self, name: str, value: Any) -> Tuple[bool, Any]:
        """
        Before serializeation, the encoder checks if this attribute should be serialized. The returned tuple
        holds whether the attribute should be serialized, and the value. The returned value of the tuple may
        be a modification of the input value

        Args:
            name (str): The name of the attribute
            value (Any): The input value

        Returns:
            Tuple[bool, Any]: A tuple (serialize, value). If the first entry of the tuple is False, the attribute is ignored when serialized
        """
        return default_should_serialize_attribute(name, value)

    def serialize(self, glb) -> Optional[SerializedClass | SerializedReference]:
        """
        Serialize this object. By default, this calls the serialize_object(self, glb). A subclass should call super().serialize(glb) and modify the result.
        The returned value should be either a SerializedClass or SerializedReference

        Args:
            glb (Dict[str, Any]): A dict data containing all serialized objects. This is used for creating references

        Returns:
            Optional[SerializedClass | SerializedReference]: The result SerializedClass or SerializedReference
        """
        return serialize_object(self, glb)


def default_should_serialize_attribute(name: str, value: Any) -> Tuple[bool, Any]:
    return (True, value)


def default_post_serial_setattr(obj, name: str, value: Any) -> None:
    setattr(obj, name, value)


def default_serialize_kwarg(name: str, default: Any) -> Any:
    if type(default).__name__ not in __builtins__ and default is not None:
        return attrs.asdict(SerializedFailed(repr(default)))
    return default


@overload
def serialize_object(obj: Serializeable, glb: dict) -> Optional[SerializedClass | SerializedReference]: ...


@overload
def serialize_object(obj: types.ModuleType, glb: dict) -> Optional[SerializedModule]: ...


def serialize_object(obj: Any, glb: dict) -> Optional[SerializedObject]:
    if serial := serialize_Reference(obj, glb):
        # If the object exists in glb, reuturn a reference SerializedReference rather than creating an
        # identical class
        return serial

    if SAVE_MODULES and isinstance(obj, (types.ModuleType, RuntimeModule)):
        if serial := serialize_Module(obj):
            return serial

    # Create a SerializedClass representation of the object
    if res := SerializedClass.from_class(obj, glb):
        glb[res.hashval] = res

    return res


def serialize_Reference(ref, glb) -> SerializedReference | None:
    if type(ref).__name__ in __builtins__:
        return None
    try:
        if (h := hash(ref)) and h in glb:
            return SerializedReference(h)
    except TypeError:
        pass

    return None


def serialize_Function(func: Callable) -> SerializedFunction | SerializedMethod:
    if inspect.ismethod(func):
        return SerializedMethod(hash(func.__self__), func.__name__)
    return SerializedFunction(func.__module__, func.__qualname__)


def serialize_Module(mod: types.ModuleType) -> SerializedModule | None:
    try:
        if isinstance(mod, RuntimeModule):
            code = mod._code
        else:
            code = inspect.getsource(mod)

        return SerializedModule(code, mod.__name__)
    except OSError:
        return None


class ChunkEncoder(json.JSONEncoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.glb = {}

    def default(self, o: Any):
        if isinstance(o, weakref.ReferenceType) and (res := serialize_Reference(o(), self.glb)):
            return attrs.asdict(res, recurse=False)

        if isinstance(o, Serializeable) and (res := o.serialize(self.glb)):
            return attrs.asdict(res, recurse=False)

        is_cb = callable(o)
        if PICKLE_ENABLED and is_cb and not inspect.ismethod(o):
            return attrs.asdict(PickledObject(codecs.encode(pickle.dumps(o), "base64").decode()), recurse=False)

        if is_cb and (res := serialize_Function(o)):
            return attrs.asdict(res, recurse=False)

        elif not is_cb and (res := serialize_object(o, self.glb)):
            return attrs.asdict(res)
        try:
            res = super().default(o)
        except Exception:
            logger.exception(f"Failed to encode: {o}")
            return attrs.asdict(SerializedFailed())
        return o


def serialize_json(chunks: Sequence[Serializeable]):
    return json.dumps(chunks, cls=ChunkEncoder, indent=4)


def serialize_py(mod: types.ModuleType):
    return inspect.getsource(mod)


def file_extensions():
    return (".json", ".chunk", ".chum", ".py")


def save_file(serial: str, file):
    if not serial:
        return

    _, ext = os.path.splitext(file)
    if ext not in file_extensions():
        return

    if COMPRESS:
        compressed = zlib.compress(serial.encode())
        with open(file, "wb") as f:
            f.write(compressed)
    else:
        with open(file, "w") as f:
            f.write(serial)


def load_file(file):
    _, ext = os.path.splitext(file)
    if ext not in file_extensions():
        return

    with open(file, "rb") as f:
        serial = f.read()

    try:
        return zlib.decompress(serial)
    except zlib.error:
        return serial


def import_module(module_name):
    if mod := sys.modules.get(module_name):
        return mod

    importlib.invalidate_caches()
    spec = util.find_spec(module_name)
    if not spec:
        return

    module = util.module_from_spec(spec)
    if not module or not spec.loader:
        return

    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return sys.modules[module_name]


def get_module_attrib(glb, mod_name, attr_name):
    if not mod_name:
        return None

    if mod := glb.get(mod_name):
        try:
            return getattr(mod, attr_name)
        except Exception:
            pass

    if not (module := import_module(mod_name)):
        return None

    glb[mod_name] = module
    try:
        return getattr(module, attr_name)
    except AttributeError:
        logger.error(f"Could not find attribute {attr_name} in module {mod_name}")
    except ModuleNotFoundError:
        pass


class TemporaryObject:
    def __init__(self, refs: Set, data, glb) -> None:
        self.data = data
        self.refs = refs
        self.glb = glb

    def __repr__(self) -> str:
        return f"TemporaryObject({self.refs})"

    def construct(self, glb):
        if glb is not None:
            self.glb.update(glb)

        res = decodeCallable(self.glb, self.data)
        if isinstance(res, TemporaryObject):
            logger.warning(f"Could not decode data from {self.data}")
            return None
        return res


class ClassConstructor:
    def __init__(self, data, glb, serialized: SerializedClass):
        self.data = data
        self.glb = glb
        self.serial = serialized

    def _cnstr_val(self, val, glb):
        if isinstance(val, (ClassConstructor, TemporaryObject)):
            return val.construct(glb=glb)

        if isinstance(val, dict):
            res = {}
            for _k, _v in val.items():
                res[_k] = self._cnstr_val(_v, glb)
            return res

        if isinstance(val, list):
            res = []
            for _v in val:
                res.append(self._cnstr_val(_v, glb))
            return res
        return val

    def construct(self, post_constuct=True, glb=None):
        # if glb is None:
        # glb = self.glb
        if glb is not None:
            self.glb.update(glb)

        kwargs = {}
        for key, val in self.serial.kwargs.copy().items():
            kwargs[key] = self._cnstr_val(val, self.glb)

        inst = self.glb.get(self.serial.hashval)
        if not inst:
            try:
                if not (cls := get_module_attrib(self.glb, self.serial.module, self.serial.name)):
                    return None
                inst = cls(**kwargs)
            except TypeError:
                return

            self.glb[self.serial.hashval] = inst

        if post_constuct:
            self.post_constuct()

        return inst

    def post_constuct(self):
        if not (inst := self.glb.get(self.serial.hashval)):
            return

        for key, val in self.serial.attributes.items():
            resval = self._cnstr_val(val, self.glb)

            if isinstance(resval, Serializeable):
                resval.post_serial(inst)

            if isinstance(inst, Serializeable):
                inst.post_serial_setattr(key, resval)
            else:
                default_post_serial_setattr(inst, key, resval)

        return inst


def decodeCallable(glb, data):
    try:
        serial = SerializedMethod(**data)
        if obj := glb.get(serial.reference):
            return getattr(obj, serial.name)
        else:
            return TemporaryObject(set((serial.reference,)), data, glb)
    except TypeError:
        pass

    try:
        serial = SerializedFunction(**data)
        if obj := get_module_attrib(glb, serial.module, serial.name):
            return obj
        return TemporaryObject(set(), data, glb)
    except TypeError:
        pass

    try:
        serial = SerializedReference(**data)
        if ref := glb.get(serial.reference):
            return ref
        else:
            return TemporaryObject(set((serial.reference,)), data, glb)
    except TypeError:
        pass

    try:
        serial = PickledObject(**data)
        if not PICKLE_ENABLED:
            logger.warning("Skipping pickled data. PICKLED_ENABLED is off")
            return None
        try:
            res = pickle.loads(codecs.decode(serial.stream.encode(), "base64"))
        except Exception:
            return TemporaryObject(set(), data, glb)
        return res
    except TypeError:
        pass

    return None


def decodeChunk(glb, data):
    """
    Used as object_hook for the json decoder. Decodes serialized chunk objects
    """
    try:
        serial = SerializedModule(**data)
        mod = serial.create()
        glb[mod.__name__] = mod
        return mod

    except TypeError:
        pass

    if cb := decodeCallable(glb, data):
        return cb

    try:
        serial = SerializedClass(**data)
    except TypeError:
        return data
    return ClassConstructor(data, glb, serial)


def deserialize_chunks(serial) -> Optional[List[ClassConstructor]]:
    if not __package__:
        return None

    glb = {}

    res = json.loads(serial, object_hook=partial(decodeChunk, glb))
    return res
