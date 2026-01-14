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
from typing import Iterable, Optional, Set, Any, Dict
from importlib import util
from dataclasses import dataclass

# import inspect
import glob
import os
import traceback
import sys

from . import chunk
from .chunk_notice import ChunkNoticeAlias, RegistrySingleton, SceneSender, send_notice

from timeit import default_timer as timer


@dataclass
class ChunkModule:
    """
    A dataclass for storing information about chunk modules. Used by ChunkRegistry

    Args:
        module (ModuleType | None): The loaded chunk module
        loaded (bool): If the module loaded successfully
        module_name (str): The name of the module
        module_file (str): The original, absolute, path to the python module
        loaded_chunk (chunk.Chunk | None): The loaded chunk retrieved from running module.main()
    """

    module: Optional[ModuleType] = None
    loaded: bool = False
    module_name: str = ""
    module_file: str = ""
    loaded_chunk: Optional[chunk.Chunk] = None

    def __hash__(self):
        return hash(self.module_name)

    def __eq__(self, other):
        return self.module_name == other.module_name

    def run(self, func_name: str = "main", *args, **kwargs) -> None | chunk.Chunk:
        """Call function func_name in the loaded module and set self.loaded_chunk to the returned value.
        Assumes the function returns a chunk.Chunk object

        Args:
            func_name (str, optional): Name of the function inside the module to call. Defaults to "main".

        Returns:
            None: If failed to call the function
            chunk.Chunk: If the main function returned a Chunk object
            chunk_qt.ChunkWidget: If the function returned a custom widget
        """

        func = getattr(self.module, func_name)

        start = timer()
        retval: Any = func(*args, **kwargs)
        end = timer()

        if not (isinstance(retval, chunk.Chunk)):
            print(f"Function: {func_name} in module: {self.module_name} does not return a Chunk object")
            self.loaded = False
            return

        try:
            retval.check_hierarchy()
        except Exception as e:
            print(e)
            self.loaded = False
            return

        self.loaded_chunk = retval

        print(f"Loaded Chunk {self.loaded_chunk.name}. Load took {end - start} seconds")
        return retval


class ChunkRegistry(metaclass=RegistrySingleton):
    """A singeton class to read and register chunk modules. By default,
    chunk modules are scanned for in the USD_CHUNK_DIR directory.

    Attributes:
        registered_directories (Set[str]) : A set of directories we have searched
        registered_modules (Dict[str, ChunkModule]) : A mapping between module names and modules
        scene (SceneSender) : An instance of the SceneSender for accesing the scene
    """

    registered_directories: Set[str] = set()
    registered_modules: Dict[str, ChunkModule] = {}
    scene: SceneSender

    def initialize(self, scene_sender: SceneSender) -> None:
        """Initialize the FileReader with the working directory.

        Args:
            scene_sender (SceneSender): A SceneSender instance for access to the scene.
                We hold a reference to it here in order to keep it alive
        """

        self.scene = scene_sender

        self.register_chunk_environ_directories()
        self.register_chunk_modules(self.registered_directories)
        print(f"Number of Registered Chunks {len(self.registered_modules)}")

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
            print(chunk_dir)
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

    def register_chunk_modules(self, working_dirs: Iterable[str]):
        """Read and register Python files in the working directory.

        Args:
            working_dir: The directories containing the Python chunk files to register.

        Returns:
            List[ModuleInfo]: A list of ModuleInfo objects containing
                the loaded modules and their file names.
        """

        new_modules: Dict[str, ChunkModule] = {}

        for working_dir in working_dirs:
            # Loop over all directories of the working_dirs directory
            chunk_python_files = glob.glob(os.path.sep.join([working_dir, "**", "*.py"]), recursive=True)
            for module_path in chunk_python_files:
                if not module_path.endswith(".py"):
                    continue
                if not os.path.isfile(module_path):
                    continue

                module_name = os.path.relpath(module_path, working_dir).replace(os.path.sep, ".").replace(".py", "")
                mod = ChunkModule(module_name=module_name, module_file=module_path, loaded=False)
                self.load_module(mod)

                new_modules[mod.module_name] = mod

        self.registered_modules |= new_modules

    def load_module(self, mod: ChunkModule):
        """
        Load the actual module and runs the main function

        Args:
            mod (ChunkModule): The ChunkModule to load and run
        """
        spec = util.spec_from_file_location(mod.module_name, mod.module_file)
        if not spec or not spec.loader:
            return

        module = util.module_from_spec(spec)
        try:
            sys.modules[mod.module_name] = module
            spec.loader.exec_module(module)
            module_attributes = dir(module)
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later§
            print(f"Failed to load module {mod.module_name}:", traceback.format_exc())
            mod.loaded = False
            return
        else:
            mod.module = module

        if "main" not in module_attributes:
            # Only python files with a main functions are valid chunk entrypoints.
            mod.loaded = False
            return

        try:
            mod.run("main")
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later§
            print(f"Failed to run module {mod.module_name}:", traceback.format_exc())
            mod.loaded = False
        else:
            mod.loaded = True
            send_notice(
                ChunkNoticeAlias.CHUNK_LOADED,
                mod.loaded_chunk,
                data=(mod.loaded_chunk),
            )

    def unload_chunk_module(self, mod: ChunkModule):
        """
        Unload a ChunkModule

        Args:
            mod (ChunkModule): The module to unload
        """
        if not mod:
            return
        if not mod.loaded or not mod.loaded_chunk:
            return

        mod.loaded_chunk.request_delete()
        mod.loaded_chunk = None
        mod.loaded = False

    def unload_chunk(self, chunk_name: str):
        """
        Similarly to unload_chunk_module(), but this takes the name of a chunk
        instead of a ChunkModule object

        Args:
            chunk_name (str): The name of the chunk we wish to unload
        """
        for mod in self.registered_modules.values():
            if mod.loaded_chunk and mod.loaded_chunk.name == chunk_name:
                self.unload_chunk_module(mod)

    def get_chunk_module(self, module_name: str) -> ChunkModule | None:
        """Get the ChunkModule object in the registry where module_name matches the
        stored name of the module. The module_name should typically be the name of the
        python script. If the module failed to load, the module name is stored as the
        path to the python script. To find a failed module, the module_name argument
        should be /full/path/to/module.py

        Args:
            name (str): The name of the module to find

        Returns:
            ChunkModule | None: A found ChunkModule object with name, or None if not found
        """
        if module_name not in self.registered_modules:
            return None
        return self.registered_modules[module_name]

    def get_chunk(self, name: str) -> chunk.Chunk | None:
        """Get a loaded chunk object in the registry

        Args:
            name (str): The name of the chunk to lookup

        Returns:
            chunk.Chunk | None: The chunk object with name, or None if not found
        """

        for mod in self.registered_modules.values():
            if mod.loaded_chunk and mod.loaded_chunk.name == name:
                return mod.loaded_chunk
        return None
