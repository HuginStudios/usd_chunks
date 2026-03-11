"""
The ChunkRegistry singleton is responsible for loading and unloading the chunks.
USD_CHUNK_DIR environment variable tells the registry where to scan for .py files
containing the chunks. The ChunkRegistry will also scan nested folders in this location

The ChunkRegistry can also be used at runtime to find loaded chunks:
``` py
reg = ChunkRegistry()
myChunk = reg.get_chunk("my_chunk")
```
"""

from __future__ import annotations

import weakref
import glob
import os
import sys
from pathlib import Path
from importlib import util
from collections import defaultdict
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Set, Generic, TypeVar, overload

from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_serial
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton, SceneSender, send_notice

if TYPE_CHECKING:
    import types
    from usd_chunks.chunk_core import ChunkCommon, ChunkConfig, ChunkHash

import ast
import importlib.abc
import logging
import subprocess

from timeit import default_timer as timer

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class ChunkEnvVar(Generic[_T]):
    def __init__(self, key: str, default: _T, relpath=False):
        self.key = key
        self._default: _T = default
        self._relpath = relpath

    @property
    def default(self) -> _T:
        if self._relpath and self._default:
            dirname = os.path.dirname(os.path.dirname(__file__))
            if isinstance(self._default, list):
                return [os.path.join(dirname, x) for x in self._default]  # type: ignore
            elif isinstance(self._default, str):
                return os.path.join(dirname, self._default)
        return self._default

    @overload
    def _get(self, default: None, fallback=None) -> _T | None: ...
    @overload
    def _get(self, default: _T, fallback=None) -> _T: ...

    def _get(self, default, fallback=None):
        envvar = os.environ.get(self.key, fallback)
        default = self.default

        if default is None:
            return envvar

        if not envvar:
            return default

        if isinstance(default, list) and isinstance(envvar, str):
            res = envvar.split(os.pathsep)

            return [x for x in set(res + default) if x]
        return envvar

    def __call__(self, fallback: Optional[_T] = None, /, *args: Any, **kwds: Any):
        return self._get(self.default, fallback)


USD_CHUNK_DIRS = ChunkEnvVar("USD_CHUNK_DIRS", ["scripts"], True)
USD_CHUNK_DEPENDENCY_DIR = ChunkEnvVar("USD_CHUNK_DEPENDENCY_DIR", "_installed_deps", True)
USD_CHUNK_LOAD_FILTER = ChunkEnvVar[str | None]("USD_CHUNK_LOAD_FILTER", None)


class ChunkDependencyManager(metaclass=RegistrySingleton):
    initialized = False

    def initialize(self):
        self._cache = defaultdict(set)

        self.package_dir = USD_CHUNK_DEPENDENCY_DIR()

        if self.package_dir:
            if self.package_dir not in sys.path:
                sys.path.append(self.package_dir)
            self.initialized = True

    @staticmethod
    def package_info(package):
        try:
            name, version = package.split("==")
        except ValueError:
            name = package
            version = None
        return name, version

    def install_packages(self, requirements):
        if not self.initialized:
            return

        packages = []
        recache = True

        for package in requirements:
            if self.package_in_cache(package):
                continue

            if recache:
                self.update_cache()
                recache = False
                if self.package_in_cache(package):
                    continue

            if self.should_install_package(package):
                packages.append(package)

        if not packages:
            return

        args = [sys.executable, "-m", "pip", "install", "-t", self.package_dir]
        args.extend(packages)
        subprocess.run(args)

        if (pip_dir := self.package_dir) and pip_dir in sys.path_importer_cache:
            sys.path_importer_cache.pop(pip_dir)

    def should_install_package(self, package):
        reg = ChunkRegistry()
        if not reg.scene or not self.initialized:
            return False
        return reg.scene.should_install_package(package)

    def update_cache(self):
        if not self.initialized:
            return

        # Search the current interpreters' environment
        args = [sys.executable, "-m", "pip", "list", "--format", "freeze"]

        proc = subprocess.run(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pip_packages = proc.stdout.decode().split()
        for p in pip_packages:
            pname, pversion = self.package_info(p)
            self._cache[pname].add(pversion)

        # Search the pip_dep_dir
        args = [sys.executable, "-m", "pip", "list", "--path", self.package_dir, "--format", "freeze"]

        proc = subprocess.run(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pip_packages = proc.stdout.decode().split()
        for p in pip_packages:
            pname, pversion = self.package_info(p)
            self._cache[pname].add(pversion)

    def package_in_cache(self, package):
        if not self.initialized:
            return False

        name, version = self.package_info(package)
        if pvs := self._cache.get(name):
            if not version or version in pvs:
                return True
        return False


class _ConfigTransformer(ast.NodeTransformer):
    def __init__(self, code, globs: dict, module: ChunkModule) -> None:
        self._code = code
        self._module = module
        self._globs = globs.copy()

    def _exec_node(self, node):
        src = ast.get_source_segment(self._code, node)
        if not src:
            return node
        try:
            dat = compile(src, filename="<ast>", mode="exec")
            exec(dat, self._globs)
        except ModuleNotFoundError:
            pass
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if node.name == "config":
            self._exec_node(node)
            if func := self._globs.get("config"):
                self._module.config = func()
        return node

    def visit_Import(self, node: ast.Import) -> Any:
        mymod = self.__module__.split(".")[0]
        if any([mymod in x.name.split(".") for x in node.names]):
            return self._exec_node(node)
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        mymod = self.__module__.split(".")[0]
        if node.module and mymod in node.module.split("."):
            return self._exec_node(node)
        return node


class ChunkLazyConfigLoader(importlib.abc.SourceLoader):
    def __init__(self, fullname: str, path: str, loader, mod: ChunkModule):
        self.fullname = fullname
        self.path = path
        self._loader = util.LazyLoader(loader)
        self._module = mod

    def get_filename(self, fullname: str) -> str:  # noqa: ARG002
        return self.path

    def get_data(self, path: str | bytes) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def load_module(self, fullname: str) -> ModuleType:
        return self._loader.load_module(fullname)

    def exec_module(self, module: types.ModuleType) -> None:
        source_bytes = self.get_data(self.get_filename(self.fullname))
        source = source_bytes.decode("utf-8")

        transformer = _ConfigTransformer(source, module.__dict__, self._module)
        transformer.visit(ast.parse(source_bytes))

        self._loader.exec_module(module)


class ChunkModule(chunk_serial.Serializeable):
    """
    A class used by ChunkRegistry for managing chunk modules

    Args:
        module (ModuleType | None): The loaded chunk module
        module_name (str): The name of the module
        module_file (str): The original, absolute, path to the python module

    Attributes:
        loaded_chunk (chunk.Chunk | None): The loaded chunk retrieved from running module.main()
        loaded (bool): If the module loaded successfully
    """

    def __init__(
        self,
        module: Optional[ModuleType] = None,
        module_name: str = "",
        module_file: Path | str = Path(),
    ):
        self.module = module
        self.module_name = module_name
        self.module_file = Path(module_file)

        self.loaded = False
        self.loaded_chunks: Dict[str, chunk.Chunk] = {}
        self.config: ChunkConfig = chunk.DefaulConfig()
        self.func: Optional[Callable[[], chunk.Chunk | List[chunk.Chunk]]] = None

        self._callbacks = []
        self._cls_cnstr: Optional[chunk_serial.ClassConstructor] = None

        self.exec_module()

    def __hash__(self):
        return hash(self.module_name)

    def __eq__(self, other):
        return self.module_name == other.module_name

    def __repr__(self) -> str:
        return f"ChunkModule(module_name='{self.module_name}', module_file='{self.module_file.as_posix()}')"

    def serialize_kwarg(self, name: str, default: Any) -> Any:
        if name == "module_name":
            return self.module_name
        if name == "module_file":
            return self.module_file.as_posix()
        if name == "module":
            return self.module
        return super().serialize_kwarg(name, default)

    def should_serialize_attribute(self, name: str, value: Any) -> chunk_serial.Tuple[bool, Any]:
        if name in ["loaded_chunks"]:
            return (True, value)
        return (False, None)

    def post_serial_setattr(self, name: str, value: Any) -> None:
        if name == "loaded_chunks":
            for c in value.values():
                self._add_chunk(c)
            return
        return super().post_serial_setattr(name, value)

    def config_changed(self, notice, sender):
        pass

    def chunk_name_changes(self, notice, sender):
        pass

    def remove_chunk(self, chunk_name: Optional[str] = None):
        """
        Unload this module by deleting the associated chunk object
        """
        if not self.loaded or len(self.loaded_chunks) == 0:
            return

        if chunk_name is not None:
            if not (c := self.get_chunk(chunk_name)):
                return
            c.request_delete()

        else:
            chunks = list(self.loaded_chunks.values())
            for c in chunks:
                c.request_delete()

    def holds_chunk(self, chunk_name: str) -> bool:
        return chunk_name in self.loaded_chunks

    def get_chunk(self, chunk_name: str) -> chunk.Chunk | None:
        return self.loaded_chunks.get(chunk_name)

    def _add_chunk(self, c: chunk.Chunk):
        if not (isinstance(c, chunk.Chunk)):
            logger.warning(f"main() in module: {self.module_name} does not return a Chunk object")
            self.loaded = False
            return False

        if self.config:
            c._config = self.config

        names = ChunkRegistry().get_chunk_names()
        if c.name in names:
            c.name = chunk.Chunk.get_valid_child_name(c.name, names)

        self.loaded_chunks[c.name] = c
        self._callbacks.append(NoticeConnection(self._pop_chunk, ChunkNoticeAlias.CHUNK_DELETED, c))

        try:
            c.check_hierarchy()
        except Exception as e:
            logger.exception(e)
            self.loaded = False
            return False

        send_notice(ChunkNoticeAlias.CHUNK_LOADED, c, data=c)

        return True

    def _pop_chunk(self, notice, sender):
        if sender in self.loaded_chunks.values():
            self.loaded_chunks.pop(sender.name)

        if len(self.loaded_chunks) == 0:
            self.loaded = False
            self._callbacks.clear()

    def exec_module(self):
        if type(self.module) is chunk_serial.RuntimeModule:
            if not self.loaded:
                transformer = _ConfigTransformer(self.module._code, self.module.__dict__, self)
                transformer.visit(ast.parse(self.module._code))

            if self.module_name not in sys.modules:
                sys.modules[self.module_name] = self.module

        elif self.module_file.exists():
            spec = util.spec_from_file_location(self.module_name, self.module_file)
            if not spec or not spec.loader:
                return False

            loader = ChunkLazyConfigLoader(self.module_name, str(self.module_file), spec.loader, self)
            spec.loader = loader
            module = util.module_from_spec(spec)
            sys.modules[self.module_name] = module

            try:
                spec.loader.exec_module(module)
            except Exception:
                # This module failed to load. Set to file_name so we can try loading it again later§
                logger.exception(f"Failed to load module {self.module_name}")
                return False
            self.module = module

        else:
            return False

        return True

    def _install_deps(self):
        dep_reg = ChunkDependencyManager()
        if not dep_reg.initialized:
            dep_reg.initialize()

        dep_reg.install_packages(self.config.requirements)

    def _pre_load(self, func_name: str = "main"):
        if not self.loaded:
            if not self.exec_module():
                return False

        self._install_deps()

        if type(self.module) is chunk_serial.RuntimeModule:
            dat = compile(self.module._code, filename=f"<ChunkSerial_{self.module_name}>", mode="exec")
            exec(dat, self.module.__dict__)

        try:
            if not hasattr(self.module, func_name):
                logger.error(f"Function: {func_name} in module: {self.module_name} is not defined")
                self.loaded = False
                return False

            self.func = getattr(self.module, func_name)
            if not self.func:
                logger.error(f"Could not find function {func_name} of module {self.module_name}")
                self.loaded = False
                return False

        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later
            logger.exception(f"Failed to run module {self.module_name}")
            self.loaded = False
            return False

        self.loaded = True
        return True

    def load(self, func_name: str = "main") -> bool:
        self.remove_chunk()

        if not self._pre_load(func_name):
            logger.warning(f"Could not load module {self.module}. {self.loaded}, {self.func}")
            return False

        start = timer()
        self.loaded = self.create()
        end = timer()


        logger.info(f"loaded module: {self.module_name}. Load took {end - start} seconds")
        return True

    def create(self):
        if not self.loaded:
            logger.warning("Trying to create a chunk from an unloaded module. Consider running load() first")
            return False
        loaded = []

        if self.func:
            try:
                retval = self.func()
            except Exception:
                logger.exception("Failed to run main function from module %s", self.module_name)
                return False
            try:
                loaded.extend(retval)  # type: ignore
            except TypeError:
                loaded.append(retval)

        else:
            logger.warning(f"module {self.module_name} does not have a function")
            return False

        for c in loaded:
            self._add_chunk(c)

        return bool(loaded)

    def should_load(self) -> bool:
        if module_filter := USD_CHUNK_LOAD_FILTER():
            if self.module_name not in module_filter:
                return False
        if not self.config:
            return False
        if not self.config.load:
            return False
        return True

    def should_load_for_plugin(self, plugin_name: str) -> bool:
        if not self.config:
            return False
        if self.config.load_for_plugin and plugin_name not in self.config.load_for_plugin:
            return False
        return True


class ChunkRegistry(metaclass=RegistrySingleton):
    """A singeton class to read and register chunk modules. By default,
    chunk modules are scanned for in the USD_CHUNK_DIRS directories.

    Attributes:
        registered_directories (Set[str]) : A set of directories we have searched
        registered_modules (Dict[str, ChunkModule]) : A mapping between module names and modules
        scene (SceneSender) : An instance of the SceneSender for accesing the scene
        plugin (str) : A string identifier of the currently loaded plugin
    """

    registered_directories: Set[Path] = set()
    registered_modules: Dict[str, ChunkModule] = {}
    scene: SceneSender
    plugin: str

    initialized: bool = False

    def initialize(self, scene_sender: SceneSender, plugin: str, unloaded: bool = True) -> None:
        """Initialize the FileReader with the working directory.

        Args:
            scene_sender (SceneSender): A SceneSender instance for access to the scene.
                We hold a reference to it here in order to keep it alive
            plugin (str): A string identifying the currently used plugin
            unloaded (bool): If the modules should not be loaded
        """

        self.scene = scene_sender
        self.plugin = plugin

        ChunkDependencyManager().initialize()

        self.register_chunk_environ_directories()
        self.register_chunk_modules(self.registered_directories.copy(), unloaded)

        c = 0
        for mod in self.registered_modules.values():
            c += 1 if mod.loaded else 0

        logger.info(f"Found {len(self.registered_modules)} chunks. Loaded {c}")
        self.initialized = True

    def register_chunk_environ_directories(self):
        """Register the USD_CHUNK_DIRS directories to find Python modules."""
        environ_dirs = USD_CHUNK_DIRS()

        for chunk_dir in environ_dirs:
            self.register_directory(chunk_dir)

    def register_directory(self, chunk_dir: str):
        """Add a single directory to the register

        Args:
            chunk_dir (str): A pathlike string to add
        """
        if (p := Path(chunk_dir)) and not p.exists():
            logger.warning(f"{chunk_dir} directory not found.")
            return

        if p in self.registered_directories:
            # directory already loaded
            return

        sys.path.append(chunk_dir)
        self.registered_directories.add(p)

    def register_chunk_modules(self, working_dirs: Iterable[Path], unloaded: bool = False):
        """Read and register Python files in the working directory.

        Args:
            working_dirs (Iterable[str]): The directories containing the Python chunk files to register.
        """
        serial = []

        for working_dir in working_dirs:
            # Loop over all directories of the working_dirs directory
            extensions = chunk_serial.file_extensions()

            for module_path in filter(lambda x: x.suffix in extensions, Path(working_dir).glob("**/*")):
                if not module_path.exists():
                    continue

                if not module_path.suffix == ".py":
                    serial.append(module_path)
                    continue

                rel = (module_path.parent / module_path.stem).relative_to(working_dir)
                module_name = str(rel).replace(os.path.sep, ".")
                mod = self.register_module(module_name, module_path)

                if not unloaded and mod and mod.should_load() and mod.should_load_for_plugin(self.plugin):
                    mod.load()

        for p in serial:
            self.import_chunks(p, unloaded)

        # if not unloaded:
        #     self._load_modules()

    # def _load_modules(self):
    #     for mod in self.registered_modules.values():
    #         if mod.should_load() and mod.should_load_for_plugin(self.plugin):
    #             mod.load()

    def register_module(self, name, path):
        # It's fairly safe to call exec_module since it's lazily loaded. Since we're using the ChunkLazyConfigLoader
        # we get access to the config() function, prior to loading the rest of the chunk module

        if name in self.registered_modules:
            logger.warning("Duplicate module:", name)
            return
        self.registered_modules[name] = ChunkModule(module_name=name, module_file=path)
        return self.registered_modules[name]

    def get_chunk_module(self, module_name: str) -> ChunkModule | None:
        """Get the ChunkModule object in the registry where module_name matches the
        stored name of the module. The module_name should typically be the name of the
        python script. If the module failed to load, the module name is stored as the
        path to the python script. To find a failed module, the module_name argument
        should be /full/path/to/module.py

        Args:
            module_name (str): The name of the module to find

        Returns:
            ChunkModule | None: A found ChunkModule object with name, or None if not found
        """
        if module_name not in self.registered_modules:
            return None
        return self.registered_modules[module_name]

    def get_chunk_names(self) -> Set[str]:
        res = set()
        for mod in self.registered_modules.values():
            res |= mod.loaded_chunks.keys()
        return res

    def get_chunk(self, name: str) -> "chunk.Chunk | None":
        """Get a loaded chunk object in the registry

        Args:
            name (str): The name of the chunk to lookup

        Returns:
            chunk.Chunk | None: The chunk object with name, or None if not found
        """

        for mod in self.registered_modules.values():
            if c := mod.get_chunk(name):
                return c
        return None

    def reload_all(self):
        for mod in self.registered_modules.values():
            if mod.loaded:
                mod.remove_chunk()
                mod.load()

    def export_chunks(self, path):
        p = Path(path)
        if p.suffix not in chunk_serial.file_extensions():
            return

        mods = [x for x in self.registered_modules.values() if x.loaded]
        if p.suffix == ".py":
            for i, mod in enumerate(mods):
                if not mod.module:
                    continue

                if not isinstance(mod.module, chunk_serial.RuntimeModule):
                    serial = chunk_serial.serialize_py(mod.module)
                else:
                    serial = mod.module._code

                indexed_path = p.parent / (p.stem + str(i) + p.suffix)
                chunk_serial.save_file(serial, indexed_path)
        else:
            serial = chunk_serial.serialize_json(mods)
            chunk_serial.save_file(serial, path)

    def import_chunks(self, path, unloaded=False):
        p = Path(path)
        if not p.exists() or p.suffix not in chunk_serial.file_extensions():
            return

        if p.suffix == ".py":
            if (mod := self.register_module(p.stem, p.absolute())) and not unloaded:
                if mod.should_load() and mod.should_load_for_plugin(self.plugin):
                    mod.load()
            return

        serial = chunk_serial.load_file(path)

        if not (data := chunk_serial.deserialize_chunks(serial)):
            return

        for mod_cnstr in data:
            if not (mod_name := mod_cnstr.serial.kwargs.get("module_name")):
                continue

            if not (mod := self.get_chunk_module(mod_name)):
                logger.info(f"Creating new module {mod_name}")

                if mod_file := mod_cnstr.serial.kwargs.get("module_file"):
                    mod_package = os.path.dirname(mod_file)
                    if os.path.exists(mod_package):
                        self.register_directory(mod_package)

                mod = mod_cnstr.construct(not unloaded)
                if not isinstance(mod, ChunkModule):
                    logger.error(f"Return value from serial is not a ChunkModule: {mod}")
                    continue

                if not unloaded and not mod._pre_load():
                    continue

                self.registered_modules[mod_name] = mod

            elif mod.module:
                logger.info(f"Adding chunks to module {mod_name}")
                loaded_chunks = mod_cnstr.serial.attributes.get("loaded_chunks")
                if not loaded_chunks:
                    continue

                if not unloaded and not mod._pre_load():
                    continue

                if not unloaded:
                    for c_cnstr in loaded_chunks.values():
                        c = c_cnstr.construct()
                        mod._add_chunk(c)

            elif (mod := mod_cnstr.construct(not unloaded)) and isinstance(mod, ChunkModule):
                logger.info(f"Overriding module {mod_name}")
                if not unloaded and not mod._pre_load():
                    continue

                self.registered_modules[mod_name] = mod

            else:
                logger.error(f"Could not create module {mod_name}")
                continue

            mod._cls_cnstr = mod_cnstr

            for c in mod.loaded_chunks.values():
                c.check_hierarchy()

            mod.loaded = not unloaded


class ChunkLayerManager(metaclass=RegistrySingleton):
    """
    A singleton class for managing the chunk layers. All chunks will be associated with a layer, on to
    which the edits of the Chunks parameters are applied. This singleton is responsible for creating and
    deleting these layers on creation / deletion of the chunks.

    Attributes:
        chunk_layers (Dict[ChunkHash, str]) : A mapping from a Chunk to a layer identifier
        scene (SceneSender) : A handle to the scene for stage access
    """

    initialized: bool = False

    def initialize(self, stage: Usd.Stage):
        """
        Called to initialize the singleton. This should be called at least once in order to setup
        the manager

        Args:
            stage (Usd.Stage): The stage to manage
        """

        self._edit_target_listener = NoticeConnection(
            self._edit_target_changed, ChunkNoticeAlias.STAGE_EDIT_TARGET_CHANGED, globally=True
        )

        self._flags_listener = NoticeConnection(self._flags_changed, ChunkNoticeAlias.STATE_CHANGED, globally=True)

        self.chunk_layers: Dict[ChunkHash, Sdf.Layer] = {}
        self._chunk_refs = weakref.WeakSet()

        # When initialized, the stage may be None as the chunks are loaded prior to loading the stage
        self.stage: Usd.Stage = stage
        self._root_layer = None

        self.initialized = True

    def get_root_layer(self) -> Sdf.Layer:
        """
        Get the parent layer for all chunk layers. This may be a custom layer in the future,
        for now this returns the session layer of the stage

        Raises:
            AttributeError: If stage is invalid

        Returns:
            Sdf.Layer: The root layer which holds the chunk sublayers
        """

        if not self._root_layer:
            self._root_layer = Sdf.Layer.CreateAnonymous("chunks")
        return Sdf.Find(self._root_layer.identifier)

    def get_layer(self, c: "ChunkHash") -> Sdf.Layer | None:
        """
        Get a layer associated with the a chunk

        Args:
            c (ChunkUsdLayerSupport): A chunk object implementing the ChunkUsdLayerSupport protocol

        Returns:
            Sdf.Layer: The layer associated with the ChunkCommon object
            None: If the layer does not exist
        """
        return self.chunk_layers.get(c)

    def get_or_create_layer(self, c: "ChunkCommon") -> Sdf.Layer | None:
        """
        Get a layer similarly to get_layer(). Will create a new one if the layer does
        not already exists

        Args:
            c (ChunkUsdLayerSupport): The chunk object which implements the ChunkUsdLayerSupport protocol

        Returns:
            Sdf.Layer: The layer, either found or created

        Rasises:
            AttributeError: If the stage has not been initialized
        """

        layer = self.get_layer(c)
        if not layer:
            if self.is_muted(c):
                return None
            layer = self.add_layer(self.get_root_layer(), c)
        return layer

    def is_muted(self, c: "ChunkHash") -> bool:
        """
        Check if a chunk layer is muted

        Args:
            c (ChunkHash): The chunk which whose layer to check

        Returns:
            bool: True when the layer is muted, False otherwise
        """
        layer = self.chunk_layers.get(c)
        if self.stage and layer and layer.IsMuted():
            return True
        return False

    def add_layer(self, target_layer: Sdf.Layer, c: "ChunkCommon") -> Sdf.Layer:
        """
        Add a layer to the stage and add a mapping from chunk c to the new layer.

        Args:
            target_layer (Sdf.Layer): The target layer onto which we append the new layer
            c (ChunkUsdLayerSupport): The chunk that can modify the new layer. The new layer's identifier
                will be created from the chunks get_layer_identifier()

        Returns:
            Sdf.Layer: A new layer
        """

        if layer := self.chunk_layers.get(c):
            return layer

        layer_id = c.get_layer_identifier()
        layer = Sdf.Layer.CreateAnonymous(layer_id)

        self.chunk_layers[c._hashable] = layer
        target_layer.subLayerPaths.insert(0, layer.identifier)

        # We must make sure to delete the layer of the chunk AFTER all other notices are sent.
        # For this reason, we can't rely on callbacks. Instead we can register a finalizer, so we delete
        # the layer once the chunk is actually deleted
        weakref.finalize(c, self._chunk_deleted, c.get_hash())

        return layer

    def clear_all_layers(self):
        for c, lay in self.chunk_layers.items():
            self.clear_layer(lay.identifier)

        sublayer_paths = self.get_root_layer().subLayerPaths.copy()

        for i, sublayer in reversed(list(enumerate(sublayer_paths))):
            if sublayer in self.chunk_layers.values():
                sublayer_paths.pop(i)

        self.get_root_layer().subLayerPaths = sublayer_paths  # type: ignore
        self.chunk_layers.clear()

    def clear_layer(self, layer_id: str | chunk.ChunkCommon):
        """
        Clear a layer and remove from sublayer list

        Args:
            layer_id (str): The layer to remove
        """
        if isinstance(layer_id, str):
            layer = Sdf.Layer.Find(layer_id)
        elif _layer_id := self.get_layer(layer_id):
            layer = Sdf.Layer.Find(_layer_id.identifier)
        else:
            return
        layer.Clear()

    def reload_all(self):
        """
        clear the chunk layer stack and recreate them

        Args:
            stage (Optional[Usd.Stage], optional): An optional stage. This is used when the stage is just loaded and the scene does not yet have access to it. Defaults to None.
        """
        sublayer_paths = self.get_root_layer().subLayerPaths.copy()

        for i, sublayer in reversed(list(enumerate(sublayer_paths))):
            if sublayer in self.chunk_layers.values():
                sublayer_paths.pop(i)

        self.get_root_layer().subLayerPaths = sublayer_paths  # type: ignore
        self.chunk_layers.clear()

        for mod in filter(lambda x: x.loaded, ChunkRegistry().registered_modules.values()):
            for c in mod.loaded_chunks.values():
                self.add_layer(self.get_root_layer(), c)

    def _edit_target_changed(self, notice: Usd.Notice.StageEditTargetChanged, sender):
        stage = notice.GetStage()
        if stage:
            self.stage = stage

            root = self.get_root_layer().identifier
            sesh = stage.GetRootLayer()
            if root not in sesh.subLayerPaths:
                sesh.subLayerPaths.append(root)

    def _chunk_deleted(self, chunk_hash):
        if lay := self.get_layer(chunk_hash):
            sublayer_paths = self.get_root_layer().subLayerPaths.copy()
            for i, sublayer in reversed(list(enumerate(sublayer_paths))):
                if sublayer == lay.identifier:
                    sublayer_paths.pop(i)

            self.get_root_layer().subLayerPaths = sublayer_paths  # type: ignore
            self.chunk_layers.pop(chunk_hash)

    def _flags_changed(self, notice, sender):
        c = sender
        if not isinstance(c, chunk.Chunk):
            return

        if c not in self.chunk_layers:
            return

        layer = self.chunk_layers.get(c)
        if layer is None:
            return

        if c.has_state(ChunkFlags.State.DISABLED):
            layer.SetMuted(True)
        elif self.is_muted(c):
            layer.SetMuted(False)
