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

from types import ModuleType
from typing import Iterable, Optional, Set, Any, Callable, List, Dict, TYPE_CHECKING
from importlib import util
from collections import defaultdict

import glob
import os
import traceback
import sys

from pxr import Sdf, Usd

from usd_chunks.chunk_notice import ChunkNoticeAlias, RegistrySingleton, SceneSender, send_notice, NoticeConnection
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk

if TYPE_CHECKING:
    from usd_chunks.chunk_core import ChunkConfig, ChunkCommon, ChunkHash

from timeit import default_timer as timer


class ChunkModule:
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
        module_file: str = "",
    ):
        self.module = module
        self.module_name = module_name
        self.module_file = module_file

        self.loaded = False
        self.loaded_chunks: Dict[str, chunk.Chunk] = {}
        self.config: ChunkConfig = chunk.DefaulConfig()
        # self.config_listener: Optional[NoticeConnection] = None
        self.listeners: List[NoticeConnection] = []
        self.func: Optional[Callable[[], chunk.Chunk | List[chunk.Chunk]]] = None

    def __hash__(self):
        return hash(self.module_name)

    def __eq__(self, other):
        return self.module_name == other.module_name

    def config_changed(self, notice, sender):
        pass

    def unload(self, chunk_name: Optional[str] = None):
        """
        Unload this module by deleting the associated chunk object
        """
        if not self.loaded or len(self.loaded_chunks) == 0:
            return

        if chunk_name is not None:
            if not (c := self.get_chunk(chunk_name)):
                return

            if len(self.loaded_chunks) == 1:
                self.loaded = False

            self.loaded_chunks.pop(chunk_name)
            c.request_delete()
            send_notice(ChunkNoticeAlias.CHUNK_UNLOADED, c, data=c)

        else:
            self.loaded = False
            chunks = list(self.loaded_chunks.values())
            for c in chunks:
                self.loaded_chunks.pop(c.name)
                print("UNLOAD CHUNK", c.name)
                c.request_delete()
                send_notice(ChunkNoticeAlias.CHUNK_UNLOADED, c, data=c)


    def load_config(self) -> bool:
        if hasattr(self.module, "config"):
            try:
                func = getattr(self.module, "config")
                self.config = func()
            except Exception as e:
                print(f"Failed to load config for {self.module_name}:", e)
                return False

        return True

    def holds_chunk(self, chunk_name: str) -> bool:
        return chunk_name in self.loaded_chunks

    def get_chunk(self, chunk_name: str) -> chunk.Chunk | None:
        return self.loaded_chunks.get(chunk_name)

    def _add_chunk(self, c: chunk.Chunk):
        if not (isinstance(c, chunk.Chunk)):
            print(f"main() in module: {self.module_name} does not return a Chunk object")
            self.loaded = False
            return False

        if self.config:
            c._config = self.config

        if self.holds_chunk(c.name):
            print(
                f"Module {self.module_name} created chunks with duplicate names {c.name}. Will unload and replace old chunk with new"
            )
            self.unload()

        if c.name in ChunkRegistry().get_chunk_names():
            print(
                f"Module {self.module_name} created chunk {c.name}. A chunk with this name is already registered. Will not load this module"
            )
            self.loaded = False
            return False

        self.loaded_chunks[c.name] = c
        self.listeners.append(NoticeConnection(self.pop_chunk, ChunkNoticeAlias.CHUNK_DELETED, c))

        try:
            c.check_hierarchy()
        except Exception as e:
            print(e)
            self.loaded = False
            return False

        return True

    def pop_chunk(self, notice, sender):
        if sender in self.loaded_chunks.values():
            self.loaded_chunks.pop(sender.name)

    def load(self, func_name: str = "main") -> bool:
        self.unload()

        if not hasattr(self.module, func_name):
            print(f"Function: {func_name} in module: {self.module_name} is not defined")
            self.loaded = False
            return False

        # if reqs := self.config.requirements:
        #     loaded = set(ChunkRegistry().registered_modules.keys())
        #     unloaded = [x for x in reqs if x not in loaded]
        #     if unloaded:
        #         raise ModuleNotFoundError(f"Module {self.module_name} is missing requirements: {unloaded}")
        try:
            self.func = getattr(self.module, func_name)
            if not self.func:
                self.loaded = False
                return False

            start = timer()
            retval: Any = self.func()
            end = timer()
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later§
            print(f"Failed to run module {self.module_name}:", traceback.format_exc())
            self.loaded = False
            return False

        try:
            for c in retval:
                if not self._add_chunk(c):
                    return False
        except TypeError:
            if not self._add_chunk(retval):
                return False

        self.loaded = True

        msg = ""
        for c in self.loaded_chunks.values():
            send_notice(ChunkNoticeAlias.CHUNK_LOADED, c, data=c)
            msg += f"   {c.name}\n"

        print(f"loaded module: {self.module_name}. Module created chunks:\n{msg}Load took {end - start} seconds")
        return True

    def create(self):
        if not self.loaded:
            print("Trying to create a chunk from an unloaded module. Consider running load() first")
            return

        if not self.func:
            return

        names = ChunkRegistry().get_chunk_names()
        retval = self.func()

        loaded = []
        try:
            loaded.extend(retval)  # type: ignore
        except TypeError:
            loaded.append(retval)

        for c in loaded:
            c.name = chunk.Chunk.get_valid_child_name(c.name, names)
            names.add(c.name)

            if not self._add_chunk(c):
                continue

            send_notice(ChunkNoticeAlias.CHUNK_LOADED, c, data=c)
        return loaded

    def should_load(self) -> bool:
        if module_filter := os.environ.get("USD_CHUNK_FILTER"):
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
    chunk modules are scanned for in the USD_CHUNK_DIR directory.

    Attributes:
        registered_directories (Set[str]) : A set of directories we have searched
        registered_modules (Dict[str, ChunkModule]) : A mapping between module names and modules
        scene (SceneSender) : An instance of the SceneSender for accesing the scene
        plugin (str) : A string identifier of the currently loaded plugin
    """

    registered_directories: Set[str] = set()
    registered_modules: Dict[str, ChunkModule] = {}
    scene: SceneSender
    plugin: str

    initialized: bool = False

    def initialize(self, scene_sender: SceneSender, plugin: str, unloaded: bool = False) -> None:
        """Initialize the FileReader with the working directory.

        Args:
            scene_sender (SceneSender): A SceneSender instance for access to the scene.
                We hold a reference to it here in order to keep it alive
            plugin (str): A string identifying the currently used plugin
            unloaded (bool): If the modules should not be loaded
        """

        self.scene = scene_sender
        self.plugin = plugin

        self.register_chunk_environ_directories()
        self.register_chunk_modules(self.registered_directories, unloaded)

        c = 0
        for mod in self.registered_modules.values():
            c += 1 if mod.loaded else 0

        print(f"Found {len(self.registered_modules)} chunks. Loaded {c}")
        self.initialized = True

    def __repr__(self):
        """Return a string representation of the FileReader."""
        return f"FileReader(registered_modules={self.registered_modules})"

    def __str__(self):
        """Return a string representation of the FileReader."""
        return f"FileReader with {len(self.registered_modules)} registered modules."

    def __len__(self):
        """Return the number of registered modules."""
        return len(self.registered_modules)

    def register_chunk_environ_directories(self):
        """Register the USD_CHUNK_DIR directories to find Python modules."""
        environ_dirs = set(
            os.environ.get(
                "USD_CHUNK_DIR", os.path.sep.join([os.path.dirname(os.path.dirname(__file__)), "scripts"])
            ).split(os.pathsep)
        )

        for chunk_dir in environ_dirs:
            self.register_directory(chunk_dir)

    def register_directory(self, chunk_dir: str):
        """Add a single directory to the register

        Args:
            chunk_dir (str): A pathlike string to add
        """
        if not os.path.isdir(chunk_dir):
            print(f"USD_CHUNK_DIR:{chunk_dir} directory not found.")
            return

        if chunk_dir in self.registered_directories:
            # directory already loaded
            return

        sys.path.append(chunk_dir)
        self.registered_directories.add(chunk_dir)

    def register_chunk_modules(self, working_dirs: Iterable[str], unloaded: bool = False):
        """Read and register Python files in the working directory.

        Args:
            working_dirs (Iterable[str]): The directories containing the Python chunk files to register.
        """

        for working_dir in working_dirs:
            # Loop over all directories of the working_dirs directory
            chunk_python_files = glob.glob(os.path.sep.join([working_dir, "**", "*.py"]), recursive=True)
            for module_path in chunk_python_files:
                if not module_path.endswith(".py"):
                    continue
                if not os.path.isfile(module_path):
                    continue

                module_name = os.path.relpath(module_path, working_dir).replace(os.path.sep, ".").replace(".py", "")

                mod = ChunkModule(module_name=module_name, module_file=module_path)

                if self.register_module(mod):
                    self.registered_modules[mod.module_name] = mod

        if not unloaded:
            self._load_modules()

    def _load_modules(self):
        for mod in self.registered_modules.values():
            if mod.should_load() and mod.should_load_for_plugin(self.plugin):
                mod.load()

    def register_module(self, mod: ChunkModule):
        """
        Load the actual module and runs the main function

        Args:
            mod (ChunkModule): The ChunkModule to load and run
        """

        mod.loaded = False

        spec = util.spec_from_file_location(mod.module_name, mod.module_file)
        if not spec or not spec.loader:
            return False

        module = util.module_from_spec(spec)
        try:
            sys.modules[mod.module_name] = module
            spec.loader.exec_module(module)
            module_attributes = dir(module)
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later§
            print(f"Failed to load module {mod.module_name}:", traceback.format_exc())
            return False
        else:
            mod.module = module

        if "main" not in module_attributes:
            # Only python files with a main functions are valid chunk entrypoints.
            return False

        if not mod.load_config():
            # Returning True because the module should be added to registered_modules
            # Although not loaded
            return True

        if not (mod.should_load() or mod.should_load_for_plugin(self.plugin)):
            return True

        return True

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
            mod.unload()

            if not mod.loaded:
                self.register_module(mod)

        self._load_modules()


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
        self._load_listener = NoticeConnection(self._chunk_loaded, ChunkNoticeAlias.CHUNK_LOADED, globally=True)
        self._unload_listener = NoticeConnection(self._chunk_unloaded, ChunkNoticeAlias.CHUNK_UNLOADED, globally=True)
        self._delete_listener = NoticeConnection(self._chunk_deleted, ChunkNoticeAlias.CHUNK_DELETED, globally=True)
        self._flags_listener = NoticeConnection(self._flags_changed, ChunkNoticeAlias.STATE_CHANGED, globally=True)

        self.chunk_layers: Dict[ChunkHash, Sdf.Layer] = {}

        # When initialized, the stage may be None as the chunks are loaded prior to loading the stage
        self.stage: Usd.Stage = stage
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
        if not self.stage:
            raise AttributeError("Stage is not initialized")
        return self.stage.GetSessionLayer()

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

        if not self.stage:
            raise AttributeError("ChunkLayerManager stage is not initialized. Can't open layer")

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
        layer_id = c.get_layer_identifier()
        layer = Sdf.Layer.CreateAnonymous(layer_id)

        self.chunk_layers[c._hashable] = layer
        target_layer.subLayerPaths.insert(0, layer.identifier)

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

    def clear_layer(self, layer_id: str):
        """
        Clear a layer and remove from sublayer list

        Args:
            layer_id (str): The layer to remove
        """
        if not self.stage:
            return

        if layer := Sdf.Layer.Find(layer_id):
            layer.Clear()

    def reload_all(self, stage: Optional[Usd.Stage] = None):
        """
        clear the chunk layer stack and recreate them

        Args:
            stage (Optional[Usd.Stage], optional): An optional stage. This is used when the stage is just loaded and the scene does not yet have access to it. Defaults to None.
        """
        stage = stage if stage else self.stage
        if not stage:
            return

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

    def _chunk_loaded(self, notice, sender: "chunk.Chunk"):
        if not self.stage:
            return

        self.add_layer(self.get_root_layer(), sender)

    def _chunk_unloaded(self, notice, sender: "chunk.Chunk"):
        if not self.stage:
            return

        if layer := self.chunk_layers.get(sender._hashable):
            sublayer_paths = self.get_root_layer().subLayerPaths.copy()

            for i, sublayer in reversed(list(enumerate(sublayer_paths))):
                if sublayer == layer:
                    sublayer_paths.pop(i)

            self.get_root_layer().subLayerPaths = sublayer_paths  # type: ignore

    def _chunk_deleted(self, notice, sender):
        pass

    def _flags_changed(self, notice, sender):
        c = sender
        if not isinstance(c, chunk.Chunk):
            return

        if not self.stage:
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
