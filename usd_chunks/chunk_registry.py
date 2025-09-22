"""Custom modules for the Usdview UI Framework.

This module provides a framework for dynamically loading and managing custom UI modules
for the Usdview application. It includes functionality to read Python files from a
specified directory, register them as modules,
and create dockable widgets in the Usdview main window.
"""

from typing import List, Callable, Optional, Set, Any
from pxr.Usdviewq import usdviewApi as UsdviewModule
from importlib import util
from dataclasses import dataclass

# import inspect
import glob
import os
import sys

from . import chunk, chunk_notice


@dataclass
class ChunkModule:
    module: Optional[Callable] = None
    failed: bool = False
    module_name: str = ""
    loaded_chunk: Optional[chunk.Chunk] = None

    def __hash__(self):
        return hash(self.module_name)

    def __eq__(self, other):
        return self.module_name == other.module_name

    def run(self, func_name: str = "main", *args, **kwargs) -> None | chunk.Chunk:
        """Call function func_name in the loaded module and set self.loaded_chunk to the returned value.
        Assumes the function returns a chunk.Chunk object

        Args:
            func_name (str, optional): Name of the function insude the module to call. Defaults to "main".

        Returns:
            None: If failed to call the function
            chunk.Chunk: If the main function returned a Chunk object
            chunk_qt.ChunkWidget: If the function returned a custom widget
        """

        func = getattr(self.module, func_name)
        retval: Any = func(*args, **kwargs)

        if not (isinstance(retval, chunk.Chunk)):
            print(f"Function: {func_name} in module: {self.module_name} does not return a Chunk")
            return

        self.loaded_chunk = retval
        return retval


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class ChunkRegistry(metaclass=Singleton):
    """A singeton class to read and register Python Chunk files in specified directories. Will send notices when a new chunk was added.
    Can be accessed at runtime to handle chunks
    """

    registered_directories: Set[str] = set()
    registered_modules: Set[ChunkModule] = set()
    usdview_api: UsdviewModule.UsdviewApi = None
    stage_sender: chunk_notice.UsdviewNotice = None

    def initialize(self, usdview_api: UsdviewModule.UsdviewApi) -> None:
        """Initialize the FileReader with the working directory."""

        self.usdview_api = usdview_api

        # Holds the UsdviewNotice object here to keep it alive
        self.stage_sender = chunk_notice.UsdviewNotice(self.usdview_api)

        self.register_chunk_environ_directories()
        self.register_chunk_modules(self.registered_directories)

    def _primSelectionChanged(self, *args, **kwargs):
        chunk_notice.PrimSelectionChangedNotice().Send()

    def _propSelectionChanged(self, *args, **kwargs):
        chunk_notice.PropSelectionChangedNotice().Send(self)

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
            os.environ.get("USD_CHUNK_DIR", os.path.sep.join([os.path.dirname(__file__), "scripts"])).split(os.pathsep)
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

    def register_chunk_modules(self, working_dirs: List[str]):
        """Read and register Python files in the working directory.

        Args:
            working_dir: The directories containing the Python chunk files to register.

        Returns:
            List[ModuleInfo]: A list of ModuleInfo objects containing
                the loaded modules and their file names.
        """

        registered_modules: Set[ChunkModule] = set()

        for working_dir in working_dirs:
            # Loop over all directories of the working_dirs directory
            chunk_python_files = glob.glob(os.path.sep.join([working_dir, "**", "*.py"]), recursive=True)
            for module_path in chunk_python_files:
                if not module_path.endswith(".py"):
                    continue
                if not os.path.isfile(module_path):
                    continue

                module_name = os.path.relpath(module_path, working_dir).replace(os.path.sep, ".").replace(".py", "")
                file_name = os.path.basename(module_path)
                spec = util.spec_from_file_location(module_name, module_path)
                module = util.module_from_spec(spec)

                try:
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                    module_attributes = dir(module)
                    if "main" not in module_attributes:
                        # Only python files with a main functions are valid chunk entrypoints.
                        continue

                    mod = ChunkModule(module=module, module_name=module_name)
                    mod.run("main", self.usdview_api)
                    registered_modules.add(mod)

                    # Send the notice of a new chunk being added
                    chunk_notice.ChunkLoadedNotice().Send(mod.loaded_chunk)

                except Exception as e:
                    print(f"Failed to load module {module_name}: {e}")
                    registered_modules.add(ChunkModule(module=module, module_name=file_name, failed=True))
        self.registered_modules |= registered_modules

    def get_chunk_module(self, name: str) -> ChunkModule | None:
        """Get the ChunkModule object in the registry with module_name == name. module_name is the name of the python script

        Args:
            name (str): The name of the module to find

        Returns:
            ChunkModule | None: A found ChunkModule object with name, or None if not found
        """
        if name not in self.registered_modules:
            return None
        return self.registered_modules[name]

    def get_chunk(self, name: str) -> chunk.Chunk | None:
        """Get a loaded chunk object in the regirstry based on the name of the chunk

        Args:
            name (str): The name of the chunk to lookup

        Returns:
            chunk.Chunk | None: The chunk object with name, or None if not found
        """
        for c in self.registered_modules:
            if c.failed:
                continue
            if c.loaded_chunk and c.loaded_chunk.name == name:
                return c.loaded_chunk

        return None
