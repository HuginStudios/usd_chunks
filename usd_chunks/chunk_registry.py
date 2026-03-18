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
import os
import sys
from pathlib import Path
from importlib import util
from collections import defaultdict
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Set, Generic, TypeVar, overload

from pxr import Sdf, Usd, UsdUtils

from usd_chunks import chunk, chunk_serial
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton
from usd_chunks.chunk_core import SceneSender

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
        self._found_main = False

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
        if node.name == "main":
            self._found_main = True
        return node

    def visit_Import(self, node: ast.Import) -> Any:
        # Makes sure that 'import usd_chunks ...' works
        mymod = self.__module__.split(".")[0]
        if any([mymod in x.name.split(".") for x in node.names]):
            return self._exec_node(node)
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        # Makes sure that 'from usd_chunks import ...' works
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
        self._found_main = False

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
        self._found_main = transformer._found_main

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
        self.loaded_chunks: weakref.WeakValueDictionary[str, chunk.Chunk] = weakref.WeakValueDictionary()
        self.config: ChunkConfig = chunk.DefaulConfig()
        self.func: Optional[Callable[[], chunk.Chunk | List[chunk.Chunk]]] = None

        self._should_serialize_chunks = True

        self._exec_module()

    @staticmethod
    def from_chunk_definition(file: str | Path, name: Optional[str] = None) -> List["ChunkModule"]:
        """
        Create a ChunkModule from a file and module name. The method for creating the module varies based on the file extension

        Returns:
            List[ChunkModule]: A list of modules that was created. For .chum files, this list may contain multiple ChunkModules
        """
        p = Path(file)

        if name is None:
            name = p.stem

        if p.suffix == chunk_serial.FileExtensions.PY_MODULE:
            # When importing .py files, we don't need to deserialize, as the loading of the module
            # can be managed by the ChunkModule
            return [ChunkModule(module_name=name, module_file=file)]

        serial = chunk_serial.load_file(p)

        res = []
        if not serial:
            return res

        if not (data := chunk_serial.deserialize_json(serial)):
            return res

        if p.suffix == chunk_serial.FileExtensions.CHUNK_MODULE:
            for mod_cnstr in data.get("modules", []):
                mod_cnstr.serial.kwargs["module_name"] = name

                mod = mod_cnstr.construct()
                if not isinstance(mod, ChunkModule):
                    logger.error(f"Failed to construct module {mod_cnstr.serial.kwargs.get('module_name')}")
                    return []

                res.append(mod)

        elif p.suffix == chunk_serial.FileExtensions.CHUNK:
            pass

        return res

    def __repr__(self) -> str:
        return f"ChunkModule(module_name='{self.module_name}', module_file='{self.module_file.as_posix()}')"

    def serialize_kwarg(self, name: str, default: Any) -> Any:
        # Override to pass current values to kwargs
        if name == "module_name":
            return self.module_name
        if name == "module_file":
            return self.module_file.as_posix()
        if name == "module":
            return self.module
        return super().serialize_kwarg(name, default)

    def post_serial_setattr(self, name: str, value: Any) -> None:
        if name == "loaded_chunks":
            for c in value:
                self._add_chunk(c)
            return

        return super().post_serial_setattr(name, value)

    def should_serialize_attribute(self, name: str, value: Any) -> chunk_serial.Tuple[bool, Any]:
        if name == "loaded_chunks" and self._should_serialize_chunks:
            return (True, list(self.loaded_chunks.values()))

        return (False, None)

    def _add_chunk(self, c: chunk.Chunk):
        if not (isinstance(c, chunk.Chunk)):
            logger.warning(f"main() in module: {self.module_name} does not return a Chunk object")
            self.loaded = False
            return False

        if self.config:
            self.config.module = self.module_name
            c._config = self.config

        if ChunkRegistry().scene.add_chunk(c):
            self.loaded_chunks[c.name] = c
        return True

    def _exec_module(self):
        if type(self.module) is chunk_serial.RuntimeModule:
            if not self.loaded:
                transformer = _ConfigTransformer(self.module._code, self.module.__dict__, self)
                transformer.visit(ast.parse(self.module._code))

                if not transformer._found_main:
                    logger.warning(f"main function is not defined in module {self.module_name}. Skipping")
                    return False

            # if self.module_name not in sys.modules:
            #     sys.modules[self.module_name] = self.module
            sys.modules[self.module_name] = self.module

        elif not self.module_file.exists():
            logger.error(f"Could not load module from file {self.module_file}")
            return False

        elif self.module_file.suffix == chunk_serial.FileExtensions.PY_MODULE:
            spec = util.spec_from_file_location(self.module_name, self.module_file)
            if not spec or not spec.loader:
                logger.warning(f"Module spec or loader failed for module {self.module_name}. Skipping")
                return False

            loader = ChunkLazyConfigLoader(self.module_name, str(self.module_file), spec.loader, self)
            spec.loader = loader
            module = util.module_from_spec(spec)

            try:
                spec.loader.exec_module(module)
            except Exception:
                # This module failed to load. Set to file_name so we can try loading it again later§
                logger.exception(f"Failed to load module {self.module_name}")
                return False

            # found_main = loader._found_main
            if not loader._found_main:
                logger.warning(f"main function is not defined in module {self.module_name}. Skipping")
                return False

            self.module = module
            # Add the module to sys.modules
            sys.modules[self.module_name] = self.module

        elif self.module_file.suffix == chunk_serial.FileExtensions.CHUNK_MODULE:
            if _m := ChunkModule.from_chunk_definition(self.module_file):
                self.module = _m[0].module
                if self.module:
                    sys.modules[self.module.__name__] = self.module

        if not self.module:
            return False

        return True

    def _install_deps(self):
        if not self.config.requirements:
            return

        dep_reg = ChunkDependencyManager()
        if not dep_reg.initialized:
            dep_reg.initialize()

        dep_reg.install_packages(self.config.requirements)

    def _pre_load(self):
        if not self.loaded:
            if not self._exec_module():
                return False

        self._install_deps()

        if type(self.module) is chunk_serial.RuntimeModule:
            self.module.compile()
        try:
            self.func = getattr(self.module, "main")
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later
            logger.exception(f"Failed to run module {self.module_name}")
            self.loaded = False
            return False

        self.loaded = True
        return True

    def load(self) -> List[chunk.Chunk]:
        """
        Loads the module. This ensures that the module is properly created. If Already loaded, it's preffered to
        call create() directly, to skip some initialization stuff that should only be called once

        Returns:
            bool: True if loaded successfully, Fale otherwise
        """
        if not self._pre_load():
            logger.warning(f"Could not load module {self.module}")
            return []

        start = timer()

        res = self.create()
        self.loaded = len(res) > 0
        end = timer()

        logger.info(f"loaded module: {self.module_name}. Load took {end - start} seconds")
        return res

    def unload(self):
        """
        Deletes any chunk created from this module and sets loaded to False
        """
        chunks = list(self.loaded_chunks.values())
        for c in chunks:
            c.request_delete()
        self.loaded = False

    def create(self) -> List[chunk.Chunk]:
        """
        Runs the main function from the module and adds the chunks returned from the main function to the
        ChunkRegistry scene

        Returns:
            Bool: True on success
        """
        if not self.loaded:
            logger.warning("Trying to create a chunk from an unloaded module. Consider running load() first")

        loaded = []

        if self.func:
            try:
                retval = self.func()
            except Exception:
                logger.exception("Failed to run main function from module %s", self.module_name)
                return []
            try:
                loaded.extend(retval)  # type: ignore
            except TypeError:
                loaded.append(retval)
        else:
            logger.warning(f"module {self.module_name} does not have a function for creating chunks")
            return []

        for c in loaded:
            self._add_chunk(c)

        return loaded

    def should_load(self) -> bool:
        """
        Check if this module should load. Checks if the module name exists in USD_CHUNK_LOAD_FILTER

        Returns:
            bool: True if this module should load
        """
        if module_filter := USD_CHUNK_LOAD_FILTER():
            if self.module_name not in module_filter:
                return False
        if not self.config:
            return False
        return True

    def should_load_for_plugin(self, plugin_name: str) -> bool:
        """
        Checks if the plugin_name exists in config.load_for_plugin. If the load_for_plugin is empty
        then assume the module is safe to load

        Args:
            plugin_name (str): The name of the plugin

        Returns:
            bool: True if the ChunkModule can be used with the plugin plugin_name
        """
        if not self.config:
            return False
        if self.config.load_for_plugin and plugin_name not in self.config.load_for_plugin:
            return False
        return True

    def export(self, path: str | Path):
        """
        Export this ChunkModule as a .py or .chum file. Any other extension will print a warning and fail

        Args:
            path (str | Path): The path to export. Should be either .py or .chum
        """
        p = Path(path)

        if not self.module:
            return

        if p.suffix == chunk_serial.FileExtensions.PY_MODULE:
            serial = chunk_serial.serialize_py(self.module)

        elif p.suffix == chunk_serial.FileExtensions.CHUNK_MODULE:
            old_path = self.module_file
            old_name = self.module_name

            self.module_file = p
            self.module_name = p.stem

            self._should_serialize_chunks = False

            data: Dict[str, Any] = {"modules": [self]}
            serial = chunk_serial.serialize_json(data, True)

            self.module_file = old_path
            self.module_name = old_name

            self._should_serialize_chunks = True

        else:
            logger.warning(f"wrong file extension {path}")
            return

        chunk_serial.save_file(serial, path)


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

    def initialize(self, scene_sender: SceneSender, plugin: str, scene_file: Optional[Path] = None) -> None:
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
        self.register_chunk_modules(self.registered_directories.copy())

        self._scene_file: Optional[Path] = None
        if scene_file:
            self.open_scene(scene_file)

        c = len(self.get_chunk_modules(loaded=True))
        logger.info(f"Found {len(self.registered_modules)} chunks. Loaded {c}")
        self.initialized = True

    def get_scene_file(self):
        """
        Get the currently loaded scene file. This may be None if no scene file was loaded

        Returns:
            Path | None: the path to a scene file, or None if not set
        """
        return self._scene_file

    def set_scene_file(self, path: str | Path):
        """
        Set the current scene file. The file extension of path should be .json. This will not
        load the content of the file. To load the content of the file, use open_scene() instead.

        Args:
            path (str | Path): A .json file path to use as scene file.
        """
        if (p := Path(path)) and p.suffix != chunk_serial.FileExtensions.SCENE:
            logger.warning(f"Unsupported scene file extension: {path}")
            return

        self._scene_file = Path(path)

    def register_chunk_environ_directories(self):
        """Register the USD_CHUNK_DIRS directories to find Python modules."""

        for chunk_dir in USD_CHUNK_DIRS():
            self.register_directory(chunk_dir)

    def register_directory(self, chunk_dir: str | Path):
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

        sys.path.append(str(chunk_dir))
        self.registered_directories.add(p)

    def register_chunk_modules(self, paths: Iterable[Path]):
        """Recursively register chunk module files (.py, .chum) from paths. Paths should be a sequence of
        directory paths that contain ChunkModule files

        Args:
            paths (Iterable[Path]): The directories containing the chunk modules to register.
        """
        chum_mods = []

        for working_dir in paths:
            # Loop over all directories of the working_dirs directory
            extensions = (chunk_serial.FileExtensions.CHUNK_MODULE, chunk_serial.FileExtensions.PY_MODULE)

            for module_path in filter(lambda x: x.suffix in extensions, Path(working_dir).glob("**/*")):
                rel = (module_path.parent / module_path.stem).relative_to(working_dir)
                module_name = str(rel).replace(os.path.sep, ".")

                if module_path.suffix != chunk_serial.FileExtensions.PY_MODULE:
                    # .py modules should be loaded first
                    chum_mods.append((module_path, module_name))
                    continue

                for mod in ChunkModule.from_chunk_definition(module_path, module_name):
                    self.register_module(mod)

        # Loop over the .chum modules and add them after all .py modules has been added. This is because python modules should
        # be prioritized if duplicate module names are found
        for module_path, module_name in chum_mods:
            for mod in ChunkModule.from_chunk_definition(module_path, module_name):
                self.register_module(mod)

    def register_module(self, module: ChunkModule) -> ChunkModule | None:
        """
        Register a ChunkModule. This will wnsure that the module is kept alive and accessible

        Args:
            module (ChunkModule): The module to add to the registry

        Returns:
            ChunkModule | None: The registered module, or None if failed
        """
        if not module.module or not module.should_load_for_plugin(self.plugin) or not module.should_load():
            logger.info(f"Skipping {module.module_name}. Module should not be registered in this context")
            return

        name = module.module_name

        if name in self.registered_modules:
            logger.warning(f"Skipping {module.module_file}. Duplicate module: {name}")
            return

        self.registered_modules[name] = module
        return self.registered_modules[name]

    def get_chunk_module(self, module_name: str) -> ChunkModule | None:
        """Get the ChunkModule object in the registry where module_name matches the
        stored name of the module. The module_name should typically be the name of the
        python script, relative to USD_CHUNK_DIRS - as one would import the module. F.e:

        A/B/C/foo.py

        USD_CHUNK_DIRS = A
        get_chunk_module('B.C.foo')

        USD_CHUNK_DIRS = A/B
        get_chunk_module('C.foo')

        Args:
            module_name (str): The name of the module to find

        Returns:
            ChunkModule | None: A found ChunkModule object with name, or None if not found
        """
        return self.registered_modules.get(module_name)

    def get_chunk_modules(self, loaded=False, sort_result=False) -> List[ChunkModule]:
        v = list(self.registered_modules.values())
        if loaded:
            v = [x for x in v if x.loaded]
        if not sort_result:
            return v
        return sorted(v, key=lambda x: x.module_name.split(".")[-1])

    def import_chunk(self, path: str | Path):
        """
        Import a chunk module from path. The file extension of path should be either .py or .chum.

        Args:
            path (str | Path): The path to the chunk module
        """
        p = Path(path)
        if p.suffix in (chunk_serial.FileExtensions.CHUNK_MODULE, chunk_serial.FileExtensions.PY_MODULE):
            for mod in ChunkModule.from_chunk_definition(p):
                if mod.module_name not in self.registered_modules:
                    self.register_module(mod)
                mod.load()
            return

        elif p.suffix == chunk_serial.FileExtensions.CHUNK:
            pass

    def export_scene(self, path: str | Path):
        """
        Export the current scene. The result will be written to path, if path is a .json file path.
        The file will contain information about all the loaded chunks and modules, and when loaded
        should recrete the scene 1:1

        Args:
            path (str | Path): A target file path which to write to
        """
        p = Path(path)
        if p.suffix != chunk_serial.FileExtensions.SCENE:
            return

        # data = {"modules": self.get_chunk_modules(True), "chunks": self.scene.get_chunks()}
        data: Dict[str, Any] = {"modules": self.get_chunk_modules(True)}

        lreg = ChunkLayerManager()
        if lreg.initialized:
            data["layers"] = lreg.save_layers(p.parent)
            # layers = lreg.save_layers(p.parent)
            # data["layers"] = [Path(x).relative_to(p.parent).as_posix() for x in layers]
            # data["layers"] = {hash(x): str(y) for x, y in lreg.chunk_layers.items()}

        serial = chunk_serial.serialize_json(data, False)
        chunk_serial.save_file(serial, path)

    def save_scene(self):
        """
        Calls export_scene with the current scene file as target, meaning this will override the currently loaded scene.
        If no scene is set, this will print a warning and do nothing.
        """
        if not (f := self.get_scene_file()):
            logger.warning("No scene file set")
            return

        self.export_scene(f)
        self.scene._modified = False

    def open_scene(self, path: str | Path):
        """
        Loads a scene file. This will clear the loaded chunks and create new ones from the file.
        If the file is a .py file, the module will be created

        A .py file will be loaded as a regular module - like those in USD_CHUNK_DIRS

        Args:
            path (str | Path): A path to a file on disk which to load
        """

        p = Path(path)
        if not p.exists() or p.suffix != chunk_serial.FileExtensions.SCENE:
            return

        if self.scene.is_modified():
            if not self.scene.request_save():
                return

        # Clear the current scene
        self.scene.clear()
        for mod in self.get_chunk_modules():
            mod.unload()

        serial = chunk_serial.load_file(path)

        # Deserialize json files
        if not serial or not (data := chunk_serial.deserialize_json(serial)):
            return

        for mod_cnstr in data.get("modules", []):
            if not (mod_name := mod_cnstr.serial.kwargs.get("module_name")):
                continue

            mod = mod_cnstr.construct(False)

            if not isinstance(mod, ChunkModule):
                logger.error(f"Return value from serial is not a ChunkModule: {mod}")
                continue

            if not mod._pre_load():
                logger.error(f"Failed to preload: {mod}")
                continue

            mod = mod_cnstr.post_constuct()
            self.registered_modules[mod_name] = mod

        for c in self.scene.get_chunks():
            c.check_hierarchy()

        self.scene._modified = False
        self.set_scene_file(p)

        lreg = ChunkLayerManager()
        if not lreg.initialized:
            return

        for rel_layer_path, c_cnstr in data.get("layers", {}).items():
            layer_path = p.parent / rel_layer_path
            if not layer_path.exists():
                logger.warning(f"Layer {rel_layer_path} is not relative to {p.parent}. Skipping")
                continue

            c = c_cnstr.construct()

            if not isinstance(c, chunk.ChunkCommon):
                logger.warning(f"Could not load layer {rel_layer_path}. The referenced chunk was not found")
                continue

            layer = lreg.open_layer(layer_path)
            if not layer:
                logger.warning(f"Could not load layer {layer_path}")
                continue

            c_layer = lreg.get_or_create_layer(c)
            if not c_layer:
                logger.warning(f"Could not load layer for chunk {c.name}")
                continue

            UsdUtils.StitchLayers(c_layer, layer)


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

    def save_layers(self, path: str | Path) -> Iterable[str]:
        p = Path(path)
        if not p.is_dir():
            logger.warning(f"Can't save layers. Path is not a directory {path}")
            return []

        layers = {}
        for key, layer in self.chunk_layers.items():
            if not layer:
                continue

            name = layer.GetDisplayName().split("_")[0]
            file = f"{p}/{name}.usda"
            layer.Export(file)

            rpath = Path(file).relative_to(p).as_posix()
            layers[rpath] = key
        return layers

    def open_layer(self, path: str | Path):
        p = Path(path)
        if not p.exists():
            return
        return Sdf.Layer.FindOrOpen(str(path))

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

        h = c.get_hash()
        if layer := self.chunk_layers.get(h):
            return layer

        layer_id = c.get_layer_identifier()
        layer = Sdf.Layer.CreateAnonymous(layer_id)

        self.chunk_layers[h] = layer
        target_layer.subLayerPaths.insert(0, layer.identifier)

        # We must make sure to delete the layer of the chunk AFTER all other notices are sent.
        # For this reason, we can't rely on callbacks. Instead we can register a finalizer, so we delete
        # the layer once the chunk is actually deleted
        weakref.finalize(c, self._chunk_deleted, h)

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

        for c in ChunkRegistry().scene.get_chunks():
            self.add_layer(self.get_root_layer(), c)

        # for mod in filter(lambda x: x.loaded, ChunkRegistry().registered_modules.values()):
        #     for c in mod.loaded_chunks.values():
        #         self.add_layer(self.get_root_layer(), c)

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
