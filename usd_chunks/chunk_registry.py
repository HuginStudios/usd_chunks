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
import functools
import re
import sys
from pathlib import Path
from importlib import util
from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Set,
    Generic,
    TypeVar,
    overload,
    Sequence,
    Tuple,
)
from enum import Enum

from pxr import Sdf, Usd, UsdUtils

from usd_chunks import chunk_cache
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton

import usd_chunks.chunk_core as core
import usd_chunks.chunk_notice as chunk_notice
# from usd_chunks.chunk_core import ChunkBase, ChunkCommon, ChunkConfig, ChunkHash

if TYPE_CHECKING:
    import types

import atexit
import ast
import inspect
import importlib.abc
import logging
import subprocess
import textwrap
from dataclasses import dataclass
from timeit import default_timer as timer

# Suppresses the console window Windows otherwise flashes for every pip subprocess
# spawned from a GUI host (usdview, Maya, etc.) that has no console of its own.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

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

            # Preserve order: env-provided dirs first (they take precedence on
            # duplicate module names -- first registered wins, see
            # ChunkRegistry.register_module), then defaults. Dedupe keeping the
            # first occurrence rather than collapsing to a set (which dropped
            # ordering, so precedence was previously non-deterministic).
            ordered: list = []
            for x in res + default:
                if x and x not in ordered:
                    ordered.append(x)
            return ordered
        return envvar

    def __call__(self, fallback: Optional[_T] = None, /, *args: Any, **kwds: Any):
        return self._get(self.default, fallback)


USD_CHUNK_DIRS = ChunkEnvVar("USD_CHUNK_DIRS", [], True)
# A chunk module is a Python file. This is the only file extension the framework loads.
PY_MODULE_EXT = ".py"

#: Discovery cache for ChunkRegistry._discover_module: (file, module name) -> (mtime, modules).
#: Process-wide, like registered_modules itself, so a re-initialize() does not re-parse the
#: whole chunk directory to rebuild objects the registry already holds.
_DISCOVERED_MODULES: "Dict[Tuple[str, str], Tuple[int, List[ChunkModule]]]" = {}

USD_CHUNK_DEPENDENCY_DIR = ChunkEnvVar("USD_CHUNK_DEPENDENCY_DIR", "_installed_deps", True)
USD_CHUNK_LOAD_FILTER = ChunkEnvVar[str | None]("USD_CHUNK_LOAD_FILTER", None)

# Installing a chunk's declared pip dependencies is OFF unless this is set. A chunk module
# is ordinary Python that anyone can drop on USD_CHUNK_DIRS, so "load this network" must
# never be able to put packages into the user's environment as a side effect -- not even
# behind a prompt, because the prompt is the wrong question in the places it gets asked.
# `usdchunks run` in CI has no one to answer it: chunk_cli's should_install_package calls
# input(), which reads EOF and blocks or throws with no useful message.
#
# When it is off the missing packages are named in a warning instead, so the user can
# install them deliberately. When it is on, the per-scene should_install_package prompt
# still applies -- this switch decides whether asking is allowed at all, not what the
# answer is.
USD_CHUNK_AUTO_INSTALL = ChunkEnvVar[str | None]("USD_CHUNK_AUTO_INSTALL", None)

# The marketplace install location (where `usdchunks install` deposits chunks).
# It is appended to USD_CHUNK_DIRS during discovery -- last, so proprietary /
# on-prem chunks on USD_CHUNK_DIRS win on a duplicate name. Default lives under
# the user home (independent of the CLI's USDCHUNKS_HOME) so both sides agree.
GLOBAL_USDCHUNKS = ChunkEnvVar[str | None](
    "GLOBAL_USDCHUNKS", str(Path.home() / ".usdchunks" / "global")
)
# When truthy, list every shadowed chunk (a name found in more than one place)
# at the end of discovery. Off by default: only same-tier collisions get a
# one-line summary -- an intentional USD_CHUNK_DIRS-over-GLOBAL override is
# silent (that's the feature, not a warning).
USD_CHUNK_VERBOSE = ChunkEnvVar[str | None]("USD_CHUNK_VERBOSE", None)


def _is_truthy(value) -> bool:
    return bool(value) and str(value).strip().lower() not in ("0", "false", "no", "off")


class ChunkDependencyManager(metaclass=RegistrySingleton):
    initialized = False

    def initialize(self):
        self._cache = defaultdict(set)
        self.package_dir = USD_CHUNK_DEPENDENCY_DIR()

        if self.package_dir:
            if self.package_dir not in sys.path:
                sys.path.append(str(self.package_dir))

                # When a chunk runs a subprocess, the os.environ is passed as the env. Therefore we should
                # also make sure the PYTHONPATH is updated in order for the subprocess to find the packages
                os.environ["PYTHONPATH"] = os.environ.get("PYTHONPATH", "") + os.pathsep + str(self.package_dir)

            already_initialized = self.initialized
            self.initialized = True
            self._load_cache()

            # Once per process, not once per initialize(). This runs again on every stage
            # reload and on every ChunkRegistry().initialize(), so an unguarded register()
            # accumulates a handler per call -- each one rewriting the same cache file at
            # exit, and each one logging after the interpreter has closed its streams, which
            # surfaces as a wall of "ValueError: I/O operation on closed file" once the
            # process ends. Harmless but alarming, and it hides real shutdown errors.
            if not already_initialized:
                atexit.register(self._write_cache)

    def _env_cache_file(self):
        checksum = chunk_cache.compute_checksum(sys.executable)
        return os.path.join(self.package_dir, f"{checksum}_chunk_package_cache.json")

    def _write_cache(self):
        # Writes the cache to disk
        # Silence logging for the duration of the write. This runs from atexit, by which
        # point pytest -- or any host that redirects output -- has already closed the stream
        # our handlers write to. logging then reports its own failure to stderr, printing a
        # full traceback *after* a run that already said "45 passed", which reads like a
        # crash. Nothing logged this late can be read anyway.
        previously_disabled = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            chunk_cache.write_package_cache(self._env_cache_file(), dict(self._cache))
        finally:
            logging.disable(previously_disabled)

    def _load_cache(self):
        # Loads the cache from disk. An unreadable or old-format file reads as empty and
        # update_cache rebuilds from `pip list` -- see chunk_cache.read_package_cache.
        self._cache = defaultdict(set, chunk_cache.read_package_cache(self._env_cache_file()))

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

        missing = []
        recache = True

        for package in requirements:
            if self.package_in_cache(package):
                continue

            if recache:
                self.update_cache()
                recache = False
                if self.package_in_cache(package):
                    continue

            missing.append(package)

        if not missing:
            return

        if not _is_truthy(USD_CHUNK_AUTO_INSTALL()):
            # Off by default -- see USD_CHUNK_AUTO_INSTALL. Say what is missing and how to
            # get it, rather than either installing it or failing silently later when the
            # chunk's import does.
            logger.warning(
                "Chunk dependencies not installed: %s. Install them yourself, or set "
                "USD_CHUNK_AUTO_INSTALL=1 to be asked about installing them into %s.",
                ", ".join(missing), self.package_dir,
            )
            return

        packages = [p for p in missing if self.should_install_package(p)]
        if not packages:
            return

        args = [sys.executable, "-m", "pip", "install", "-t", self.package_dir]
        args.extend(packages)
        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW_FLAGS
        )

        # Only cache what pip actually installed. These packages used to be added to the
        # cache the moment the user approved the dialog, before pip ran and without ever
        # checking its return code -- so a failed install (an embedded interpreter shipping
        # no pip, no network, a bad package name) was remembered as a success. Every later
        # run then short-circuited on package_in_cache: the install was never retried, the
        # user was never prompted again, and the chunk errored on creation forever.
        if proc.returncode != 0:
            output = (proc.stdout or b"").decode(errors="replace").strip()
            logger.error(
                f"Failed to install chunk dependencies {packages} (pip exited {proc.returncode}). "
                f"They will be retried the next time a chunk requires them.\n{output}"
            )
            return

        for package in packages:
            n, v = self.package_info(package)
            self._cache[n].add(v)

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

        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW_FLAGS,
        )
        pip_packages = proc.stdout.decode().split()
        for p in pip_packages:
            pname, pversion = self.package_info(p)
            self._cache[pname].add(pversion)

        # Search the pip_dep_dir
        args = [sys.executable, "-m", "pip", "list", "--path", self.package_dir, "--format", "freeze"]

        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW_FLAGS,
        )
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
        self._deferred_config: Optional[Callable] = None

    def _get_node_source(self, node):
        return ast.get_source_segment(self._code, node)

    def _exec_node(self, node):
        if not (src := self._get_node_source(node)):
            return node
        try:
            dat = compile(src, filename="<ast>", mode="exec")
            exec(dat, self._globs)
        except ModuleNotFoundError:
            pass
        return node

    def resolve_deferred_config(self, tree: ast.Module) -> None:
        """Retry a config() that referenced module-level names, with those names available.

        Discovery deliberately executes only usd_chunks imports and config() itself, so a
        module's top-level code never runs just because the registry looked at it. That
        also means config() cannot normally see module-level constants or helpers -- which
        a config derived from module data (ChunkConfig.variants built from an op table,
        say) needs.

        Reached only after the first call raised NameError, so a self-contained config
        never causes its module's assignments to run at discovery time.
        """
        if not self._deferred_config:
            return

        # Source order, and defs as well as assignments: a config built from module data
        # usually reaches a helper too, and a helper defined after the constant it reads
        # must be executed after it.
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)):
                try:
                    self._exec_node(node)
                except Exception:  # noqa: BLE001 - module-level code we cannot evaluate in isolation
                    continue

        try:
            self._module.config = self._deferred_config()
        except Exception as e:  # noqa: BLE001 - keep a bad config from taking down discovery
            logger.warning(f"Could not evaluate config() for {self._module.module_name}: {e}")

        self._deferred_config = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if node.name == "config":
            self._exec_node(node)
            if func := self._globs.get("config"):
                try:
                    self._module.config = func()
                except NameError:
                    # config() read a module-level name, which the walk has not executed.
                    # resolve_deferred_config retries once the whole tree is available.
                    self._deferred_config = func
        if node.name == "main":
            self._module._has_main = True
            if src := self._get_node_source(node):
                self._module._checksum = chunk_cache.compute_checksum("".join(src.split()))
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


def _pre_load_module(source: str, globs: dict, module: ChunkModule) -> _ConfigTransformer:
    tree = ast.parse(source)
    transformer = _ConfigTransformer(source, globs, module)
    transformer.visit(tree)
    transformer.resolve_deferred_config(tree)
    return transformer


class _ChunkLazyConfigLoader(importlib.abc.SourceLoader):
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

    def load_module(self, fullname: str) -> types.ModuleType:
        return self._loader.load_module(fullname)

    def exec_module(self, module: types.ModuleType) -> None:
        source_bytes = self.get_data(self.get_filename(self.fullname))
        source = source_bytes.decode("utf-8")

        _pre_load_module(source, module.__dict__, self._module)

        self._loader.exec_module(module)


#: Import name -> distribution name, for the handful where they differ. A chunk declaring
#: "opencv-python" imports "cv2", and nothing in the import statement says so.
IMPORT_TO_PACKAGE = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "usd": "usd-core",
    "pxr": "usd-core",
}

#: Import roots every chunk may use without declaring: the host's own USD build, its Qt,
#: the framework, and anything the interpreter ships. PySide6 belongs here for the same
#: reason pxr does -- a chunk drawing a custom widget draws it into the host's toolkit and
#: cannot bring its own.
_ASSUMED_IMPORTS = {"pxr", "usd_chunks", "PySide6"}


def _normalise_package(name: str) -> str:
    """PEP 503 name, with any version specifier or extra dropped."""
    for sep in ("[", "<", ">", "=", "!", "~", ";", " "):
        name = name.split(sep)[0]
    return re.sub(r"[-_.]+", "-", name).strip().lower()


@functools.lru_cache(maxsize=1)
def _packages_distributions() -> dict:
    try:
        from importlib.metadata import packages_distributions
    except ImportError:  # pragma: no cover - 3.10+
        return {}
    return packages_distributions()


def candidate_distributions(imported: str) -> set:
    """Every pip name that could plausibly supply this import.

    Several distributions can provide one module -- opencv-python and
    opencv-python-headless both give you cv2 -- so there is no single right answer to check
    against. The static map covers the well-known renames without needing anything
    installed; the environment is consulted as well, which catches the variants the map
    does not know, but is never the only source or a chunk would pass or fail on which
    variant this particular machine happens to have.
    """
    candidates = {imported, IMPORT_TO_PACKAGE.get(imported, imported)}
    candidates.update(_packages_distributions().get(imported, ()))
    return {_normalise_package(name) for name in candidates}


#: Descending into one of these would leave module scope. Everything else -- try, if, with
#: -- is still module scope and can hold an import.
_NOT_MODULE_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def top_level_imports(module_file) -> set:
    """The root names a chunk module imports at module scope, read without executing it.

    Walks into try/except, if and with rather than only `tree.body`. An earlier version
    read the top level literally and so never saw a guarded import -- and rbd_solver is
    exactly that case:

        try:
            import mujoco as _mj
            import numpy as _np
            _HAS_MUJOCO = True
        except ImportError:
            _HAS_MUJOCO = False

    It reported nothing for that file. Removing its requirements would still have read as
    clean, in the one chunk where a version silently changing the answer matters most --
    try-guarding a heavy optional is the normal pattern for precisely the chunks that carry
    a simulator.

    Function and class bodies are still skipped, which is the actual house rule: imports go
    at module top level, never inside a function. A try-guarded import obeys that rule
    lexically; it just is not an ast.Import node in tree.body, which is a different thing.
    """
    try:
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()

    roots = set()

    def walk(nodes):
        for node in nodes:
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
            elif not isinstance(node, _NOT_MODULE_SCOPE):
                walk(ast.iter_child_nodes(node))

    walk(tree.body)
    return roots


def undeclared_imports(module: "ChunkModule", sibling_modules=()) -> list:
    """Third-party imports a chunk makes without declaring them in config().

    An undeclared dependency is not only an install that breaks on a clean machine -- it is
    a version that never reaches a run's manifest. A chunk importing numpy without saying so
    means a numpy upgrade can change its results while the manifest still reads clean, which
    is worse than having no manifest, because it looks authoritative.

    sibling_modules are the other chunk modules' import names, which resolve locally and are
    not dependencies to declare.
    """
    # split_requirement, not package_info: the latter only understands "==", so a
    # requirement declared as a range would come back with the whole string as its name
    # and every import it covers would read as undeclared.
    declared = {
        _normalise_package(split_requirement(r)[0])
        for r in (module.config.requirements or [])
    }
    declared.update(_normalise_package(m) for m in (module.config.required_modules or []))

    undeclared = []
    for root in sorted(top_level_imports(module.module_file)):
        if root in _ASSUMED_IMPORTS or root in sys.stdlib_module_names:
            continue
        if root in sibling_modules:
            continue
        if not candidate_distributions(root) & declared:
            undeclared.append(root)
    return undeclared


#: PEP 440 comparison operators, longest first so ">=" is not read as ">".
_SPECIFIER_OPS = ("===", "==", "!=", "<=", ">=", "~=", "<", ">")


def split_requirement(requirement: str) -> tuple:
    """A requirement string as (name, specifier), specifier "" when unconstrained.

    Separate from ChunkDependencyManager.package_info, which answers a narrower question --
    the exact version to cache an install under -- and only understands "==".
    """
    text = str(requirement).strip()
    for op in _SPECIFIER_OPS:
        if op in text:
            head, _, _ = text.partition(op)
            return head.strip(), text[len(head):].strip()
    return text, ""


def unconstrained_requirements(module: "ChunkModule") -> list:
    """Requirements declared with no version constraint at all.

    A bare "mujoco" says nothing, so two runs a month apart can use different solvers from
    an identical recipe and nothing recorded explains the difference.

    A *range* is what a chunk should declare -- what it is compatible with -- rather than an
    exact pin. Exact pins do not compose: two chunks pinning numpy==2 and numpy==3 have no
    solution, there is one sys.modules['numpy'], and resolving it would mean editing someone
    else's chunk. Ranges intersect, and the exact version belongs in the recipe that runs
    them, recorded as what was actually imported.
    """
    return [r for r in (module.config.requirements or []) if not split_requirement(r)[1]]


#: Back-compat alias: this used to mean "declared without an exact ==version".
unpinned_requirements = unconstrained_requirements


def conflicting_requirements(modules) -> dict:
    """Packages whose installed version fails some chunk's declared range.

    Reported against the version actually installed rather than by intersecting ranges in
    the abstract, because that is the question that has an answer: two chunks disagreeing
    matters exactly when what is on the machine cannot satisfy both. It also catches the
    single-chunk case, where the one declared range is not what is installed.

    Returns {package: [(module_name, specifier, installed), ...]} for the failures only.
    """
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with pip
        return {}

    wanted: dict = defaultdict(list)
    for module in modules:
        for requirement in module.config.requirements or []:
            name, specifier = split_requirement(requirement)
            if specifier:
                wanted[_normalise_package(name)].append((module.module_name, specifier))

    failures: dict = {}
    for package, claims in wanted.items():
        installed = installed_version(package)
        if installed is None:
            continue
        try:
            version = Version(installed)
        except InvalidVersion:
            continue
        bad = [
            (name, specifier, installed)
            for name, specifier in claims
            if version not in SpecifierSet(specifier)
        ]
        if bad:
            failures[package] = bad
    return failures


def installed_version(package: str):
    """The version actually importable right now, or None.

    The only number that is true. What a chunk declared is a claim; this is what ran, and it
    is what a recipe's manifest has to record -- otherwise a conflict resolved by whichever
    install landed last is invisible after the fact.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover
        return None
    for candidate in (package, package.replace("-", "_"), package.replace("_", "-")):
        try:
            return version(candidate)
        except PackageNotFoundError:
            continue
    return None


class ChunkModule:
    """
    A class used by ChunkRegistry for managing chunk modules. Modules are loaded at runtime using the
    ChunkLazyConfigLoader

    Args:
        module (ModuleType | None): The loaded chunk module
        module_name (str): The name of the module
        module_file (str): The original, absolute, path to the python module
    """

    def __init__(
        self,
        module: Optional[types.ModuleType] = None,
        module_name: str = "",
        module_file: Path | str = Path(),
    ):
        self.module = module
        self.module_name = module_name
        self.module_file = Path(module_file)

        self.loaded = False
        # self.loaded_chunks: weakref.WeakValueDictionary[str, ChunkBase] = weakref.WeakValueDictionary()
        self.func: Optional[Callable[[], core.ChunkBase | List[core.ChunkBase]]] = None

        self._has_main = False

        # This is set by the _ConfigTransformer when parsing the module (before loading) if the module
        # defines a config() function
        self.config: core.ChunkConfig = core.ChunkConfig()
        # This is set by the _ConfigTransformer when parsing the module (before loading). The value is
        # the checksum of the main() function, used for comparing if it has changed when loading a scene
        # file
        self._checksum: int = -1

        # Call _exec_module to lazily load the module. This runs the _ConfigTransformer. If module_file is .py and
        # module is not a RuntimeModule, _exec_module will create a _ChunkConfigLazyLoader
        self._exec_module()

    @staticmethod
    def from_chunk_definition(file: str | Path, name: Optional[str] = None) -> List["ChunkModule"]:
        """
        Create a ChunkModule from a chunk module file (.py).

        Returns:
            List[ChunkModule]: A list holding the created module, or empty for anything
            that is not a .py file.
        """
        p = Path(file)

        if name is None:
            name = p.stem

        if p.suffix != PY_MODULE_EXT:
            logger.warning(f"Cannot import {p.name}: a chunk module is a .py file")
            return []

        # No deserialising to do -- loading the module is ChunkModule's own job.
        return [ChunkModule(module_name=name, module_file=file)]

    def __repr__(self) -> str:
        return f"ChunkModule(module_name='{self.module_name}', module_file='{self.module_file.as_posix()}')"

    def get_menu_path(self) -> List[str]:
        """
        Get the menu namespace this module lives under, outermost element first.

        The module's dotted path is prefixed with its category, dropping a leading path
        element that merely repeats that category.

        Returns:
            List[str]: The menu namespace, outermost first; the last element is the label.
        """

        cat = self.config.category
        if isinstance(cat, Enum):
            cat = cat.value

        import_path = self.module_name.split(".")
        if not cat:
            return import_path
        else:
            namespace = cat.split("/")
            namespace.append(import_path[-1])

        return namespace

    def get_menu_name(self) -> str:
        """
        Get a path-like string refereing to where this module can be found in a menu

        Returns:
            str: A path-like string to where this module can be found in a menu.
        """
        return os.pathsep.join(self.get_menu_path())

    def _exec_module(self):
        if not self.module_file.exists():
            logger.error(f"Could not load module from file {self.module_file}")
            return False

        elif self.module_file.suffix == PY_MODULE_EXT:
            spec = util.spec_from_file_location(self.module_name, self.module_file)
            if not spec or not spec.loader:
                logger.warning(f"Module spec or loader failed for module {self.module_name}. Skipping")
                return False

            loader = _ChunkLazyConfigLoader(self.module_name, str(self.module_file), spec.loader, self)
            spec.loader = loader
            module = util.module_from_spec(spec)

            try:
                spec.loader.exec_module(module)
            except Exception:
                # This module failed to load. Set to file_name so we can try loading it again later§
                logger.exception(f"Failed to load module {self.module_name}")
                return False

            if not self.has_main():
                return False

            if mod := sys.modules.get(self.module_name):
                self.module = mod
                return True

            self.module = module
            sys.modules[self.module_name] = module

        if not self.module:
            return False

        return True

    def _install_deps(self):
        if not self.config.requirements:
            return

        # A requirement with no constraint says nothing, so two runs a month apart can use
        # different libraries from an identical recipe and nothing recorded explains the
        # difference. A range rather than an exact pin: exact pins do not compose, and two
        # chunks pinning different versions of one package would otherwise be resolved by
        # editing somebody else's chunk.
        #
        # Warned rather than refused: a chunk from outside this repo may float a dependency
        # deliberately, and breaking its load over our reproducibility opinion is not ours
        # to make. `usdchunks deps --strict` gates CI for anyone who wants it enforced.
        if unconstrained := unconstrained_requirements(self):
            logger.warning(
                "%s declares %s with no version constraint; a recipe using this chunk "
                "cannot record what it left open. Use a range, e.g. name>=2,<3.",
                self.module_name, ", ".join(unconstrained),
            )

        dep_reg = ChunkDependencyManager()
        if not dep_reg.initialized:
            dep_reg.initialize()

        dep_reg.install_packages(self.config.requirements)

    def _pre_load(self):
        if not self.loaded:
            if not self._exec_module():
                return False

        self._install_deps()

        try:
            self.func = getattr(self.module, "main")
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later
            logger.exception(f"Failed to run module {self.module_name}")
            self.loaded = False
            return False

        if not self.module:
            return False

        self.loaded = True
        return True

    def load(self) -> Sequence[core.ChunkBase]:
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
        self.loaded = False

    def create(self) -> Sequence[core.ChunkBase]:
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

        if self.config:
            for c in loaded:
                self.config.module = self.module_name
                c._config = self.config
                c._module = self.module

        return loaded

    def should_load_for_filter(self) -> bool:
        """
        Check if this module should load. Checks if the module name exists in USD_CHUNK_LOAD_FILTER

        Returns:
            bool: True if this module should load
        """
        if module_filter := USD_CHUNK_LOAD_FILTER():
            if self.module_name not in module_filter:
                return False
        return True

    def has_config(self):
        return bool(self.config)

    def has_main(self):
        return self._has_main

    def should_load_for_plugin(self, plugin_name: str) -> bool:
        """
        Checks if the plugin_name exists in config.load_for_plugin. If the load_for_plugin is empty
        then assume the module is safe to load

        Args:
            plugin_name (str): The name of the plugin

        Returns:
            bool: True if the ChunkModule can be used with the plugin plugin_name
        """
        if self.config.load_for_plugin and plugin_name not in self.config.load_for_plugin:
            return False
        return True

    def export(self, path: str | Path):
        """
        Export this ChunkModule's source as a .py file. Any other extension warns and fails.

        Args:
            path (str | Path): The path to export to. Should be .py
        """
        p = Path(path)

        if not self.module:
            return

        if p.suffix != PY_MODULE_EXT:
            logger.warning(f"wrong file extension {path}")
            return

        try:
            source = inspect.getsource(self.module)
        except (OSError, TypeError):
            logger.exception(f"Could not read the source of {self.module_name}")
            return

        with open(p, "w", encoding="utf-8") as f:
            f.write(source)


# Mirrors the ChunkConfig.description default, so an undocumented chunk shows no tooltip
# rather than showing the placeholder as if it were real documentation.
_PLACEHOLDER_DESC = "Chunk description..."


def clean_description(desc: str) -> str:
    """Dedented/stripped chunk description for a tooltip; '' for the unset placeholder.

    Descriptions are often triple-quoted with leading indentation; dedent + strip so the
    tooltip reads cleanly. Returns '' for the default placeholder so undocumented chunks
    show no tooltip.

    Args:
        desc (str): The raw ChunkConfig.description

    Returns:
        str: Tooltip-ready text, or '' when the chunk is undocumented
    """
    cleaned = textwrap.dedent(desc or "").strip()
    return "" if cleaned == _PLACEHOLDER_DESC else cleaned


@dataclass(frozen=True)
class MenuEntry:
    """One selectable item in a chunk-creation menu, described without reference to any UI.

    A module contributes one base entry, plus one more per ChunkConfig.variants element.
    A variant creates exactly the same chunk as its module and then presets some params,
    so it needs no ChunkModule of its own: the preset lands in an ordinary param value,
    which round-trips through save/load like any other. That is what keeps N menu entries
    from costing N files -- module identity, discovery and shadowing all stay 1:1 with a
    file, and only the menu grows.

    Frontends render this list rather than deriving it, so a module that declares variants
    gains them in every frontend at once.

    Attributes:
        module (ChunkModule): The module this entry creates from
        namespace (Tuple[str, ...]): Menu path, outermost first; the last element is the label
        tooltip (str): Cleaned description, '' when the chunk is undocumented
        presets (Optional[Dict[str, Any]]): Param values to apply on create; None for a base entry
    """

    module: "ChunkModule"
    namespace: Tuple[str, ...]
    tooltip: str = ""
    presets: Optional[Dict[str, Any]] = None

    @property
    def label(self) -> str:
        """The text this entry shows in a menu."""
        return self.namespace[-1] if self.namespace else self.module.module_name

    @property
    def search_terms(self) -> Set[str]:
        """Lowercased namespace elements, for substring matching in a search field."""
        return {x.lower() for x in self.namespace}

    def create(self) -> list:
        """Create this entry's chunk(s), applying any variant presets.

        Presets are applied here rather than by the caller because they must land before
        the chunk is added to a container: PARAM_ADDED fires synchronously and builds
        widgets that read their values immediately, so a preset applied after the add
        would never reach the widgets it was supposed to configure.

        Returns:
            list: The created chunks, ready for the caller to add
        """
        chunks = self.module.create() if self.module.loaded else self.module.load()

        if self.presets:
            for c in chunks or []:
                for param_name, value in self.presets.items():
                    if param := c.get_param(param_name):
                        param.value = value

        return chunks


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
    scene: core.SceneSender
    plugin: str

    # Duplicate module names skipped during registration, as
    # (name, winner_file, loser_file). Reported after discovery. See
    # _report_shadows / _tier_of.
    shadowed_modules: List[Tuple[str, Path, Path]] = []
    _global_dir: Optional[Path] = None

    initialized: bool = False

    def initialize(self, scene_sender: core.SceneSender, plugin: str) -> None:
        """Initialize the FileReader with the working directory.

        Args:
            scene_sender (SceneSender): A SceneSender instance for access to the scene.
                We hold a reference to it here in order to keep it alive
            plugin (str): A string identifying the currently used plugin
        """

        self.scene = scene_sender
        self.plugin = plugin

        ChunkDependencyManager().initialize()

        self.shadowed_modules = []
        ordered_dirs = self.register_chunk_environ_directories()
        # Scan in precedence order (env dirs, then GLOBAL_USDCHUNKS). Include any
        # directories registered out-of-band before initialize at the tail, so
        # they still get scanned but never override the explicit env dirs.
        extras = [p for p in self.registered_directories if p not in ordered_dirs]
        self.register_chunk_modules(ordered_dirs + extras)

        c = len(self.get_chunk_modules(loaded=True))
        logger.info(f"Found {len(self.registered_modules)} chunks. Loaded {c}")
        self._report_shadows()
        self.initialized = True

    def register_chunk_environ_directories(self) -> list[Path]:
        """Register USD_CHUNK_DIRS, then append the GLOBAL_USDCHUNKS directory.

        Returns the registered directories in precedence order. On a duplicate
        module name the first-registered wins (see register_module), so the
        USD_CHUNK_DIRS entries -- your proprietary / on-prem chunks -- override
        marketplace chunks installed into the appended GLOBAL_USDCHUNKS dir.
        """
        dirs = list(USD_CHUNK_DIRS())
        # Append the marketplace install dir last (lowest precedence). Skip it
        # when it doesn't exist yet (nothing installed) to avoid a spurious
        # "directory not found" warning on a clean machine.
        global_dir = GLOBAL_USDCHUNKS()
        self._global_dir = Path(global_dir) if global_dir else None
        if global_dir and global_dir not in dirs and Path(global_dir).exists():
            dirs.append(global_dir)

        ordered: list[Path] = []
        for chunk_dir in dirs:
            self.register_directory(chunk_dir)
            p = Path(chunk_dir)
            if p.exists() and p not in ordered:
                ordered.append(p)
        return ordered

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
        """Recursively register chunk module files (.py) from paths. Paths should be a sequence of
        directory paths that contain ChunkModule files

        Args:
            paths (Iterable[Path]): The directories containing the chunk modules to register.
        """
        for working_dir in paths:
            # Loop over all directories of the working_dirs directory
            for module_path in Path(working_dir).glob("**/*" + PY_MODULE_EXT):
                rel = (module_path.parent / module_path.stem).relative_to(working_dir)
                module_name = str(rel).replace(os.path.sep, ".")

                for mod in self._discover_module(module_path, module_name):
                    self.register_module(mod)

    @staticmethod
    def _discover_module(module_path: Path, module_name: str) -> "List[ChunkModule]":
        """ChunkModule.from_chunk_definition, memoised on (file, mtime).

        Constructing a ChunkModule is not bookkeeping: it reads the file, AST-parses it and
        executes its config() plus its usd_chunks imports (see _ChunkLazyConfigLoader), which
        is essentially all of what a scan costs. A second initialize() -- a re-init in the
        host app, and every test that stands a scene up -- paid that for the whole chunk
        directory again and then threw the result away, because registered_modules is a class
        attribute that outlives it and the already-registered module keeps the name.

        Keyed on modification time, so a chunk edited on disk is still re-read: the point is
        to stop re-parsing files that have not changed, not to freeze discovery.
        """
        try:
            stamp = module_path.stat().st_mtime_ns
        except OSError:
            # Unreadable/vanished between the glob and here -- let the real constructor
            # produce its usual diagnostics rather than caching a guess.
            return ChunkModule.from_chunk_definition(module_path, module_name)
        key = (str(module_path), module_name)
        cached = _DISCOVERED_MODULES.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        mods = ChunkModule.from_chunk_definition(module_path, module_name)
        _DISCOVERED_MODULES[key] = (stamp, mods)
        return mods

    def register_module(self, module: ChunkModule) -> ChunkModule | None:
        """
        Register a ChunkModule. This will wnsure that the module is kept alive and accessible

        Args:
            module (ChunkModule): The module to add to the registry

        Returns:
            ChunkModule | None: The registered module, or None if failed
        """
        if not module.has_main():
            logger.info(f"Skipping {module.module_name}. Module is has no main function defined")
            return
        if not module.has_config():
            logger.info(f"Skipping {module.module_name}. Module is has no config function defined")
            return
        if not module.module:
            logger.info(f"Skipping {module.module_name}. Module has no valid python source module")
            return
        if not module.should_load_for_plugin(self.plugin):
            logger.info(f"Skipping {module.module_name}. Module should not be registered in this context")
            return
        if not module.should_load_for_filter():
            logger.info(f"Skipping {module.module_name}. Module is excluded in CHUNK_MODULE_FILTER")
            return

        name = module.module_name

        if name in self.registered_modules:
            winner = self.registered_modules[name]
            if Path(winner.module_file) == Path(module.module_file):
                # The same file rediscovered, not two files claiming one name -- what a
                # re-initialize() does to every chunk it already holds. Reporting those as
                # shadows drowned the genuinely ambiguous ones (a second initialize logged
                # "69 shadowed chunk name(s)", i.e. all of them).
                return winner
            self.shadowed_modules.append(
                (name, Path(winner.module_file), Path(module.module_file))
            )
            logger.debug(f"Skipping {module.module_file}. Duplicate module: {name}")
            return

        self.registered_modules[name] = module
        return self.registered_modules[name]

    def _tier_of(self, file: Path) -> str:
        """Which precedence tier a module file belongs to: the appended
        GLOBAL_USDCHUNKS install dir, or an explicit USD_CHUNK_DIRS entry."""
        if self._global_dir is not None:
            try:
                if file.resolve().is_relative_to(self._global_dir.resolve()):
                    return "GLOBAL_USDCHUNKS"
            except (OSError, ValueError):
                pass
        return "USD_CHUNK_DIRS"

    def _report_shadows(self) -> None:
        """Summarize duplicate chunk names found during discovery.

        A USD_CHUNK_DIRS chunk shadowing one in GLOBAL_USDCHUNKS is the intended
        override -- silent. Only same-tier collisions (two files at the same
        precedence level claiming one name -- the genuinely ambiguous case) get
        a one-line nudge. USD_CHUNK_VERBOSE lists every shadow, cross-tier ones
        included.
        """
        if not self.shadowed_modules:
            return

        if _is_truthy(USD_CHUNK_VERBOSE()):
            for name, winner, loser in self.shadowed_modules:
                logger.info(
                    f"shadowed chunk '{name}': using {winner} [{self._tier_of(winner)}], "
                    f"ignoring {loser} [{self._tier_of(loser)}]"
                )
            return

        same_tier = [
            (name, winner, loser)
            for name, winner, loser in self.shadowed_modules
            if self._tier_of(winner) == self._tier_of(loser)
        ]
        if same_tier:
            names = ", ".join(sorted({s[0] for s in same_tier}))
            logger.info(
                f"usd_chunks: {len(same_tier)} shadowed chunk name(s) at the same "
                f"precedence tier ({names}) -- set USD_CHUNK_VERBOSE=1 to list"
            )

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

    def get_menu_entries(self, sort_result: bool = True) -> List[MenuEntry]:
        """
        Get every entry a chunk-creation menu should offer, described without reference to any UI.

        One entry per registered module, plus one per element of that module's
        ChunkConfig.variants. Variant entries sit alongside their module rather than
        beneath it, so a declared variant reads as a chunk in its own right in both the
        menu tree and the search field -- which is the whole point of declaring one.

        A variant label may contain "/" to nest further, e.g. "standard_surface/Standard
        Surface". A catalog-driven module can declare hundreds of variants, and those need
        grouping or the category becomes one unnavigable list; search stays flat either way.

        Args:
            sort_result (bool): Sort modules by trailing name, as get_chunk_modules does

        Returns:
            List[MenuEntry]: Base and variant entries, in module order
        """
        entries: List[MenuEntry] = []

        for mod in self.get_chunk_modules(sort_result=sort_result):
            if mod.config.hidden:
                continue  # a type that exists to be loaded, not to be picked
            namespace = mod.get_menu_path()
            tooltip = clean_description(mod.config.description)

            entries.append(MenuEntry(module=mod, namespace=tuple(namespace), tooltip=tooltip))

            parent = tuple(namespace[:-1])
            # get_variants, not .variants: a module may declare them as a callable so the
            # cost lands here, when a menu is actually being drawn, rather than at discovery
            # in every headless process.
            for label, presets in mod.config.get_variants():
                sub = tuple(p for p in str(label).split("/") if p)
                if not sub:
                    continue
                entries.append(
                    MenuEntry(
                        module=mod,
                        namespace=parent + sub,
                        tooltip=tooltip,
                        presets=dict(presets),
                    )
                )

        return entries

    def import_chunk(self, path: str | Path):
        """
        Import a chunk module from path. The file extension of path should be .py.

        Args:
            path (str | Path): The path to the chunk module
        """
        p = Path(path)
        if p.suffix == PY_MODULE_EXT:
            for mod in ChunkModule.from_chunk_definition(p):
                if mod.module_name not in self.registered_modules:
                    self.register_module(mod)
                mod.load()

    def reload_modules(self):
        """
        Reload all loaded modules
        """
        loaded_config_names = defaultdict(int)
        for c in self.scene.get_chunks():
            if conf := c.get_config():
                loaded_config_names[conf.module] += 1

        self.scene.clear()

        for mod in self.get_chunk_modules():
            if mod.loaded:
                mod.unload()
            for i in range(loaded_config_names.get(mod.module_name, 0)):
                if i == 0:
                    chunks = mod.load()
                else:
                    chunks = mod.create()
                self.scene.add_chunks(chunks)


def is_chunk_internal_layer(layer: Sdf.Layer) -> bool:
    """Whether the chunk graph owns this layer.

    "Internal" means a per-chunk procedural layer or a graph layer grouping them: content
    the chunk system Clear()s and regenerates, never something a user picks as an Edit
    Target or exports, and never somewhere a chunk's bookkeeping may be hand-moved.

    Answered from the identifiers ChunkLayerManager records as it creates them, so it costs
    a set lookup. The alternative — deriving it by walking every tracked layer's sublayers —
    has to be redone against live subLayerPaths every time it is asked, which is once per
    reconcile pass and one pass per wire, and the layer stack grows with the graph.

    Deliberately NOT stored on the layer itself, which is the obvious place and is wrong:
    Sdf.Layer.Clear() drops customLayerData along with everything else, and Clear() on its
    own layer is exactly what a procedural chunk does every time it cooks. A mark there
    survives until the chunk first runs and then silently disappears.
    """
    if not layer:
        return False
    return layer.identifier in ChunkLayerManager().internal_layer_ids


def _without_sublayer(parent: Sdf.Layer, identifier: str) -> list:
    """parent's subLayerPaths with identifier dropped, order preserved, duplicates collapsed.

    Both the "drop every occurrence" and the de-dup matter, because an anonymous layer's
    identifier embeds its address (anon:<address>:<tag>) and addresses get recycled: once a
    tracked layer is freed while its identifier is still listed here, a later anon layer can
    be handed the same address and produce a byte-identical identifier. subLayerPaths is a
    list editor that rejects duplicates outright, so assigning such a list raises rather than
    edits, and the delete never completes. Collapsing them is the only legal state anyway —
    two entries could never have named two different live layers at once.
    """
    seen: set = set()
    return [
        p for p in parent.subLayerPaths
        if p != identifier and not (p in seen or seen.add(p))
    ]


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
    #: Identifiers of every layer this manager created. Declared here, not only in
    #: initialize(), because is_chunk_internal_layer reads it and is reachable before this
    #: singleton has been initialized at all: ChunkStageSync.initialize ->
    #: reconcile_edit_targets -> _migratable_layers asks the question while setting a stage
    #: up, and a host or a test that brings the sync up first crashed with AttributeError
    #: instead of getting the correct answer, which is that nothing is internal yet.
    #: initialize() and reset() both rebind it to a fresh set, so the class-level one is
    #: only ever the empty pre-initialize answer.
    internal_layer_ids: set = set()

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

        self._deleted_listener = NoticeConnection(
            self._on_chunk_deleted_notice, ChunkNoticeAlias.CHUNK_DELETED, globally=True
        )

        self.chunk_layers: Dict[core.ChunkHash, Sdf.Layer] = {}
        # Keyed by (graph_id, target_layer_identifier) — a graph's chunks get split into a
        # separate sibling group per Edit Target they're actually authored against, since a
        # single graph_layer can only ever be a sublayer of one target at a time.
        self.graph_layers: Dict[tuple, Sdf.Layer] = {}
        # Which real layer each graph_layer entry is currently sublayered under — needed to
        # detect and reparent when the resolved Edit Target changes between calls.
        self._graph_layer_target: Dict[tuple, Sdf.Layer] = {}
        # Which real layer each individual chunk's content currently resolves against (same
        # value as its graph_layer's entry above, but keyed per-chunk for easy lookup) — lets
        # export tooling group a chunk's own bookkeeping prim with whichever layer its driven
        # content nests in (see scripts/export_usd.py's per-layer export mode).
        self._chunk_target: Dict[core.ChunkHash, Sdf.Layer] = {}
        # Hashes of chunks whose layer has been torn down. Never served a layer again, so a
        # notice fired by the teardown itself can't resurrect one — see _chunk_deleted.
        self._retired_chunks: set = set()
        self._chunk_refs = weakref.WeakSet()
        # Identifiers of every layer above that this manager created, so is_chunk_internal_layer
        # can answer in one lookup. Maintained beside the two dicts rather than derived from
        # them on demand, and dropped with them, because an anonymous identifier embeds an
        # address and addresses get recycled: a leftover entry would eventually claim an
        # unrelated layer.
        self.internal_layer_ids: set = set()

        # When initialized, the stage may be None as the chunks are loaded prior to loading the stage
        self.stage: Usd.Stage = stage

        self.initialized = True

    def reset(self):
        """Return the singleton to its pre-initialize state, dropping every layer
        mapping. Used when switching stages (reload, tests) so state from the old
        stage's stack doesn't leak into the new one. Keeps this class the single
        owner of its private field list — callers must not clear fields directly.
        """
        self.initialized = False
        self.stage = None
        self.chunk_layers = {}
        self.graph_layers = {}
        self._graph_layer_target = {}
        self._chunk_target = {}
        self._retired_chunks = set()
        self.internal_layer_ids = set()

    def get_layer_file_name(self, layer: Sdf.Layer):
        return "_".join(layer.GetDisplayName().split("_")[:-1]) + ".usda"

    def save_layer(self, c: core.ChunkHash, dirname: str | Path):
        if not (layer := self.get_layer(c)):
            return None

        name = self.get_layer_file_name(layer)
        file = f"{dirname}/{name}"
        layer.Export(file)
        return Path(file)

    def save_layers(self, path: str | Path) -> Dict[str, str]:
        p = Path(path)
        if not p.is_dir():
            logger.warning(f"Can't save layers. Path is not a directory {path}")
            return {}

        layers = {}
        for key, layer in self.chunk_layers.items():
            if not layer:
                continue
            file = self.save_layer(key, path)
            if not file:
                continue
            layers[file.as_posix()] = key
        return layers

    def open_layer(self, path: str | Path):
        p = Path(path)
        if not p.exists():
            return
        return Sdf.Layer.FindOrOpen(str(path))

    def stitch_layers(self, layer_a, layer_b):
        UsdUtils.StitchLayers(layer_a, layer_b)

    def get_layer_usda(self, layer: Sdf.Layer):
        return layer.ExportToString()

    def get_stage_root(self) -> Sdf.Layer:
        return self.stage.GetRootLayer()

    def get_layer(self, c: "core.ChunkHash") -> Sdf.Layer | None:
        """
        Get a layer associated with the a chunk

        Args:
            c (ChunkUsdLayerSupport): A chunk object implementing the ChunkUsdLayerSupport protocol

        Returns:
            Sdf.Layer: The layer associated with the ChunkCommon object
            None: If the layer does not exist
        """
        return self.chunk_layers.get(c)

    def get_chunk_target(self, c: "core.ChunkHash") -> Sdf.Layer | None:
        """Return the real layer this chunk's content last resolved against — the session
        layer by default, or an Edit-Target-chosen layer — as tracked by get_or_create_layer.

        Used by export tooling to group a chunk's own bookkeeping prim (always in the
        session layer) with whichever real layer its driven content actually nests in.
        """
        return self._chunk_target.get(c)

    def register_chunk_layer(self, c: "core.ChunkCommon", layer: Sdf.Layer) -> None:
        """Associate an existing layer as this chunk's edit layer."""
        h = c.get_hash()
        self.chunk_layers[h] = layer
        weakref.finalize(c, self._chunk_deleted, h)

    def get_or_create_layer(self, c: "core.ChunkCommon") -> Sdf.Layer | None:
        """
        Get a layer similarly to get_layer(). Will create a new one if the layer does
        not already exists

        Args:
            c (ChunkUsdLayerSupport): The chunk object which implements the ChunkUsdLayerSupport protocol

        Returns:
            Sdf.Layer: The layer, either found or created

        Raises:
            AttributeError: If the stage has not been initialized
        """
        if not self.stage:
            return

        # A deleted chunk never gets a layer back — see _chunk_deleted for why re-creating
        # one mid-teardown is what corrupts the graph layer's sublayer list.
        if c.get_hash() in self._retired_chunks:
            return

        existing = self.chunk_layers.get(c.get_hash())
        if existing and not existing.anonymous:
            return existing

        owner = c.get_owner()
        while owner and not isinstance(owner, core.ChunkBase):
            owner = owner.get_owner()
        if isinstance(owner, core.ChunkBase) and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            ctx_layer = self.chunk_layers.get(owner.get_hash())
            if ctx_layer and not ctx_layer.anonymous:
                return ctx_layer

        # Resolve where this chunk's content should nest: the user's chosen Edit Target,
        # or the session layer by default — the same resolution chunk-prim redirect
        # opinions already follow (chunk_utils.get_edit_layer). CONTEXT chunks (Tab-like
        # containers) are structural scaffolding, not content — they always stay pinned
        # to the session layer regardless of Edit Target, same as chunk-prim bookkeeping;
        # only a regular content-producing chunk's own driven data should move.
        # Local import: chunk_utils imports this module at module scope, so importing it
        # back at module scope here would fail depending on which of the two loads first.
        if isinstance(c, core.ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            target = self.stage.GetSessionLayer()
        else:
            from usd_chunks import chunk_utils
            # Pass c as the owner so the target is resolved from c's own exec-chain position
            # (nearest upstream Edit Target), not from whichever chunk executed last.
            owner_chunk = c if isinstance(c, core.ChunkBase) else None
            target = chunk_utils.get_edit_layer(self.stage, owner_chunk)
        self._chunk_target[c.get_hash()] = target

        # Create a graph layer - these are used for organising the layers. When chunks are part of
        # the same graph AND currently resolve to the same target, they become siblings in the
        # same graph layer (a graph_layer can only ever be a sublayer of one real layer at a time,
        # so chunks split into separate groups per target they're actually authored against).
        graph_id = c.get_dag_node().get_graph_id()
        graph_key = (graph_id, target.identifier)

        # Every subLayerPaths edit below is a plain Sdf write that fires StageContentsChanged
        # like any other stage edit. Without this guard, one can synchronously re-enter this
        # same method (via ChunkStageSync._on_contents_changed -> load_chunks, the same
        # reentrancy class documented on _chunk_deleted's own guard below) for a sibling chunk
        # targeting the same graph_key, before this call's own bookkeeping/insert has settled
        # — seen live in CI as a genuine, if intermittent, "Duplicate item ... not allowed for
        # field 'subLayers'" Sdf error on the exact same graph layer being inserted twice.
        prev_w = chunk_notice._usd_write_active
        prev_internal = chunk_notice._internal_layer_write_active
        chunk_notice._usd_write_active = True
        # Everything sublayered below is a graph layer or a chunk's own procedural layer, so
        # none of it can change which Edit Target a chunk resolves to — see the flag's own
        # comment. Set alongside _usd_write_active because the Edit Target memo is dropped
        # ahead of that flag's bail-out, deliberately: an ordinary suppressed sublayer edit
        # still moves a real layer in or out of the stack, and this one does not.
        chunk_notice._internal_layer_write_active = True
        try:
            if not (parent := self.graph_layers.get(graph_key)):
                parent = Sdf.Layer.CreateAnonymous(f"graph_{graph_id}")
                self.internal_layer_ids.add(parent.identifier)
                self.graph_layers[graph_key] = parent
                self._graph_layer_target[graph_key] = target
                # Insert at the top so this graph's layers win over other sublayers already on
                # the target — e.g. an RBD bake must beat the prim's existing xformOp:transform.
                # Checked rather than unconditional: a freshly minted anon layer's identifier
                # can still collide with one already sitting in target.subLayerPaths — e.g. a
                # stale reference left over from before a reload (anon identifiers are reused
                # once the process's underlying slot is freed), which USD's Sdf list editor
                # rejects outright as a duplicate ("Duplicate item ... not allowed for field
                # 'subLayers'") rather than silently no-opping.
                if parent.identifier not in target.subLayerPaths:
                    target.subLayerPaths.insert(0, parent.identifier)
            else:
                prev_target = self._graph_layer_target.get(graph_key)
                if prev_target and prev_target.identifier != target.identifier:
                    # The resolved Edit Target changed since this graph layer was created (e.g.
                    # an upstream Edit Target chunk's selection changed) — reparent rather than
                    # leave it sublayered under a now-stale target.
                    if parent.identifier in prev_target.subLayerPaths:
                        prev_target.subLayerPaths.remove(parent.identifier)
                    self._graph_layer_target[graph_key] = target
                if parent.identifier not in target.subLayerPaths:
                    target.subLayerPaths.insert(0, parent.identifier)

            # The sort_key is used to determine the order of the chunk sublayers
            index = c.get_dag_node().get_sort_key()
            if index > len(parent.subLayerPaths) - 1:
                index = len(parent.subLayerPaths)

            # Reverse the order
            index = len(parent.subLayerPaths) - index

            layer = self.get_layer(c)

            if not layer:
                if self.is_muted(c):
                    return
                layer = self.add_layer(parent, c, index)

            if layer.identifier not in parent.subLayerPaths:
                parent.subLayerPaths.insert(index, layer.identifier)

            # If a chunk connection changed after the chunk layer was created, the layer may have been
            # added to a new graph. This can be cleaned up as to not sublayer the chunk layer twice.
            # Only inspect pl for emptiness right after we've just removed its child above — never as
            # a blanket sweep over every tracked graph layer, since a graph layer can legitimately be
            # momentarily empty while it's still being constructed (its own first leaf not yet added).
            #
            # Looks only at the graph layers' own direct sublayers, not the whole composed
            # hierarchy iter_chunk_layers() walks. A chunk leaf layer is only ever inserted as a
            # direct sublayer of a graph layer (add_layer), so those are the only pairs that could
            # ever match here — the deeper levels of that walk are a chunk layer's own imports
            # (a Sublayer chunk's files), which are never this layer. The full walk resolves every
            # sublayer path through Sdf.Find on every call, and this runs on every single
            # get_edit_context: it was ~30% of the cost of opening a saved graph.
            for pl in list(self.graph_layers.values()):
                if pl.identifier != parent.identifier and layer.identifier in pl.subLayerPaths:
                    pl.subLayerPaths.remove(layer.identifier)
                    if len(pl.subLayerPaths) == 0:
                        for gkey, g_layer in list(self.graph_layers.items()):
                            if g_layer.identifier != pl.identifier:
                                continue
                            gtarget = self._graph_layer_target.pop(gkey, None)
                            if gtarget and pl.identifier in gtarget.subLayerPaths:
                                gtarget.subLayerPaths.remove(pl.identifier)
                            self.graph_layers.pop(gkey, None)
                            self.internal_layer_ids.discard(pl.identifier)
                            break
        finally:
            chunk_notice._usd_write_active = prev_w
            chunk_notice._internal_layer_write_active = prev_internal

        return layer

    def is_muted(self, c: "core.ChunkHash") -> bool:
        """
        Check if a chunk layer is muted

        Args:
            c (ChunkHash): The chunk which whose layer to check

        Returns:
            bool: True when the layer is muted, False otherwise
        """
        layer = self.chunk_layers.get(c)
        if not (self.stage and layer):
            return False
        # A chunk layer can be muted stage-scoped (disable path) or Sdf-level (node graph
        # view slots); treat either as muted.
        return self.stage.IsLayerMuted(layer.identifier) or layer.IsMuted()

    def add_layer(self, target_layer: Sdf.Layer, c: "core.ChunkCommon", index: int = 0) -> Sdf.Layer:
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
        self.internal_layer_ids.add(layer.identifier)

        self.chunk_layers[h] = layer
        target_layer.subLayerPaths.insert(index, layer.identifier)

        # We must make sure to delete the layer of the chunk AFTER all other notices are sent.
        # For this reason, we can't rely on callbacks. Instead we can register a finalizer, so we delete
        # the layer once the chunk is actually deleted
        weakref.finalize(c, self._chunk_deleted, h)

        return layer

    def clear_all_layers(self):
        for c, lay in self.chunk_layers.items():
            self.clear_layer(lay.identifier)

        for gkey, g_layer in list(self.graph_layers.items()):
            target = self._graph_layer_target.pop(gkey, None)
            if target and g_layer.identifier in target.subLayerPaths:
                target.subLayerPaths.remove(g_layer.identifier)

        self.graph_layers.clear()
        self.chunk_layers.clear()
        self._chunk_target.clear()
        self.internal_layer_ids.clear()

    def clear_layer(self, layer_id: str | core.ChunkBase):
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
        """
        for gkey, g_layer in list(self.graph_layers.items()):
            target = self._graph_layer_target.pop(gkey, None)
            if target and g_layer.identifier in target.subLayerPaths:
                target.subLayerPaths.remove(g_layer.identifier)

        self.graph_layers.clear()
        self.chunk_layers.clear()
        self._chunk_target.clear()
        self.internal_layer_ids.clear()

        for c in ChunkRegistry().scene.get_chunks():
            self.get_or_create_layer(c)

    def _edit_target_changed(self, notice: Usd.Notice.StageEditTargetChanged, sender):
        stage = notice.GetStage()
        if stage:
            self.stage = stage

    def _iter_sublayers(self, layer: Sdf.Layer, visited: set | None = None):
        if visited is None:
            visited = set()
        if layer.identifier in visited:
            return
        visited.add(layer.identifier)
        for lname in layer.subLayerPaths:
            slayer = Sdf.Find(lname)
            if slayer:
                yield (layer, slayer)
                yield from self._iter_sublayers(slayer, visited)

    def iter_chunk_layers(self) -> Generator[Tuple[Sdf.Layer, Sdf.Layer], None, None]:
        """Yield (parent, sublayer) for every graph layer and chunk leaf layer currently
        tracked, regardless of which real layer (session, or an Edit-Target-chosen
        sublayer) each graph layer is actually attached under.
        """
        # Don't pre-mark g_layer as visited here — _iter_sublayers does that itself as
        # its first action, and would otherwise immediately bail (thinking it's already
        # walked) before ever descending into g_layer's own chunk leaf layers.
        visited: set = set()
        for gkey, g_layer in list(self.graph_layers.items()):
            target = self._graph_layer_target.get(gkey)
            if target:
                yield (target, g_layer)
            yield from self._iter_sublayers(g_layer, visited)

    def _on_chunk_deleted_notice(self, notice, sender):
        # Remove the chunk's edit layer immediately on delete. The layer is normally freed by
        # a GC finalizer (add_layer), but a lingering reference — a node/widget or a reference
        # cycle — can defer collection, leaving the layer and any baked edits (e.g. an RBD
        # sim's time samples) composed into the scene. Idempotent with the finalizer.
        get_hash = getattr(sender, "get_hash", None)
        if callable(get_hash):
            self._chunk_deleted(get_hash())

    def _chunk_deleted(self, chunk_hash):
        # Retire the chunk before touching any layer. The subLayerPaths edits below fire
        # StageContentsChanged, and a chunk script listening for it (create_layer's and
        # edit_target's menu-refresh callbacks) re-executes off that notice — landing back in
        # get_or_create_layer, which would hand this same chunk a BRAND NEW anon layer to
        # replace the one being torn down and sublayer it right back in. Every delete then
        # feeds the next: the leftovers pile up under the graph layer until two of them are
        # handed recycled addresses, at which point their anon:<address>:<tag> identifiers
        # collide and subLayerPaths refuses the edit outright. Retiring first breaks the loop
        # at the source, and doubles as the idempotence the finalizer/notice pair needs.
        if chunk_hash in self._retired_chunks:
            return
        self._retired_chunks.add(chunk_hash)
        self._chunk_target.pop(chunk_hash, None)
        if not (layer := self.get_layer(chunk_hash)):
            return

        layer_hierarchy: Dict[Sdf.Layer, Sdf.Layer] = {}

        # subLayerPaths edits below are plain Sdf writes that fire StageContentsChanged
        # like any other stage edit. Without this guard, each one re-enters
        # ChunkStageSync._on_contents_changed -> load_chunks mid-cascade (e.g. while
        # request_delete() is still working through a context's other children),
        # causing a full stage rescan per chunk deleted instead of once.
        prev_w = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            for parent, sublayer in self.iter_chunk_layers():
                layer_hierarchy[sublayer] = parent

                if sublayer == layer:
                    sublayer_paths = _without_sublayer(parent, layer.identifier)

                    if len(sublayer_paths) == 0 and (p := layer_hierarchy.get(parent)):
                        if parent.identifier in p.subLayerPaths:
                            p.subLayerPaths.remove(parent.identifier)

                    parent.subLayerPaths = sublayer_paths  # type: ignore
        finally:
            chunk_notice._usd_write_active = prev_w
        self.chunk_layers.pop(chunk_hash, None)
        # Dropped with the mapping: the identifier embeds an address, and addresses are
        # recycled, so a leftover entry would eventually claim an unrelated layer.
        self.internal_layer_ids.discard(layer.identifier)

    def _flags_changed(self, notice, sender):
        c = sender
        if not isinstance(c, core.ChunkBase):
            return

        if c not in self.chunk_layers:
            return

        if not (layer := self.get_layer(c)):
            return

        if not self.stage:
            return

        if c.has_state(ChunkFlags.State.DISABLED):
            # Mute through the stage (not Sdf.Layer.SetMuted) so usdview receives the
            # recomposition notice and repaints. A plain layer-level mute recomposes lazily,
            # leaving usdview showing sibling prims (e.g. a ScatterInstancer whose def lives
            # in another chunk's layer) as if removed until the next edit forces a refresh.
            self.stage.MuteLayer(layer.identifier)
        elif self.is_muted(c) and not c.is_blocked():
            # Only un-mute a disable-muted layer. A view-driven block also mutes the layer, and
            # this handler runs on EVERY state change (incl. selection) — without the is_blocked
            # guard, selecting an out-of-view node would un-mute it and show its output.
            self.stage.UnmuteLayer(layer.identifier)
