import json
import logging
import inspect
import importlib
from importlib import util
import sys
import weakref

import attrs
from attr import _make as attrMake
from functools import partial
from typing import Any, Dict, Callable, Sequence, Optional, Set, Tuple, List

logger = logging.getLogger(__name__)


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


@attrs.define
class SerializedReference(SerializedObject):
    reference: int = attrs.field()


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
    failed: bool = attrs.field(default=True)


class Serializeable:
    __slots__ = ()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def post_serial(self, owner):
        pass

    def post_serial_setattr(self, name, value):
        try:
            setattr(self, name, value)
        except AttributeError:
            print(self, name, value)
            raise

    def should_serialize_attribute(self, name: str, value: Any) -> Tuple[bool, Any]:
        return default_should_serialize_attribute(name, value)

    def serialize(self, glb) -> Optional[SerializedObject | SerializedReference]:
        return serialize_object(self, glb)


def default_should_serialize_attribute(name: str, value: Any) -> Tuple[bool, Any]:
    return (not name.startswith("_"), value)


def serialize_object(obj, glb) -> Optional[SerializedObject | SerializedReference]:
    if not hasattr(obj, "__module__"):
        return None

    if isinstance(obj, SerializedObject):
        print(obj)
        return None

    if serial := serialize_Reference(obj, glb):
        return serial

    fields = {}
    try:
        fields = attrs.fields_dict(type(obj))
    except attrs.exceptions.NotAnAttrsClassError:
        pass

    res = SerializedClass(obj.__module__, obj.__class__.__qualname__, hash(obj))

    if hasattr(obj, "__dict__"):
        attributes = obj.__dict__.copy()
    else:
        # attributes = {x: getattr(obj, x) for x in obj.__slots__}
        attributes = {x: getattr(obj, x) for x in dir(obj)}

    signature = inspect.signature(obj.__init__)
    res.kwargs = {
        x.name: x.default if x.default and x.default is not inspect._empty else None
        for x in signature.parameters.values()
    }

    for name, val in filter(lambda x: not (inspect.ismethod(x[1]) or inspect.isfunction(x[1])), attributes.items()):
        if name.startswith("__"):
            continue

        if isinstance(obj, Serializeable):
            sa, val = obj.should_serialize_attribute(name, val)
        else:
            sa, val = default_should_serialize_attribute(name, val)

        if field := fields.get(name):
            sa = field.metadata.get("serialize", sa)

            default = field.default
            if isinstance(default, attrMake.Factory):
                if default.takes_self:
                    default = default.factory(obj)
                else:
                    default = default.factory()
            if val == default:
                continue

        if not sa:
            continue

        if isinstance(val, weakref.ReferenceType):
            val = val()

        if serial := serialize_Reference(val, glb):
            val = attrs.asdict(serial)

        if name in res.kwargs:
            res.kwargs[name] = val
        else:
            res.attributes[name] = val

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


class ChunkEncoder(json.JSONEncoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.glb = {}

    def default(self, o: Any):
        if isinstance(o, Serializeable) and (res := o.serialize(self.glb)):
            return attrs.asdict(res, recurse=False)

        if callable(o) and (res := serialize_Function(o)):
            return attrs.asdict(res, recurse=False)
        try:
            return attrs.asdict(serialize_object(o, self.glb))
        except Exception:
            try:
                res = super().default(o)
            except Exception:
                logger.exception(f"Failed to encode: {o}")
                return attrs.asdict(SerializedFailed())
        return o


def serialize_chunks(chunks: Sequence[Serializeable]):
    res = json.dumps(chunks, cls=ChunkEncoder, indent=4)
    print(res)
    return res


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


def get_module_attrib(mod_name, attr_name):
    if not mod_name:
        return None
    if not (module := import_module(mod_name)):
        return None
    return getattr(module, attr_name)


class TemporaryObject:
    def __init__(self, refs: Set, data, glb) -> None:
        self.data = data
        self.refs = refs
        self.glb = glb

    def __repr__(self) -> str:
        return f"TemporaryObject({self.refs})"

    def construct(self):
        return decodeCallable(self.glb, self.data)


class ClassConstructor:
    def __init__(self, cls, glb, serialized: SerializedClass):
        self.cls = cls
        self.glb = glb
        self.serial = serialized

    def _cnstr_val(self, val):
        if isinstance(val, ClassConstructor):
            return val.construct()

        if isinstance(val, TemporaryObject):
            return val.construct()

        if isinstance(val, dict):
            res = {}
            for _k, _v in val.items():
                res[_k] = self._cnstr_val(_v)
            return res

        if isinstance(val, list):
            res = []
            for _v in val:
                res.append(self._cnstr_val(_v))
            return res

        return val

    def construct(self):
        for key, val in self.serial.kwargs.copy().items():
            self.serial.kwargs[key] = self._cnstr_val(val)

        inst = self.glb["__chunk_objects"].get(self.serial.hashval)
        if not inst:
            inst = self.cls(**self.serial.kwargs)
            self.glb["__chunk_objects"][self.serial.hashval] = inst

        for key, val in self.serial.attributes.items():
            resval = self._cnstr_val(val)

            if isinstance(resval, Serializeable):
                resval.post_serial(inst)

            if isinstance(inst, Serializeable):
                inst.post_serial_setattr(key, resval)
            else:
                Serializeable.post_serial_setattr(inst, key, resval)

        return inst


def decodeCallable(glb, data):
    try:
        serial = SerializedMethod(**data)
        if obj := glb["__chunk_objects"].get(serial.reference):
            return getattr(obj, serial.name)
        else:
            return TemporaryObject(set((serial.reference,)), data, glb)
    except TypeError:
        pass

    try:
        serial = SerializedFunction(**data)
        return get_module_attrib(serial.module, serial.name)
    except TypeError:
        pass

    try:
        serial = SerializedReference(**data)
        if ref := glb["__chunk_objects"].get(serial.reference):
            return ref
        else:
            return TemporaryObject(set((serial.reference,)), data, glb)
    except TypeError:
        pass

    return None


def decodeChunk(glb, data):
    if "__chunk_objects" not in glb:
        glb["__chunk_objects"] = {}

    # res = None
    if cb := decodeCallable(glb, data):
        return cb

    else:
        try:
            serial = SerializedClass(**data)
            if not (cls := get_module_attrib(serial.module, serial.name)):
                return None

            return ClassConstructor(cls, glb, serial)
        except TypeError:
            pass

    return data

    # elif cls_str := data.get("class"):
    #     if not (cls := get_module_attrib(data.get("module"), cls_str)):
    #         return None
    #     res = ClassConstructor(cls, glb, data.get("args"), data.get("attributes"), data.get("hash"))
    # if not res:
    #     return data
    # return res


def deserialize_chunks(serial) -> Optional[List[ClassConstructor]]:
    if not __package__:
        return None

    importlib.invalidate_caches()
    glb = globals().copy()
    glb[__package__] = importlib.import_module(__package__)

    res = json.loads(serial, object_hook=partial(decodeChunk, glb))

    import pprint

    pprint.pp(res, indent=4)
    return res
