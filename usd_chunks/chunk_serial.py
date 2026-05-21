"""
## Encoding
Chunk Serial is the interface for serializing Chunks and entire scenes composed of multiple Chunks.

The Serializeable class may be extended by a class to modify how and what to encode. .chum and .py
extensions represent a chunk module, where .chum is simply a compressed representation of a ChunkModule
with the source code embedded. A .json file represents a scene file without embedding of any modules. Modules
do not encode any loaded chunks, meaning they do not save any modified values, but can be used to create chunks.
A scene file on the other hand does encode all loaded chunks and their respective param values, connections and callbacks.

To serialize a scene, one should use the serialize_json function and pass a Scene. The Scene is a basic TypedDict
with entries for chunks, modules and layers. The output is a json representation of the scene. The layers entry
is just a mapping between a usda file and the chunk id, meaning the layer is not embedded in the serialized data.

!!! warning
    One source of confusion atm is that if a module used in a saved scene file is modified after being
    serialized, the result of loading the scene file may not be 1:1 with what one would expect. The reasons being:

    1. When loading a serialized scene file, all chunks are recreated from scratch using the saved states - NOT
        using the main() function of the module
    2. All constructors are serialized, even those of attributes and arguments to __init__. If a modules main
        function changed after serialization by f.e modifying the arguments passed to an add_callback(),
        loading the scene file will recreate the chunks from how they were before the changes, and the callback
        may not behave as expected
    3. As a scene file does not serialize the modules in their entirety, refrences to functions of the modules
        will always use the functions defined in the module.

    Because of this, scene files may break if modules main() functions are changed after serialization.

## Decoding
Decoding is done with the deserialize_* functions and translates any SerializedObject back in
to a python object. In most cases, the result of the decode contains ClassConstructors and
CallableConstructors. Calling construct() on these will recreate the original, serialized objects.
Any nested ClassConstructor or CallableConstructor in kwargs or attributes will be constructed prior to being used.

Implementing import functionality for a ChunkModule may look like:

``` py
serial = chunk_serial.load_file(p)
if not serial:
    return

if not (data := chunk_serial.deserialize_json(serial)):
    return

if p.suffix == chunk_serial.FileExtensions.CHUNK_MODULE:
    for mod_cnstr in data.get("modules", []):
        mod = mod_cnstr.construct()
```

And similarly, exporting a module may look like:
``` py
mod: ChunkModule = self.module

if p.suffix == chunk_serial.FileExtensions.PY_MODULE:
    serial = chunk_serial.serialize_py(mod.module)

elif p.suffix == chunk_serial.FileExtensions.CHUNK_MODULE:
    data = {"modules": [mod], "chunks": []}
    serial = chunk_serial.serialize_json(data, True)

else:
    logger.warning(f"wrong file extension {path}")
    return

chunk_serial.save_json(serial, path)
```

Both when encoding and decoding, a glb (short for global) dictionairy is used to keep track
of objects. This is for reference creation - an object should only be encoded / created once, all other
references to the same object will contain a SerializedReference (or similar) entry. When decoding,
constructed classes and modules are stored in the glb, and references will be solved by lookup
in to the glb. When constructing an object multiple times, the glb.objects dict should be cleared in between
constructing the objects, as the objects will otherwise be identical (same id and hash). Some of the objects
may be resolved when decoding; modules, for example, will almost always be resolved during decoding,
which is why the _Glb holds objects and modules seperately

"""

import marshal
import codecs
import inspect
import json
import logging
import pickle
import sys
import types
import weakref
import zlib
import os
import ast
import enum
from pathlib import Path
from functools import partial
from importlib import util
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, overload, TypedDict, Mapping
import zipfile
from types import ModuleType

import attrs
from attr._make import Factory

logger = logging.getLogger(__name__)

## These are placeholders waiting for a better implementation
PICKLE_ENABLED = True
SAVE_MODULES = True


class FileExtensions(str, enum.Enum):
    PY_MODULE = ".py"
    SCENE = ".chsc"
    CHUNK = ".chunk"
    CHUNK_MODULE = ".chum"
    JSON = ".json"


@attrs.define
class _Glb:
    modules: Dict[int | str, Any] = attrs.field(factory=dict)
    objects: Dict[int, Any] = attrs.field(factory=dict)

    def get_obj(self, hashval):
        if not (obj := self.objects.get(hashval)):
            return None

        return obj

    def get_mod(self, hashval):
        if not (obj := self.modules.get(hashval)):
            return None

        return obj


@attrs.define
class SerializedObject:
    """
    Base class for representing objects in serialized form
    """

    def as_dict(self):
        return attrs.asdict(self, recurse=False)


@attrs.define
class SerializedClass(SerializedObject):
    """
    A Serialized class is a generic placeholder for a non-builtin class to be serialized by JSON. The
    attributes contains relevant information to recreate the class. When recreating the class, the
    'module' attribute will be imported, and a class of type 'name' will be created. The arguments to
    __init__ is the 'kwargs' attribute. After initialization, calls setattr() for each item in 'attributes'.

    A subclass of Serializeable may determine what attributes and kwargs to serialize, as well as converting
    them to appropriate values prior to serialization.

    To create a SerializedClass instance, use the static from_class function. The glb argument should be a dictionairy
    holding the created objects. When an attribute or kwarg references an existing object (its hash maps to an object
    in the glb dict), then the encoder can create a SerializedReference or similar.
    """

    module: str = attrs.field()
    name: str = attrs.field()
    hashval: int = attrs.field()
    attributes: Dict[str, Any] = attrs.field(factory=dict)
    kwargs: Dict[str, Any] = attrs.field(factory=dict)

    def __hash__(self) -> int:
        return self.hashval

    @staticmethod
    def from_class(obj, glb) -> Optional["SerializedClass"]:
        """
        Returns a new SerializedClass, that makes the passed obj serializeable for JSON. This is called by the ChunkEncoder
        for all objects that are not builtins, functions, methods or weakrefs

        Args:
            obj (Any): An object which to serialize
            glb (dict): A dictionairy that holds the created objects.

        Returns:
            SerializedClass | None: A new serialized class for obj if successful
        """
        if obj is None or not hasattr(obj, "__module__"):
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

        logger.debug(f"Serialize class: {obj}")
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
                val = _default_serialize_kwarg(i.name, val)

            if (serial := _serialize(val, glb)) and serial is not _Failed:
                val = serial

            if isinstance(val, SerializedFailed):
                logger.debug(f"   Failed: {i.name}")
                continue

            res.kwargs[i.name] = val
            logger.debug(f"   kwarg: {i.name}")

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
                sa, val = _default_should_serialize_attribute(name, val)

            if field := fields.get(name):
                # For attrs classes, we can get the field and check if the field metadata excplicitly sets 'serialize'. This can be used as
                # a nicer way to specify what attributes should be serialized, instead of overriding the 'should_serialize_attribute'
                sa = field.metadata.get("serialize", sa)

                default = field.default
                if isinstance(default, Factory):
                    if default.takes_self:
                        default = default.factory(obj)
                    else:
                        default = default.factory()

                if val == default:
                    # The value of the attribute is the default value and does not need to be saved. The value is set to the
                    # field's default value on creation. This may produce unexpected behaviour if the field changes in between
                    # saving / loading and for this reason it may be worth reconsidering if the serialized representation should
                    # include default values as well... The positives of doing this is that default values perhaps should be controlled
                    # by the class definition, and deserialization only sets overridden attributes. TODO
                    if name in res.kwargs:
                        res.kwargs.pop(name)
                    continue

            if not sa:
                continue

            if isinstance(val, SerializedObject):
                val = attrs.asdict(val, recurse=False)

            else:
                try:
                    if (serial := _serialize(val, glb)) and serial is not _Failed:
                        val = serial
                except RuntimeError:
                    pass

            if field and field.init and name in res.kwargs:
                res.kwargs[name] = val
            else:
                res.attributes[name] = val
        return res


@attrs.define
class PickledObject(SerializedObject):
    """A SerializedObject holding a pickle string"""

    pickle: str = attrs.field()


@attrs.define
class SerializedReference(SerializedObject):
    """A SerializedObject holding a reference to another, already serialized object"""

    reference: int = attrs.field()


class RuntimeModule(types.ModuleType):
    """
    A ModuleType holding the code of an imported module. The imported module may not be
    a file on disc, so this can be used to compile() and exec() the code at runtime.
    """

    def __init__(self, code: str, name: str, doc: str | None = None) -> None:
        super().__init__(name, doc)
        self._code = code

    def compile(self):
        """
        Compiles and executes this module. The result is stored in __dict__
        """
        dat = compile(self._code, filename=f"<ChunkSerial_{self.__name__}>", mode="exec")
        exec(dat, self.__dict__)


@attrs.define
class SerializedModule(SerializedObject):
    """A SerializedObject holding a ModuleType."""

    code: str = attrs.field()
    module_name: str = attrs.field()
    hashval: int = attrs.field()

    def create(self) -> RuntimeModule:
        """
        Creates a RuntimeModule from the code and module_name

        Returns:
            RuntimeModule: The RuntimeModule
        """
        return RuntimeModule(self.code, self.module_name)


@attrs.define
class SerializedMethod(SerializedObject):
    """
    A SerializedObject holding a reference to another object (self) and the name of the
    method
    """

    reference: int = attrs.field()
    name: str = attrs.field()


@attrs.define
class SerializedFunction(SerializedObject):
    """
    A SerializedObject holding a function. Module is a string from where to import the module. and name is the name of the function
    """

    reference: int = attrs.field()
    module: str = attrs.field()
    name: str = attrs.field()


@attrs.define
class SerializedLambda(SerializedObject):
    """
    A SerializedObject holding a lambda expr. Module is a string from what module the lambda is defined. closure is the closure values
    """

    lambda_code: bytes = attrs.field()
    module: str = attrs.field()
    closure: Dict = attrs.field()


@attrs.define
class SerializedFailed(SerializedObject):
    """
    Returned by ChunkEncoder when the encoding failed
    """

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
        _default_post_serial_setattr(self, name, value)

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
        return _default_serialize_kwarg(name, default)

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
        return _default_should_serialize_attribute(name, value)

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


def _default_should_serialize_attribute(name: str, value: Any) -> Tuple[bool, Any]:
    return (True, value)


def _default_post_serial_setattr(obj, name: str, value: Any) -> None:
    setattr(obj, name, value)


def _default_serialize_kwarg(name: str, default: Any) -> Any:
    if type(default).__name__ not in __builtins__ and default is not None:
        return attrs.asdict(SerializedFailed(repr(default)))
    return default


@overload
def serialize_object(obj: Serializeable, glb: dict) -> Optional[SerializedClass | SerializedReference]: ...


@overload
def serialize_object(obj: types.ModuleType, glb: dict) -> Optional[SerializedModule]: ...


def serialize_object(obj: Any, glb: dict) -> Optional[SerializedObject]:
    """
    Create a SerializedObject from obj. If not seccessful, this returns None

    Args:
        obj (Any): The object which to create a serial representation of
        glb (dict): The dict holding already created SerializedObjects. Used for reference finding

    Returns:
        Optional[SerializedObject]: The created SerializedObject or None
    """
    if serial := serialize_reference(obj, glb):
        # If the object exists in glb, reuturn a reference SerializedReference rather than creating an
        # identical class
        return serial

    # Create a SerializedClass representation of the object
    if res := SerializedClass.from_class(obj, glb):
        glb[res.hashval] = res
    return res


def serialize_reference(ref, glb) -> Optional[SerializedReference]:
    """
    Create a SerializedReference to ref it exists in glb. Otherwise returns None

    Args:
        ref (Any): An object which to reference
        glb (dict): A dictionairy holding created SerializedObjects

    Returns:
        Optional[SerializedReference]: A SerializedReference or None
    """
    if type(ref).__name__ in __builtins__:
        return None
    try:
        if (h := hash(ref)) and h in glb:
            return SerializedReference(h)
    except TypeError:
        pass

    return None


def serialize_function(func: Callable, glb) -> SerializedFunction | SerializedMethod | SerializedLambda:
    """
    Serialize a function or method and return an appropriate SerializedObject. func must be
    a function or method. No checks is done to confirm...

    Args:
        func (Callable): A callable which to reference

    Returns:
        SerializedFunction | SerializedMethod: A SerializedFunction or SerilalizedMethod
    """

    if func.__name__ == "<lambda>":
        closure = {}
        # If the lambda expression is a closure, we need to store the closure cell_contents
        # in order to recreate them at de-serialization. The cell_contents will be serialized
        # individually.
        if func.__closure__:
            for i, cell in enumerate(func.__closure__):
                closure[func.__code__.co_freevars[i]] = cell.cell_contents

        return SerializedLambda(marshal.dumps(func.__code__), func.__module__, closure)

    if inspect.ismethod(func):
        return SerializedMethod(hash(func.__self__), func.__name__)

    if mod := _import_module(func.__module__):
        return SerializedFunction(hash(mod), func.__module__, func.__qualname__)

    return SerializedFunction(0, func.__module__, func.__qualname__)


def serialize_module(mod: types.ModuleType, glb) -> SerializedModule | None:
    """
    Serialize a module and return a new SerializedModule if successful

    Args:
        mod (types.ModuleType): The module to serialize. This may be a RuntimeModule

    Returns:
        SerializedModule | None: If successful, return a SerializedModule. Else returns None
    """
    try:
        if isinstance(mod, RuntimeModule):
            code = mod._code
        else:
            code = inspect.getsource(mod)

        res = SerializedModule(code, mod.__name__, hash(mod))
        glb[res.hashval] = res
        return res

    except OSError:
        return None


def _serialize(o: Any, glb) -> Dict | type["_Failed"] | None:
    """
    Serialize an object. Will attempt to serialize modules, enums, weakrefs, Serializable, and callables. If none of them succeeds,
    an attempt to serialize using the default serialize_object function is made. returns _Failed if not succeeded

    Args:
        o (Any): An object to serialize
        glb (Dict): _description_

    Returns:
        Dict: A dictionairy that contains serialized data
        Type[_Failed]: If failed
        None: If the object should not be serialized
    """

    if isinstance(o, (types.ModuleType, RuntimeModule)):
        if not ChunkEncoder._export_modules:
            return None

        # If _export_modules, the module may be serialized to a SerializedModule
        if serial := serialize_module(o, glb):
            return attrs.asdict(serial, recurse=False)

    # Enums can be serialized as their values, rather than their class
    if isinstance(o, enum.Enum):
        return o.value

    # References can be directly serialized as SerializedReference
    if isinstance(o, weakref.ReferenceType) and (res := serialize_reference(o(), glb)):
        return attrs.asdict(res, recurse=False)

    # Any object that inherits Serializeable are serialized with their serialize() method
    if isinstance(o, Serializeable):
        res = o.serialize(glb)
        if isinstance(res, SerializedObject):
            return attrs.asdict(res, recurse=False)
        return res

    is_cb = callable(o)

    # A function or method should be serialized as either SerializedMethod or SerializedFunction
    if is_cb and (res := serialize_function(o, glb)):
        return attrs.asdict(res, recurse=False)

    # If nothing else, try the default serialize_object, which if successful returns a SerializedReference or
    # SerializedClass
    elif not is_cb and (res := serialize_object(o, glb)):
        return attrs.asdict(res)

    return _Failed


class ChunkEncoder(json.JSONEncoder):
    _export_modules = True
    _enable_pickle = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.glb = {}

    def default(self, o: Any):
        # First try to serialize using _serialize(). This can serialize most objects.
        res = _serialize(o, self.glb)
        if res is not _Failed:
            return res

        # If _serialize() failed, we may pickle the object
        if self._enable_pickle and not inspect.ismethod(o):
            return attrs.asdict(PickledObject(codecs.encode(pickle.dumps(o), "base64").decode()), recurse=False)
        try:
            # Finally, we can try to use the default json encoder
            res = super().default(o)
        except Exception:
            logger.exception(f"Failed to encode: {o}")
            return attrs.asdict(SerializedFailed())
        return o


def serialize_json(data: Mapping, save_modules=True) -> str:
    """
    From a sequence of Serializables, create a serialized representation. The format is JSON and encoded using the ChunkEncoder

    Args:
        data (Dict[str, Any]): A dictionairy to serialize

    Returns:
        str: A string representation of the objects in JSON format
    """
    ChunkEncoder._export_modules = save_modules
    ChunkEncoder._enable_pickle = PICKLE_ENABLED

    res = json.dumps(data, cls=ChunkEncoder, indent=2)

    # Current work around for cleaning up circular references as I am yet to find a better solution
    # that does not require all the Serializeable classes to implement complex serialize() functions.
    # The issue arises from serialized classes that set attributes and kwargs with unserialized data, such as
    # NameSyncedContainer, that sets kwargs["params"] to a list of Chunks.
    import gc

    gc.collect()

    return res


def serialize_source(obj: Any) -> str:
    """
    Serialize the source code of an object. The obj should be a module, class, method, function, traceback, frame,
    or code object.

    Args:
        obj (Any): The object which to get the source code from

    Returns:
        str: The source code of the object
    """
    return inspect.getsource(obj)


def compute_checksum(obj: str) -> int:
    """
    compute the adler32 checksum of a string

    Args:
        obj (str): The string which to compute the checksum of

    Returns:
        int: The checksum
    """
    return zlib.adler32(obj.encode())


def serialize_py(mod: types.ModuleType) -> str:
    """
    Serialize a module to python by extracting the source code

    Args:
        mod (types.ModuleType): A module to get the source of

    Returns:
        str: The modules source code
    """

    if isinstance(mod, RuntimeModule):
        return mod._code
    return serialize_source(mod)


def file_extensions() -> Tuple[str, ...]:
    """
    Returns a tuple of supported file extensions

    Returns:
        Tuple[str, ...]: All supported file extensions for saving / loading serialized chunk files
    """
    return tuple(x.value for x in FileExtensions)


def ext_should_compress(ext: str) -> bool:
    """
    Returns if the extension ext should be compressed

    Args:
        ext (str): A string, such as .json

    Returns:
        bool: If ext represents a compressed file
    """
    return ext in (FileExtensions.CHUNK, FileExtensions.CHUNK_MODULE, ".jsonc")


class Scene(TypedDict):
    """
    A container required for serializing a scene file
    """

    modules: Sequence[Any]
    chunks: Any
    layers: Dict[str, str]


class Module(TypedDict):
    """
    A container required for serializing ChunkModules
    """

    modules: Sequence[Any]


class PyModule(TypedDict):
    """
    A container required for serializing python modules
    """

    module: ModuleType


class _Failed:
    pass


def save_module(file: str | Path, module: Module):
    """
    Save serial to a file. serial should be created with either serialize_py() or serialize_json(). The
    encoding is determined by the file extension. .chunk or .chum will be compressed.

    Args:
        file (str): The file save to
        module (Module): The Module to save
    """
    _, ext = os.path.splitext(file)

    if ext != FileExtensions.CHUNK:
        logger.warning(f"Unsupported file extension of {file}. Exptected {FileExtensions.CHUNK.value}")
        return

    serial = serialize_json(module, True)
    if ext_should_compress(ext):
        compressed = zlib.compress(serial.encode())
        with open(file, "wb") as f:
            f.write(compressed)
    else:
        with open(file, "w") as f:
            f.write(serial)


def save_py_module(file: str | Path, module: PyModule):
    """
    Save a python module to a file

    Args:
        file (str): The file save to
        module (PyModule): The module to save
    """
    _, ext = os.path.splitext(file)

    if ext != FileExtensions.PY_MODULE:
        logger.warning(f"Unsupported file extension of {file}. Exptected {FileExtensions.CHUNK.value}")
        return

    serial = serialize_py(module.get("module"))
    if ext_should_compress(ext):
        compressed = zlib.compress(serial.encode())
        with open(file, "wb") as f:
            f.write(compressed)
    else:
        with open(file, "w") as f:
            f.write(serial)


def save_scene(
    file: str | Path,
    scene: Scene,
    include_serial: Sequence[Tuple[str, str]] | None = None,
    include_files: Sequence[str] | None = None,
    compressed=True,
):
    """
    Save a scene to a .chsc archive file using the standard zip

    Args:
        file (str): The file save to
        scene (Scene): The scene content to save
        include_serial (Sequence[Tuple[str, str]] | None): Additional data to write directly to the archive. Defaults to None
        include_files (Sequence[str, str] | None): Additional files to write to the archive. Defaults to None
        compressed (bool): If underlying scene should be compressed. Defaults to True
    """
    p = Path(file)

    if p.suffix != FileExtensions.SCENE:
        logger.warning(f"Unsupported file extension of {file}. Exptected {FileExtensions.SCENE.value}")
        return

    serial = serialize_json(scene, False)

    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as f:
        f.writestr("scene.json", serial)

        if include_serial:
            for name, data in include_serial:
                f.writestr(name, data)

        if include_files:
            for inc_file in include_files:
                inc_p = Path(inc_file)
                if not inc_p.exists():
                    continue
                f.write(inc_p)


def save_json(file: str | Path, data: str, include_files: Sequence[str] | None = None):
    """
    Save serial to a file. serial should be created with either serialize_py() or serialize_json(). The
    encoding is determined by the file extension. .chunk or .chum will be compressed.

    Args:
        file (str): The file save to
        data (str): The content to save
    """

    _, ext = os.path.splitext(file)

    if ext_should_compress(ext):
        compressed = zlib.compress(data.encode())
        with open(file, "wb") as f:
            f.write(compressed)
    else:
        with open(file, "w") as f:
            f.write(data)

    logger.debug(f"Saved file {file}")


def load_scene_data(file: str | Path, name: str) -> Optional[bytes]:
    """
    From a .chsc scene file, load the content at name

    Args:
        file (str | Path): Path to the .chsc scene file
        name (str): The name within the archive to load

    Returns:
        Optional[bytes]: The loaded data if success, otherwise None
    """
    _, ext = os.path.splitext(file)
    if ext != FileExtensions.SCENE:
        return None

    _, fext = os.path.splitext(name)

    with zipfile.ZipFile(file, "r") as f:
        serial = f.read(name)

        if fext and ext_should_compress(fext):
            try:
                # If the file was saved with compression, we can try to decompress it here
                serial = zlib.decompress(serial)
            except zlib.error:
                # The file may have been forcefully uncompressed, so instead of returning None, just print a warning
                logger.warning(f"Failed to decompress {file}")
    return serial


def load_file(file: str | Path) -> Optional[bytes]:
    """
    Load a file and return its content as bytes

    Args:
        file (str): A path to a file which to load

    Returns:
        Optional[bytes]: The content of the file. If the file is not supported, returns None
    """
    if not os.path.exists(file):
        logger.warning(f"Tried to load a file that does not exist: {file}")
        return

    _, ext = os.path.splitext(file)
    if ext not in file_extensions():
        logger.warning(f"Unsupported file extension of {file}")
        return

    if ext == FileExtensions.SCENE:
        # Load only the chunk scene from the archive. Although the archive may contain other files,
        # those are not managed or recognized by default
        try:
            serial = load_scene_data(file, "scene.jsonc")
        except KeyError:
            serial = load_scene_data(file, "scene.json")
    else:
        with open(file, "rb") as f:
            serial = f.read()

    if not serial:
        return

    if ext_should_compress(ext):
        try:
            # If the file was saved with compression, we can try to decompress it here
            return zlib.decompress(serial)
        except zlib.error:
            # The file may have been forcefully uncompressed, so instead of returning None, just print a warning
            logger.warning(f"Failed to decompress {file}")
    return serial


def _import_module(module_name: str) -> Optional[types.ModuleType]:
    """
    Import a module with name module_name

    Args:
        module_name (str): A module to import from the current environment

    Returns:
        Optional[typed.ModuleType]: The imported module if successful, else None
    """

    if mod := sys.modules.get(module_name):
        return mod

    # If the module is not already loaded, then ModuleNotFoundError is thrown. We can handle this by loading it at construction
    # time instead
    try:
        spec = util.find_spec(module_name)
    except ModuleNotFoundError:
        return

    if not spec:
        return
    module = util.module_from_spec(spec)
    if not module or not spec.loader:
        return

    spec.loader.exec_module(module)
    sys.modules[module_name] = module

    return module


def get_module_attrib(glb: _Glb, mod_name: str, attr_name) -> Any:
    """
    get an attribute from a module

    Args:
        glb (dict): A dictionairy holding previously loaded modules and objects
        mod_name (str): The name of the module to get the attribute from
        attr_name (str): The attribute of the module to get

    Returns:
        Any: The module attribute. Returns None if something went wrong
    """
    if not mod_name:
        return None

    if mod := glb.modules.get(mod_name):
        # If glb has mod, then we can try to get the attribute from there. If unseccessful and
        # _import_module succeeds, the module in glb is overritten
        try:
            return getattr(mod, attr_name)
        except Exception:
            return None

    if not (module := _import_module(mod_name)):
        return None

    try:
        return getattr(module, attr_name)
    except AttributeError:
        logger.error(f"Could not find attribute {attr_name} in module {mod_name}")
    except ModuleNotFoundError:
        # Silently fail as this should be handled elsewhere
        pass


class CallableConstructor:
    def __init__(self, data, glb: _Glb) -> None:
        """
        A CallableConstructor is created when decodeCallable fails, meaning the reference or module was not yet
        created. For this reason, the direct result of the decoding is not 'valid', and instead contain
        CallableConstructors and ClassConstructors, which in turn need to be constructed after decoding is done.

        Args:
            data (dict): The dict representation of a SerializedObject. This is used to decode the object
            glb (dict): The dict containing decoded objects
        """
        self.data = data
        self.glb: _Glb = glb

    def construct(self) -> Any:
        """
        After decoding, calling construct() will return the recreated object. If unable to create the object, this returns None
        and prints a warning

        Returns:
            Optional[Any]: The decoded callable or None

        Raises:
            ValueError: If decodeCallable returns a CallableConstructor (decoding the reference failed)
        """

        res = decodeCallable(self.glb, self.data)
        if isinstance(res, ClassConstructor):
            return res.construct()

        if isinstance(res, CallableConstructor):
            raise ValueError(f"Failed to decode callable {self.data}")

        if res is _Failed:
            return None

        return res


class ClassConstructor:
    def __init__(self, glb: _Glb, serialized: SerializedClass):
        """
        A ClassConstructor is created when decoding a SerializedClass. The ClassConstructor
        holds the necessary information for recreating the serialized class. Calling construct()
        will create the new python object based on the information of the SerializedClass

        Args:
            glb (dict): A dictionairy holding previously decoded modules, classes and functions
            serialized (SerializedClass): The SerializedClass to ultimately recreate
        """
        self.glb: _Glb = glb
        self.serial: SerializedClass = serialized

    def construct_value(self, val, glb) -> Any:
        """
        Called to construct a value of serial.kwargs or serial.attributes before use

        Args:
            val (Any): The value to construct
            glb (dict): A dictionairy holding created objects

        Returns:
            Any: The constructed value
        """
        if isinstance(val, (ClassConstructor, CallableConstructor)):
            return val.construct()

        # TODO: These checks should be more generic... This will ultimately skip kwargs and attributes that do not match these checks
        if isinstance(val, dict):
            res = {}
            for _k, _v in val.items():
                res[_k] = self.construct_value(_v, glb)
            return res

        # Same as above... How should this be more general?
        if isinstance(val, (list, tuple, set)):
            res = []
            for _v in val:
                res.append(self.construct_value(_v, glb))
            if isinstance(val, tuple):
                return tuple(res)
            if isinstance(val, set):
                return set(res)
            return res
        return val

    def construct(self, post_constuct=True) -> Any:
        """
        Construct the class with the information from self.serial. This should be called after
        the JSON decoder is done, as the glb should contain information about all the objects
        that will be created. The reason for this is that some SerializedObjects will reference
        other objects, and if they cannot be de-referenced, these object will fail to construct

        Args:
            post_constuct (bool, optional): Set to also call setattr() after initialization. If False, this will only create the class, without updating any attributes. Defaults to True.

        Returns:
            Any: The constructed class
        """

        kwargs = {}
        for key, val in self.serial.kwargs.copy().items():
            kwargs[key] = self.construct_value(val, self.glb)

        inst = self.glb.objects.get(self.serial.hashval)
        if not inst or isinstance(inst, (ClassConstructor, CallableConstructor)):
            try:
                if not (cls := get_module_attrib(self.glb, self.serial.module, self.serial.name)):
                    logger.warning(f"Failed to get module attrib {self.serial}")
                    return None
                inst = cls(**kwargs)
            except TypeError:
                logger.warning(f"Failed to call ClassConstructor {self.serial}")
                return

        self.glb.objects[self.serial.hashval] = inst

        if post_constuct:
            return self.post_constuct()

        return inst

    def post_constuct(self) -> Any:
        """
        calls setattr() on the object created by construct() for all attributes in self.serial.attributes

        Returns:
            Any: The object created by construct
        """

        if not (inst := self.glb.objects.get(self.serial.hashval)):
            return

        for key, val in self.serial.attributes.items():
            resval = self.construct_value(val, self.glb)

            if isinstance(resval, (ClassConstructor, CallableConstructor)):
                print(key, resval)

            if isinstance(resval, Serializeable):
                resval.post_serial(inst)

            if isinstance(inst, Serializeable):
                inst.post_serial_setattr(key, resval)
            else:
                _default_post_serial_setattr(inst, key, resval)
        return inst


def decodeCallable(glb: _Glb, data) -> Callable | CallableConstructor | _Failed:
    """
    Decode data. Will try to decode to SerializedMethod, SerialziedFunction, SerializedReference and PickledObject. If
    Any of these succeeds, the returned value is the function, method or object that was referenced. Otherwise, if any of these
    classes can be constructed from data, but the reference, method or object cannot be retrieved, then return a CallableConstructor
    that should be constructed later

    Args:
        glb (dict): A dict holding information about the previously created objects
        data (dict): The decoded data as a dictionairy. Typically attrs.asdict(SerializedFunction()) or similar

    Returns:
        Callable | CallableConstructor | _DecodeFailed: The callable if found, else a CallableConstructor if a SerializedObject could be created from data, else _DecodeFailed
    """
    try:
        serial = SerializedMethod(**data)
        if obj := glb.get_obj(serial.reference):
            return getattr(obj, serial.name)
        return CallableConstructor(data, glb)
    except TypeError:
        pass

    try:
        serial = SerializedFunction(**data)
        if ref := glb.get_mod(serial.reference):
            return getattr(ref, serial.name)

        elif ref := glb.get_mod(serial.module):
            return getattr(ref, serial.name)

        elif func := get_module_attrib(glb, serial.module, serial.name):
            return func

        return CallableConstructor(data, glb)
    except TypeError:
        pass

    try:
        serial = SerializedReference(**data)
        if ref := glb.get_obj(serial.reference):
            return ref
        return CallableConstructor(data, glb)
    except TypeError:
        pass

    try:
        serial = PickledObject(**data)
        if not PICKLE_ENABLED:
            logger.warning("Skipping pickled data. PICKLED_ENABLED is off")
            return _Failed
        try:
            res = pickle.loads(codecs.decode(serial.pickle.encode(), "base64"))
        except Exception:
            return CallableConstructor(data, glb)
        return res
    except TypeError:
        pass

    try:
        serial = SerializedLambda(**data)
        try:
            res = marshal.loads(serial.lambda_code)
        except Exception:
            return CallableConstructor(data, glb)

        mod = glb.get_mod(serial.module)
        if not mod:
            mod = _import_module(serial.module)
        if not mod:
            return CallableConstructor(data, glb)

        closure = []
        if serial.closure:
            for x in serial.closure.values():
                obj = x
                if isinstance(obj, (ClassConstructor, CallableConstructor)):
                    obj = obj.construct()
                closure.append(types.CellType(obj))

        return types.FunctionType(res, mod.__dict__, "<lambda>", closure=tuple(closure))

    except TypeError:
        pass

    return _Failed


def _decodeChunk(glb: _Glb, data):
    """
    Used as object_hook for the json decoder. Decodes serialized chunk objects
    """

    # First see if the data is a Module. In that case, we can return it
    try:
        serial = SerializedModule(**data)
        mod = serial.create()
        glb.modules[serial.hashval] = mod
        glb.modules[serial.module_name] = mod
        return mod

    except TypeError:
        pass

    # If we can create a Callable from the data, return it
    cb = decodeCallable(glb, data)
    if cb is not _Failed:
        return cb

    # If a SerializedClass can be created from data, return it. Else just return data
    try:
        serial = SerializedClass(**data)
    except TypeError:
        return data

    res = ClassConstructor(glb, serial)
    if not glb.get_obj(serial.hashval):
        glb.objects[serial.hashval] = res
    return res


def is_py_source(serial: str | bytes) -> bool:
    """
    Check if the content of serial is valid python code

    Args:
        serial (str | bytes): The code to check for validity

    Returns:
        bool: True if the code is valid python code
    """
    try:
        ast.parse(serial)
    except SyntaxError:
        return False
    return True


def deserialize_py(code: str, mod_name: str) -> RuntimeModule:
    """
    Create a RuntimeModule from a string

    Args:
        code (str): The code of the runtime module
        mod_name (str): The name of the module

    Returns:
        RuntimeModule: The runtime module
    """
    return RuntimeModule(code, mod_name)


class DecodedScene(TypedDict):
    modules: Sequence[ClassConstructor]
    layers: Dict[str, ClassConstructor]
    chunks: Optional[ClassConstructor]


def deserialize_scene(serial: str | bytes) -> Optional[DecodedScene]:
    """
    Deserialize a bytes object. Typically, ChunkModules are serialized, then the result of this function
    is a list of ClassConstructors. To recreate the ChunkModules, call construct() on the list entries.

    Args:
        serial (str | bytes): The serial data to deserialize. This data is usually created from load_file()

    Returns:
        Optional[List[ClassConstructor]]: Depending on the content of serial, the return value may differ. But typically this is a list of ClassConstructors
    """

    glb = _Glb()
    res = json.loads(serial, object_hook=partial(_decodeChunk, glb))

    return DecodedScene(**res)


def deserialize_json(serial: str | bytes) -> Any:
    glb = _Glb()
    return json.loads(serial, object_hook=partial(_decodeChunk, glb))
