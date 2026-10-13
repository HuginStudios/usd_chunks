"""The ChunkRegistry singleton, which finds, loads and unloads chunk modules.

The USD_CHUNK_DIRS environment variable tells the registry where to scan for chunk
modules (.py files), nested folders included. The registry also holds the scene, which
is how running code finds a chunk by name:

``` py
reg = ChunkRegistry()
my_chunk = reg.scene.get_chunk("my_chunk")  # or chunk.get_chunk("my_chunk")
```
"""

from __future__ import annotations

import contextlib
import weakref
import os
import functools
import re
import sys
from pathlib import Path
from importlib import util
from importlib.metadata import PackageNotFoundError, distributions, packages_distributions
from importlib.metadata import version as distribution_version
from collections import defaultdict
from collections.abc import Callable, Generator, Iterable, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
    overload,
)
from typing_extensions import override
from enum import Enum

from pxr import Sdf, Usd, UsdUtils

try:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.tags import sys_tags
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - packaging ships with pip
    SpecifierSet = Version = InvalidSpecifier = InvalidVersion = sys_tags = None

from usd_chunks import chunk_cache, chunk_cook, chunk_layers, chunk_scheduler, cook_cache, skip_cook
from usd_chunks.chunk_process import python_executable
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_notice import ChunkNoticeAlias, NoticeConnection, RegistrySingleton

import usd_chunks.chunk_core as core
import usd_chunks.chunk_notice as chunk_notice
from usd_chunks import chunk_utils
from usd_chunks.store import pin
# from usd_chunks.chunk_core import ChunkBase, ChunkCommon, ChunkConfig, ChunkHash

if TYPE_CHECKING:
    import types

    # Annotations only: chunk_scene imports chunk_core, which imports this module.
    from usd_chunks.chunk_scene import SceneSender

import atexit
import ast
import base64
import hashlib
import inspect
import importlib.abc
import logging
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from timeit import default_timer as timer

# Suppresses the console window Windows otherwise flashes for every pip subprocess
# spawned from a GUI host (usdview, Maya, etc.) that has no console of its own.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

logger = logging.getLogger(__name__)

#: A `hosts` config entry: a host name, then an optional version specifier ("blender>=5.1").
_HOST_ENTRY = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*(.*)$")

_T = TypeVar("_T")


class ChunkEnvVar(Generic[_T]):
    """An environment variable the chunk system reads, with a typed default.

    A list-typed variable is split on os.pathsep, and its entries come before the defaults.

    Args:
        key: The environment variable's name.
        default: The value when the variable is unset or empty.
        relpath: Resolve a string or list default relative to the repository root.
    """
    def __init__(self, key: str, default: _T, relpath=False):
        self.key = key
        self._default: _T = default
        self._relpath = relpath

    @property
    def default(self) -> _T:
        """The default, resolved against the repository root when `relpath` is set."""
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
            # first occurrence rather than collapsing to a set, which would drop
            # ordering and make precedence non-deterministic.
            ordered: list = []
            for x in res + default:
                if x and x not in ordered:
                    ordered.append(x)
            return ordered
        return envvar

    def __call__(self, fallback: _T | None = None, /, *args: Any, **kwds: Any):
        """The variable's current value.

        A list variable returns the environment's entries first, then the defaults, keeping the
        first of any duplicate: env-provided dirs take precedence on duplicate module names,
        since the first one registered wins.

        Args:
            fallback: Used in place of the environment value when the variable is unset.
            *args: Ignored.
            **kwds: Ignored.

        Returns:
            The value, or the default when the variable is unset or empty.
        """
        return self._get(self.default, fallback)


# The bundled chunks are always searched, after the folders the variable names (so yours
# win on a duplicate name). Resolved against the folder holding the package: chunks/ there
# is a checkout's copy, usd_chunks/chunks the one setup.py copies into an installed package.
USD_CHUNK_DIRS = ChunkEnvVar("USD_CHUNK_DIRS", ["chunks", os.path.join("usd_chunks", "chunks")], True)

# Said once per process: each new Scene registers the directories again.
_warned_no_chunk_dirs = False
# A chunk module is a Python file. This is the only file extension the framework loads.
PY_MODULE_EXT = ".py"

#: Discovery cache for ChunkRegistry._discover_module: (file, module name) -> (mtime, modules).
#: Process-wide, like registered_modules itself, so a re-initialize() does not re-parse the
#: whole chunk directory to rebuild objects the registry already holds.
_DISCOVERED_MODULES: dict[tuple[str, str], tuple[int, list[ChunkModule]]] = {}

# A per-user folder: beside the package is site-packages once installed, which is often
# read-only and no place for `pip install -t` to write.
USD_CHUNK_DEPENDENCY_DIR = ChunkEnvVar("USD_CHUNK_DEPENDENCY_DIR", chunk_cache.user_cache_dir("installed_deps"))
USD_CHUNK_LOAD_FILTER = ChunkEnvVar[str | None]("USD_CHUNK_LOAD_FILTER", None)

# What happens when a chunk declares pip dependencies that are not installed. A chunk module
# is ordinary Python that anyone can drop on USD_CHUNK_DIRS, so "load this network" must
# never put packages into the user's environment as a side effect: by default nothing is
# installed without a yes from the person at the screen.
#
# - Unset: the host asks, once per chunk, naming the chunk and its packages (usdview's
#   dialog, the CLI's terminal prompt, Blender's popup). A host with nobody to ask -- CI, a
#   piped run -- answers no through ChunkScene.should_install_packages, and only warns.
# - Truthy ("1", "on", ...): install without asking. The user said yes ahead of time.
# - Falsey ("0", "off", ...): never install and never ask; the missing packages are named
#   in a warning, so they can be installed deliberately.
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


def _install_mode() -> str:
    """"ask", "always" or "never" -- see USD_CHUNK_AUTO_INSTALL."""
    value = USD_CHUNK_AUTO_INSTALL()
    if value is None:
        return "ask"
    return "always" if _is_truthy(value) else "never"


def _run_pip(args, on_line=None) -> tuple[int, str]:
    """Run pip, handing each line of its output to on_line as it arrives.

    Streamed rather than collected at the end so a host can show what pip is doing while
    it downloads -- a progress dialog that sits on "Installing..." for a minute reads as a
    hang. Safe off the main thread: it touches nothing but the subprocess.

    Args:
        args: The full command line.
        on_line: Called with each output line, stripped; None to just collect.

    Returns:
        (return code, the whole output).
    """
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, errors="replace", creationflags=_NO_WINDOW_FLAGS,
    )
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if on_line is not None and line.strip():
            on_line(line.strip())
    return proc.wait(), "".join(lines)


def _legacy_dependency_dirs(base: str) -> list[str]:
    """Where packages were installed before the per-Python-version split.

    The dependency dir itself, flat, and -- when USD_CHUNK_DEPENDENCY_DIR is not set -- the
    repository's _installed_deps, the default before packages moved to the per-user folder.
    """
    dirs = [base]
    if os.environ.get(USD_CHUNK_DEPENDENCY_DIR.key) is None:
        dirs.append(os.path.join(os.path.dirname(os.path.dirname(__file__)), "_installed_deps"))
    return [d for d in dirs if os.path.isdir(d)]


def _record_hash(path: str) -> str:
    """A file's hash spelled the way a wheel's RECORD spells it."""
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _intact_files(source: str, dist_info: str) -> list[str] | None:
    """The files a distribution installed into `source`, or None if any differs from RECORD.

    A shared folder that two interpreters both `pip install -t`'d into holds one copy of
    each file: numpy for 3.13 overwrote numpy for 3.11's .py files and left its .pyd beside
    them. Only a distribution whose every file still hashes as installed is whole.
    """
    record = os.path.join(source, dist_info, "RECORD")
    if not os.path.isfile(record):
        return None
    files = []
    with open(record, encoding="utf-8") as f:
        for line in f:
            path, _, rest = line.strip().partition(",")
            digest = rest.partition(",")[0]
            # Unhashed entries are RECORD itself and compiled .pyc; ".." ones are scripts
            # pip put beside the target, not in it.
            if not digest or path.startswith(".."):
                continue
            full = os.path.join(source, path)
            if not os.path.isfile(full) or _record_hash(full) != digest:
                return None
            files.append(path)
    return files


def migrate_legacy_dependencies(sources: list[str], target: str) -> list[str]:
    """Copy into `target` the packages in `sources` that this interpreter can import.

    Run while this Python version's folder has no packages yet, so a user upgrading
    is not asked to reinstall every chunk's packages. A distribution is copied only if its
    wheel tags fit this interpreter and its files are intact (_intact_files); anything else
    is left for the usual ask-to-install. Copied, not moved: the sources may also hold
    packages for another Python version, whose own first run migrates them. Several
    versions of one distribution: the newest that qualifies wins.

    Args:
        sources: Folders packages were `pip install -t`'d into.
        target: This Python version's dependency folder. It may already exist holding only
            the package cache file; anything else in it means it is in use, and nothing is
            copied.

    Returns:
        The distributions copied, as "name-version".
    """
    if sys_tags is None or not _no_packages_in(target):
        return []
    supported = {str(t) for t in sys_tags()}

    candidates: dict[str, list[tuple[Version, str, str]]] = defaultdict(list)
    for source in sources:
        for entry in os.listdir(source):
            if not entry.endswith(".dist-info"):
                continue
            name, _, version = entry[: -len(".dist-info")].rpartition("-")
            try:
                parsed = Version(version)
                with open(os.path.join(source, entry, "WHEEL"), encoding="utf-8") as f:
                    tags = {line.split(":", 1)[1].strip() for line in f if line.startswith("Tag:")}
            except (InvalidVersion, OSError):
                continue
            if tags & supported:
                candidates[_normalise_package(name)].append((parsed, source, entry))

    staging = f"{target}.migrating-{os.getpid()}"
    copied = []
    for versions in candidates.values():
        for _, source, dist_info in sorted(versions, reverse=True):
            files = _intact_files(source, dist_info)
            if files is None:
                continue
            for path in files:
                dest = os.path.join(staging, path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(os.path.join(source, path), dest)
            shutil.copytree(os.path.join(source, dist_info), os.path.join(staging, dist_info), dirs_exist_ok=True)
            copied.append(dist_info[: -len(".dist-info")])
            break

    try:
        if not _no_packages_in(target):
            # Another process (an xdist worker, a second usdview) migrated first.
            return []
        if copied:
            shutil.copytree(staging, target, dirs_exist_ok=True)
        # Once, even when nothing qualified: hashing every file of a large package (vtk)
        # again on each stage reload would buy nothing. The marker makes the folder in use.
        os.makedirs(target, exist_ok=True)
        open(os.path.join(target, _MIGRATED_MARKER), "w").close()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return copied


#: Left in a dependency folder once migrate_legacy_dependencies has run for it.
_MIGRATED_MARKER = ".migrated_legacy_dependencies"


def _no_packages_in(folder: str) -> bool:
    """Whether `folder` is missing or holds nothing but package cache files."""
    if not os.path.isdir(folder):
        return True
    return all(entry.endswith("_chunk_package_cache.json") for entry in os.listdir(folder))


class ChunkDependencyManager(metaclass=RegistrySingleton):
    """Installs and tracks the pip packages chunk modules declare as requirements.

    Packages go into USD_CHUNK_DEPENDENCY_DIR, which is added to sys.path. Nothing is
    installed without the scene's say-so; see USD_CHUNK_AUTO_INSTALL.
    """
    initialized = False
    #: Whether `pip list` has run this process -- see install_packages. Not reset by
    #: initialize(), which runs again on every stage reload.
    _listed = False
    #: Requirements the user said no to this process. Opening a network with ten nodes of
    #: one chunk loads that chunk ten times; asking again after each "no" is ten dialogs.
    #: A restart asks again.
    _declined: set = set()

    def initialize(self):
        """Put the dependency directory on sys.path and in PYTHONPATH for subprocesses."""
        self._cache = defaultdict(set)
        # One folder per Python version. usdview, Blender and a CLI venv run different
        # interpreters against the same USD_CHUNK_DEPENDENCY_DIR, and a compiled wheel
        # (numpy, mujoco) pip-installed for one cannot be imported by another -- while
        # `pip list --path` would still report it as installed to all of them.
        self.package_dir = ""
        self.package_dir = self.target_dir()

        if self.package_dir and _no_packages_in(self.package_dir):
            try:
                migrated = migrate_legacy_dependencies(
                    _legacy_dependency_dirs(USD_CHUNK_DEPENDENCY_DIR()), self.package_dir
                )
            except OSError:
                logger.warning("Could not migrate installed chunk dependencies", exc_info=True)
                migrated = []
            if migrated:
                logger.info("Migrated chunk dependencies to %s: %s", self.package_dir, ", ".join(migrated))

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
        checksum = chunk_cache.compute_checksum(python_executable())
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
        """Split an "==" requirement into (name, version); version is None without "==".

        Args:
            package: A requirement string such as "numpy==2.1.0".

        Returns:
            (name, version).
        """
        try:
            name, version = package.split("==")
        except ValueError:
            name = package
            version = None
        return name, version

    def install_packages(self, requirements, chunk_name: str = "") -> list[str]:
        """Install whichever of these requirements are not already satisfied.

        The scene is asked once for all of them (should_install_packages), naming the chunk
        that needs them; nothing is installed without its yes. With USD_CHUNK_AUTO_INSTALL
        set falsey, or a scene that cannot ask, the missing packages are named in a warning
        instead. Only what pip actually installed is cached.

        Args:
            requirements: Requirement strings, ranges allowed.
            chunk_name: The chunk module that declares them, for the question and the warning.

        Returns:
            The requirements still not installed afterwards -- declined, failed, or never
            asked. Empty when the manager is not initialized, since it cannot tell.
        """
        if not self.initialized:
            return []

        missing = []

        for package in requirements:
            if self.package_in_cache(package):
                if self._still_installed(package):
                    continue
                # The cache file outlived the package: removed by hand, or recorded by a
                # session that had another folder on sys.path. Trusted, it skipped the
                # install prompt and the chunk fell back or failed on import -- Blender's
                # Scatter Points ran its pure-Python path for minutes that way.
                self._forget(package)

            # One `pip list` per process, not one per chunk that misses. A package that is
            # genuinely absent stays absent for every chunk that wants it, and each of those
            # loads at once when a host generates its node classes -- Blender paid two
            # subprocesses per such chunk on the first open of a network. Installing through
            # this manager records what it installed, so nothing here goes stale short of an
            # install made by hand mid-session, which a restart picks up.
            if not self._listed:
                self.update_cache()
                type(self)._listed = True
                if self.package_in_cache(package):
                    continue

            missing.append(package)

        if not missing:
            return []

        # Asked about once per process: a "no" stands until restart -- see _declined.
        askable = [p for p in missing if p not in self._declined]
        mode = _install_mode()
        if askable and mode != "never" and (
            mode == "always" or self.should_install_packages(askable, chunk_name)
        ):
            if self.install(askable, chunk_name):
                missing = [p for p in missing if p not in askable]
        else:
            type(self)._declined.update(askable)

        if missing:
            # Say what is missing and how to get it, rather than failing later with an
            # import error from inside the chunk's module.
            quoted = " ".join(f'"{p}"' for p in missing)
            logger.warning(
                "Chunk %s needs %s, which is not installed. Install it with:\n"
                "    %s -m pip install -t \"%s\" %s",
                chunk_name or "module", ", ".join(missing), python_executable(), self.package_dir, quoted,
            )
        return missing

    def install(self, packages, chunk_name: str = "") -> bool:
        """pip-install `packages` into the dependency dir, now, without asking; whether pip succeeded.

        The scene runs pip (ChunkScene.run_install) so it can show progress; the bookkeeping
        after it stays here, on the calling thread.

        Args:
            packages: Requirement strings.
            chunk_name: The chunk that needs them, for the progress display.
        """
        if not self.initialized or not packages:
            return False
        packages = list(packages)
        args = [python_executable(), "-m", "pip", "install", "-t", self.package_dir]
        args.extend(packages)

        def run(on_line=None):
            return _run_pip(args, on_line)

        scene = ChunkRegistry().scene
        returncode, output = scene.run_install(packages, chunk_name, run) if scene else run()

        # Only cache what pip actually installed, after checking its return code. A failed
        # install (an embedded interpreter shipping no pip, no network, a bad package name)
        # remembered as a success would short-circuit package_in_cache forever: never
        # retried, never prompted again, and the chunk erroring on every creation.
        if returncode != 0:
            logger.error(
                "Failed to install chunk dependencies %s (pip exited %s). "
                "They will be retried the next time a chunk requires them.\n%s",
                packages, returncode, output.strip(),
            )
            return False

        if (pip_dir := self.package_dir) and pip_dir in sys.path_importer_cache:
            sys.path_importer_cache.pop(pip_dir)
        importlib.invalidate_caches()
        # A "no" given earlier no longer applies once the package is in.
        type(self)._declined.difference_update(packages)

        # By the package's name, not the requirement string: "numpy>=2,<3" is what a chunk
        # declares, and the cache is keyed the way `pip list` spells things. The version is
        # the one that is importable now that pip has run; None when that cannot be told,
        # which package_in_cache reads as satisfying whatever was asked for.
        for package in packages:
            name = split_requirement(package)[0]
            self._cache[name].add(installed_version(name))
        return True

    def target_dir(self) -> str:
        """Where install() puts packages -- for telling the user, before initialize() too."""
        if getattr(self, "package_dir", None):
            return self.package_dir
        base = USD_CHUNK_DEPENDENCY_DIR()
        return os.path.join(base, f"py{sys.version_info[0]}{sys.version_info[1]}") if base else ""

    def should_install_packages(self, packages, chunk_name: str = "") -> bool:
        """Whether the scene allows installing `packages` for `chunk_name`; False with no scene."""
        reg = ChunkRegistry()
        if not reg.scene or not self.initialized:
            return False
        return reg.scene.should_install_packages(list(packages), chunk_name)

    def update_cache(self):
        """Record every installed package: what Python can import, then `pip list` on top."""
        if not self.initialized:
            return

        # What Python itself can see, everywhere on sys.path: site-packages, PYTHONPATH
        # folders, the dependency dir. Needs no pip. usdview's bundled Python ships without
        # pip, so `pip list` below finds nothing there -- and every requirement read as
        # missing, even mujoco and numpy that were on PYTHONPATH all along.
        for dist in distributions():
            if (name := dist.metadata["Name"]) and dist.version:
                self._cache[name].add(dist.version)

        # Search the current interpreters' environment
        args = [python_executable(), "-m", "pip", "list", "--format", "freeze"]

        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW_FLAGS,
        )
        pip_packages = proc.stdout.decode().split()
        for p in pip_packages:
            pname, pversion = self.package_info(p)
            self._cache[pname].add(pversion)

        # Search the pip_dep_dir
        args = [python_executable(), "-m", "pip", "list", "--path", self.package_dir, "--format", "freeze"]

        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW_FLAGS,
        )
        pip_packages = proc.stdout.decode().split()
        for p in pip_packages:
            pname, pversion = self.package_info(p)
            self._cache[pname].add(pversion)

    @staticmethod
    def _still_installed(package) -> bool:
        """Whether this Python can still see `package`'s distribution on sys.path."""
        try:
            distribution_version(split_requirement(package)[0])
        except PackageNotFoundError:
            return False
        return True

    def _forget(self, package) -> None:
        """Drop every cached version of `package`, so the next lookup asks pip again."""
        wanted = _normalise_package(split_requirement(package)[0])
        for cached in [c for c in self._cache if _normalise_package(c) == wanted]:
            del self._cache[cached]

    def package_in_cache(self, package):
        """Whether `package` -- a requirement, range and all -- is satisfied by the cache.

        Reads the ranges chunks declare ("numpy>=2,<3"), not only "==". package_info splits
        on "==" alone, so a ranged requirement would come back with the whole string as its
        name, which no cache entry is ever keyed by -- every chunk declaring a range would
        miss the cache on every load, costing two `pip list` subprocesses each time.

        Args:
            package: A requirement string.

        Returns:
            True if a cached version satisfies it.
        """
        if not self.initialized:
            return False

        name, specifier = split_requirement(package)
        wanted = _normalise_package(name)
        versions: set = set()
        for cached, pvs in self._cache.items():
            if _normalise_package(cached) == wanted:
                versions |= set(pvs)
        if not versions:
            return False
        return any(_satisfies(v, specifier) for v in versions)


#: One source line with its ending, split where the parser splits: at \r\n, \r or \n and
#: nowhere else (not at the form feeds and other separators str.splitlines also honours).
_SOURCE_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


class _ConfigTransformer(ast.NodeTransformer):
    def __init__(self, code, globs: dict, module: ChunkModule) -> None:
        self._code = code
        self._module = module
        self._globs = globs.copy()
        self._deferred_config: Callable | None = None
        self._lines: list[str] | None = None

    def _get_node_source(self, node):
        """The source text of node, as ast.get_source_segment returns it.

        Not ast.get_source_segment itself: that splits the whole source into lines on
        every call, and this is called for every import, config() and main() of every
        chunk module, at discovery and again at load -- quadratic in a module's size, and
        seconds of every process start. The lines are split once here instead. Column
        offsets are UTF-8 byte offsets, as in the original.
        """
        try:
            if node.end_lineno is None or node.end_col_offset is None:
                return None
            first, last = node.lineno - 1, node.end_lineno - 1
            start, end = node.col_offset, node.end_col_offset
        except AttributeError:
            return None
        if self._lines is None:
            self._lines = _SOURCE_LINE.findall(self._code)
        lines = self._lines
        if first == last:
            return lines[first].encode()[start:end].decode()
        return "".join([
            lines[first].encode()[start:].decode(),
            *lines[first + 1:last],
            lines[last].encode()[:end].decode(),
        ])

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
            logger.warning("Could not evaluate config() for %s: %s", self._module.module_name, e)

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

    @override
    def get_filename(self, fullname: str) -> str:  # noqa: ARG002
        return self.path

    @override
    def get_data(self, path: str | bytes) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    @override
    def load_module(self, fullname: str) -> types.ModuleType:
        return self._loader.load_module(fullname)

    @override
    def exec_module(self, module: types.ModuleType) -> None:
        source_bytes = self.get_data(self.get_filename(self.fullname))
        source = source_bytes.decode("utf-8")
        self._module.source_hash = pin.hash_file(self.path)

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
    "OCP": "cadquery-ocp",
    "usd": "usd-core",
    "pxr": "usd-core",
}

#: Import roots every chunk may use without declaring: the host's own USD build, its Qt,
#: the framework, and anything the interpreter ships. PySide6 belongs here for the same
#: reason pxr does -- a chunk drawing a custom widget draws it into the host's toolkit and
#: cannot bring its own.
_ASSUMED_IMPORTS = {"pxr", "usd_chunks", "PySide6"}

#: Import roots a host's own Python ships, assumed only for a chunk gated to that host
#: (config hosts=[...]): like pxr, they come with the application and there is no
#: distribution to declare. Not in _ASSUMED_IMPORTS, where any chunk could import bpy.
_HOST_IMPORTS = {"blender": {"bpy", "bmesh", "mathutils"}, "houdini": {"hou"}}


def _normalise_package(name: str) -> str:
    """PEP 503 name, with any version specifier or extra dropped."""
    for sep in ("[", "<", ">", "=", "!", "~", ";", " "):
        name = name.split(sep)[0]
    return re.sub(r"[-_.]+", "-", name).strip().lower()


@functools.lru_cache(maxsize=1)
def _packages_distributions() -> dict:
    return packages_distributions()


def candidate_distributions(imported: str) -> set:
    """Every pip name that could plausibly supply this import.

    Several distributions can provide one module -- opencv-python and
    opencv-python-headless both give you cv2 -- so there is no single right answer to check
    against. The static map covers the well-known renames without needing anything
    installed; the environment is consulted as well, which catches the variants the map
    does not know, but is never the only source or a chunk would pass or fail on which
    variant this particular machine happens to have.

    Args:
        imported: A top-level import name.

    Returns:
        Normalised distribution names.
    """
    candidates = {imported, IMPORT_TO_PACKAGE.get(imported, imported)}
    candidates.update(_packages_distributions().get(imported, ()))
    return {_normalise_package(name) for name in candidates}


#: Descending into one of these would leave module scope. Everything else -- try, if, with
#: -- is still module scope and can hold an import.
_NOT_MODULE_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def top_level_imports(module_file) -> set:
    """The root names a chunk module imports at module scope, read without executing it.

    Walks into try/except, if and with rather than only `tree.body`, so a guarded import is
    seen. rbd_solver is exactly that case::

        try:
            import mujoco as _mj
            import numpy as _np
            _HAS_MUJOCO = True
        except ImportError:
            _HAS_MUJOCO = False

    Reading only the literal top level would report nothing for that file, in the one chunk
    where a version silently changing the answer matters most -- try-guarding a heavy
    optional is the normal pattern for precisely the chunks that carry a simulator.

    Function and class bodies are still skipped, which is the actual house rule: imports go
    at module top level, never inside a function. A try-guarded import obeys that rule
    lexically; it just is not an ast.Import node in tree.body, which is a different thing.

    Args:
        module_file: The chunk module's file.

    Returns:
        The imported root names, empty if the file cannot be read or parsed.
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


def undeclared_imports(module: ChunkModule, sibling_modules=()) -> list:
    """Third-party imports a chunk makes without declaring them in config().

    An undeclared dependency is not only an install that breaks on a clean machine -- it is
    a version that never reaches a run's manifest. A chunk importing numpy without saying so
    means a numpy upgrade can change its results while the manifest still reads clean, which
    is worse than having no manifest, because it looks authoritative.

    Args:
        module: The chunk module.
        sibling_modules: The other chunk modules' import names, which resolve locally and
            are not dependencies to declare.

    Returns:
        The undeclared import names.
    """
    # split_requirement, not package_info: the latter only understands "==", so a
    # requirement declared as a range would come back with the whole string as its name
    # and every import it covers would read as undeclared.
    declared = {
        _normalise_package(split_requirement(r)[0])
        for r in (module.config.requirements or [])
    }
    declared.update(_normalise_package(m) for m in (module.config.required_modules or []))

    host_imports = set()
    for entry in module.config.hosts or []:
        if match := _HOST_ENTRY.match(entry or ""):
            host_imports |= _HOST_IMPORTS.get(match.group(1).lower(), set())

    undeclared = []
    for root in sorted(top_level_imports(module.module_file)):
        if root in _ASSUMED_IMPORTS or root in host_imports or root in sys.stdlib_module_names:
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

    Args:
        requirement: A requirement string.

    Returns:
        (name, specifier).
    """
    text = str(requirement).strip()
    for op in _SPECIFIER_OPS:
        if op in text:
            head, _, _ = text.partition(op)
            return head.strip(), text[len(head):].strip()
    return text, ""


def _satisfies(version, specifier: str) -> bool:
    """Does an installed `version` meet `specifier` ("" for any)?

    None is a version this manager could not determine after an install it made itself,
    and counts as meeting anything -- the alternative is re-installing on every load. A
    version or specifier packaging cannot parse falls back to a literal "==" match.
    """
    if version is None or not specifier or SpecifierSet is None:
        return True
    try:
        return Version(version) in SpecifierSet(specifier)
    except (InvalidVersion, InvalidSpecifier):
        return specifier == f"=={version}"


def unconstrained_requirements(module: ChunkModule) -> list:
    """Requirements declared with no version constraint at all.

    A bare "mujoco" says nothing, so two runs a month apart can use different solvers from
    an identical recipe and nothing recorded explains the difference.

    A *range* is what a chunk should declare -- what it is compatible with -- rather than an
    exact pin. Exact pins do not compose: two chunks pinning numpy==2 and numpy==3 have no
    solution, there is one sys.modules['numpy'], and resolving it would mean editing someone
    else's chunk. Ranges intersect, and the exact version belongs in the recipe that runs
    them, recorded as what was actually imported.

    Args:
        module: The chunk module.

    Returns:
        The unconstrained requirement strings.
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

    Args:
        modules: The chunk modules to check.

    Returns:
        {package: [(module_name, specifier, installed), ...]} for the failures only.
    """
    if SpecifierSet is None:  # pragma: no cover - packaging ships with pip
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

    Args:
        package: A distribution name; "-" and "_" spellings are both tried.

    Returns:
        The installed version, or None.
    """
    for candidate in (package, package.replace("-", "_"), package.replace("_", "-")):
        try:
            return distribution_version(candidate)
        except PackageNotFoundError:
            continue
    return None


class ChunkModule:
    """One chunk module file, as the ChunkRegistry manages it.

    Args:
        module (ModuleType | None): The loaded chunk module
        module_name (str): The name of the module
        module_file (str): The original, absolute, path to the python module
    """

    #: pin.hash_file of the source as the loader last read it -- what
    #: info:moduleHash is stamped with and compared against. Set when the file is read, so it
    #: is the code this module runs and not whatever the file holds now. None until then, and
    #: for a module built without a file.
    source_hash: str | None = None

    def __init__(
        self,
        module: types.ModuleType | None = None,
        module_name: str = "",
        module_file: Path | str = Path(),
    ):
        self.module = module
        self.module_name = module_name
        self.module_file = Path(module_file)

        self.loaded = False
        # self.loaded_chunks: weakref.WeakValueDictionary[str, ChunkBase] = weakref.WeakValueDictionary()
        self.func: Callable[[], core.ChunkBase | list[core.ChunkBase]] | None = None

        self._has_main = False

        # This is set by the _ConfigTransformer when parsing the module (before loading) if the module
        # defines a config() function
        self.config: core.ChunkConfig = core.ChunkConfig()
        # Call _exec_module to lazily load the module. This runs the _ConfigTransformer. If module_file is .py and
        # module is not a RuntimeModule, _exec_module will create a _ChunkConfigLazyLoader
        #
        # Whether the file needs reading again before the next load: not after this read
        # succeeds, until unload() says the file may have changed. Reading it twice parses and
        # transforms it twice, and was half of what finding a scene's chunk modules cost.
        self._needs_read = not self._exec_module()

    @staticmethod
    def from_chunk_definition(file: str | Path, name: str | None = None) -> list[ChunkModule]:
        """Create a ChunkModule from a chunk module file (.py).

        Args:
            file: The module file.
            name: The module name; defaults to the file's stem.

        Returns:
            list[ChunkModule]: A list holding the created module, or empty for anything
            that is not a .py file.
        """
        p = Path(file)

        if name is None:
            name = p.stem

        if p.suffix != PY_MODULE_EXT:
            logger.warning("Cannot import %s: a chunk module is a .py file", p.name)
            return []

        # No deserialising to do -- loading the module is ChunkModule's own job.
        return [ChunkModule(module_name=name, module_file=file)]

    def __repr__(self) -> str:
        """The module's name and file."""
        return f"ChunkModule(module_name='{self.module_name}', module_file='{self.module_file.as_posix()}')"

    def get_menu_path(self) -> list[str]:
        """Get the menu namespace this module lives under, outermost element first.

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

    def _exec_module(self):
        if not self.module_file.exists():
            logger.error("Could not load module from file %s", self.module_file)
            return False

        elif self.module_file.suffix == PY_MODULE_EXT:
            spec = util.spec_from_file_location(self.module_name, self.module_file)
            if not spec or not spec.loader:
                logger.warning("Module spec or loader failed for module %s. Skipping", self.module_name)
                return False

            loader = _ChunkLazyConfigLoader(self.module_name, str(self.module_file), spec.loader, self)
            spec.loader = loader
            module = util.module_from_spec(spec)

            try:
                spec.loader.exec_module(module)
            except Exception:
                # This module failed to load. Set to file_name so we can try loading it again later§
                logger.exception("Failed to load module %s", self.module_name)
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

    def _install_deps(self) -> list[str]:
        """Install the module's requirements, asking first; the ones still missing."""
        if not self.config.requirements:
            return []

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

        return dep_reg.install_packages(self.config.requirements, self.module_name)

    def _pre_load(self):
        if not self.loaded and (self._needs_read or not self.module):
            if not self._exec_module():
                return False
            self._needs_read = False

        # Tried even with requirements still missing: a chunk may guard an optional import
        # and load without it (cad_import shows a "not installed" readout), and the cache
        # that says what is installed can be wrong where an import is not.
        missing = self._install_deps()

        try:
            self.func = getattr(self.module, "main")
        except Exception:
            # This module failed to load. Set to file_name so we can try loading it again later
            if missing:
                # The import of what was declined is what failed: say that, not the
                # traceback -- install_packages has already said how to install it.
                logger.error(
                    "Chunk %s could not load: it needs %s, which is not installed.",
                    self.module_name, ", ".join(missing),
                )
            else:
                logger.exception("Failed to run module %s", self.module_name)
            self.loaded = False
            # The lazy module ran half-way and stays broken: asked for again it reports
            # "has no attribute 'main'" even once the package is in. Drop it, so the next
            # load reads the file afresh.
            if sys.modules.get(self.module_name) is self.module:
                del sys.modules[self.module_name]
            self.module = None
            self._needs_read = True
            return False

        if not self.module:
            return False

        self.loaded = True
        return True

    def load(self) -> Sequence[core.ChunkBase]:
        """Load the module and create its chunks.

        Runs the one-time initialization before create(). Once loaded, call create() directly
        to skip it.

        Returns:
            Sequence[ChunkBase]: The created chunks, empty if loading failed.
        """
        if not self._pre_load():
            logger.warning("Could not load module %s", self.module)
            return []

        start = timer()

        res = self.create()
        self.loaded = len(res) > 0
        end = timer()

        logger.info("loaded module: %s. Load took %s seconds", self.module_name, end - start)
        return res

    def installed_packages(self) -> dict[str, str]:
        """The installed version of each package config.requirements names.

        A package unpacked into a folder on PYTHONPATH, as the usdview bundles do it, has no
        install record to read a version from; if it is imported, its __version__ stands in.

        Returns:
            {package: installed version}; a package with no version to find is left out.
        """
        packages = {}
        for requirement in self.config.requirements or []:
            name, _ = split_requirement(requirement)
            if name in packages:
                continue
            installed = installed_version(name)
            if installed is None:
                imported = sys.modules.get(name) or sys.modules.get(name.replace("-", "_"))
                installed = getattr(imported, "__version__", None)
            if installed is not None:
                packages[name] = str(installed)
        return packages

    def unload(self):
        """Mark the module as not loaded."""
        self.loaded = False
        # Reload Chunks unloads to pick up an edited file, so the next load reads it again.
        self._needs_read = True

    def create(self) -> Sequence[core.ChunkBase]:
        """Run the module's main function and return the chunks it made, with its config attached.

        The chunks are not added to a scene; the caller does that.

        Returns:
            Sequence[ChunkBase]: The created chunks, empty if main failed or is missing.
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
            logger.warning("module %s does not have a function for creating chunks", self.module_name)
            return []

        if self.config:
            for c in loaded:
                self.config.module = self.module_name
                # The config's module name is all a chunk holds of its module: hooks are
                # looked up in the registry by it (ChunkBase.get_chunk_module), so a chunk
                # runs what its saved info:chunkType will run on reopen, and nothing else.
                c._config = self.config
                # Here, at creation, so a load restores a saved untick onto it like any
                # other param -- values are read back before the chunk joins the scene.
                if (self.config.cache_cook and not self.config.cache_always
                        and c.get_param(cook_cache.TOGGLE) is None):
                    c.add_param(core.Checkbox(
                        cook_cache.TOGGLE, "Cache Result", default_value=True,
                        tooltip="Read this node's result back from the cook cache when "
                        "nothing it depends on has changed, instead of cooking it again. "
                        "USD_CHUNKS_COOK_CACHE=0 turns the cache off for every node.",
                    ))

        return loaded

    def should_load_for_filter(self) -> bool:
        """Check if this module passes USD_CHUNK_LOAD_FILTER.

        Returns:
            bool: True if no filter is set or the module is named in it
        """
        if module_filter := USD_CHUNK_LOAD_FILTER():
            if self.module_name not in module_filter:
                return False
        return True

    def has_config(self):
        """Whether the module defines a config."""
        return bool(self.config)

    def has_main(self):
        """Whether the module defines a main function."""
        return self._has_main

    def should_load_for_host(self, name: str, version: str) -> bool:
        """Whether config.hosts lets the module load in host `name` at `version`.

        Empty hosts means every host. Otherwise one entry must name this host, and its
        specifier (if any) must admit the version: "blender>=5.1" admits "5.1.0", and a
        host that reports no version only matches an entry without a specifier.

        Args:
            name: The host's name, from SceneSender.get_host().
            version: Its version string.

        Returns:
            bool: True if the module may load here
        """
        hosts = self.config.hosts if self.config else None
        if not hosts:
            return True
        for entry in hosts:
            match = _HOST_ENTRY.match(entry or "")
            if match is None or match.group(1).lower() != (name or "").lower():
                continue
            spec = match.group(2).strip()
            if not spec:
                return True
            if SpecifierSet is None or not version:
                continue
            try:
                if Version(version) in SpecifierSet(spec):
                    return True
            except (InvalidSpecifier, InvalidVersion):
                logger.warning("%s: cannot compare host %s %s against %r",
                               self.module_name, name, version, entry)
        return False

    def should_load_for_plugin(self, plugin_name: str) -> bool:
        """Check if the module may be used with a plugin.

        An empty config.load_for_plugin means the module is safe to load everywhere.

        Args:
            plugin_name (str): The name of the plugin

        Returns:
            bool: True if the ChunkModule can be used with the plugin plugin_name
        """
        if self.config.load_for_plugin and plugin_name not in self.config.load_for_plugin:
            return False
        return True

    def export(self, path: str | Path):
        """Export this ChunkModule's source as a .py file. Any other extension warns and fails.

        Args:
            path (str | Path): The path to export to. Should be .py
        """
        p = Path(path)

        if not self.module:
            return

        if p.suffix != PY_MODULE_EXT:
            logger.warning("wrong file extension %s", path)
            return

        try:
            source = inspect.getsource(self.module)
        except (OSError, TypeError):
            logger.exception("Could not read the source of %s", self.module_name)
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


_WORD_SPLIT = re.compile(r"[^0-9a-zA-Z]+|(?<=[a-z0-9])(?=[A-Z])")


def _words(text: str) -> list[str]:
    """Lowercased words of a menu label: split on separators and camelCase humps."""
    return [w.lower() for w in _WORD_SPLIT.split(text) if w]


def _subsequence_gaps(token: str, text: str) -> int | None:
    """How far the tightest in-order match of `token` in `text` spreads beyond its own length.

    Returns None when token's letters do not all appear in order.
    """
    best = None
    for start in (i for i, ch in enumerate(text) if ch == token[0]):
        pos = start
        for ch in token[1:]:
            pos = text.find(ch, pos + 1)
            if pos < 0:
                break
        else:
            gaps = pos - start + 1 - len(token)
            best = gaps if best is None else min(best, gaps)
    return best


def _token_score(token: str, label: str, path: tuple[str, ...]) -> int:
    """How well one query word matches an entry; 0 for no match. Higher is closer.

    The label is what you are looking for, so every way of matching it outranks matching
    the category it sits in -- except a loose, letters-in-order match, which is a guess.
    """
    name = label.lower()
    words = _words(label)
    if name == token:
        return 1000
    if name.startswith(token):
        return 900 - len(name)
    if any(w.startswith(token) for w in words):
        return 700 - len(name)
    if (at := name.find(token)) >= 0:
        return 600 - at - len(name)
    if len(token) > 1 and "".join(w[0] for w in words).startswith(token):
        return 550 - len(name)
    for part in path:
        part_words = _words(part)
        if part.lower().startswith(token) or any(w.startswith(token) for w in part_words):
            return 400
        if token in part.lower():
            return 350
    compact = "".join(words)
    if len(token) > 2 and (gaps := _subsequence_gaps(token, compact)) is not None and gaps <= len(token):
        return 300 - 10 * gaps - len(name)
    return 0


#: Enough to settle a near-tie between a chunk and a variant, not to outrank a closer match.
_BASE_ENTRY_BONUS = 60


def match_score(query: str, label: str, path: tuple[str, ...] = ()) -> int:
    """How well query matches label, or the category path it sits in; 0 if it does not.

    Every word of query must match. The scoring is search_menu_entries', so the chunk menu
    and the node graph's search rank the same way.

    Args:
        query: What was typed; case and surrounding whitespace are ignored.
        label: The name being searched for.
        path: Weaker names the label also answers to, such as its category.

    Returns:
        The score, higher for a closer match; 0 for an empty query or any word that misses.
    """
    total = 0
    for token in query.lower().split():
        if not (score := _token_score(token, label, path)):
            return 0
        total += score
    return total


def search_menu_entries(entries: list[MenuEntry], query: str) -> list[MenuEntry]:
    """The entries matching query, closest first -- the chunk menu's search.

    Every word of the query must match the entry's name or its category path. A name that
    starts with the query ranks first (shorter names ahead), then a name with a word that
    starts with it, then the query anywhere in the name, then the name's initials ("vmo"
    for vector_math_ops), then the category, then the query's letters in order with a few
    missing in between ("tranform"). Shared by every frontend so "trans" lists the same
    chunks in the same order everywhere.

    Args:
        entries (List[MenuEntry]): The entries to search, in menu order
        query (str): What was typed; case and surrounding whitespace are ignored

    Returns:
        List[MenuEntry]: Matching entries, best first; all of entries, unchanged, for an
        empty query.
    """
    if not query.split():
        return list(entries)
    scored = []
    for index, entry in enumerate(entries):
        label = entry.label
        if total := match_score(query, label, tuple(entry.namespace[:-1])):
            if entry.presets is None:
                # A chunk ahead of a variant that matches about as well: one module can
                # declare hundreds (every MaterialX node), and "ref" should reach Reference
                # before the Reflect shader.
                total += _BASE_ENTRY_BONUS
            scored.append((-total, len(label), label.lower(), index, entry))
    scored.sort(key=lambda s: s[:4])
    return [s[-1] for s in scored]


_FIRST_CATEGORY = "examples"


def _category_order(entry: MenuEntry) -> tuple:
    """Sort key for a menu entry's category.

    Examples come first, the rest A-Z, and chunks with no category after every category.
    """
    category = tuple(part.lower() for part in entry.module.get_menu_path()[:-1])
    if not category:
        return (2,)
    return (0 if category[0] == _FIRST_CATEGORY else 1, category)


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

    module: ChunkModule
    namespace: tuple[str, ...]
    tooltip: str = ""
    presets: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        """The text this entry shows in a menu."""
        return self.namespace[-1] if self.namespace else self.module.module_name

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
                # Values only, no cook. Setting a param runs the chunk's module, and here
                # the chunk is not in any context yet: it cooked into a layer the stage
                # had not adopted, and when the real cook came on CHUNK_LOADED a module
                # that remembered what it last authored (light.py's since-removed _last_light_type)
                # saw nothing new and returned -- a "Dome Light" entry created the chunk
                # with the right menu value and no prim. Same guard the Qt paste path
                # sets before applying copied values (_paste_guard_sets): execute()
                # still solves and notifies, so the widgets built on add see the preset,
                # but skips module.execute while _executing is set.
                prev = getattr(c, "_executing", False)
                c._executing = True
                try:
                    for param_name, value in self.presets.items():
                        if param := c.get_param(param_name):
                            param.value = value
                finally:
                    c._executing = prev

        return chunks


class ChunkRegistry(metaclass=RegistrySingleton):
    """A singleton that finds and registers chunk modules.

    By default chunk modules are scanned for in the USD_CHUNK_DIRS directories.

    Attributes:
        registered_directories (set[str]): A set of directories we have searched
        registered_modules (dict[str, ChunkModule]): A mapping between module names and modules
        scene (SceneSender): An instance of the SceneSender for accessing the scene
        plugin (str): A string identifier of the currently loaded plugin
    """

    registered_directories: set[Path] = set()
    registered_modules: dict[str, ChunkModule] = {}
    scene: SceneSender
    plugin: str

    # Duplicate module names skipped during registration, as
    # (name, winner_file, loser_file). Reported after discovery. See
    # _report_shadows / _tier_of.
    shadowed_modules: list[tuple[str, Path, Path]] = []
    _global_dir: Path | None = None

    initialized: bool = False

    def initialize(self, scene_sender: SceneSender, plugin: str) -> None:
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
        logger.info("Found %s chunks. Loaded %s", len(self.registered_modules), c)
        self._report_shadows()
        self.initialized = True

    def register_chunk_environ_directories(self) -> list[Path]:
        """Register USD_CHUNK_DIRS, then append the GLOBAL_USDCHUNKS directory.

        On a duplicate module name the first-registered wins (see register_module), so the
        USD_CHUNK_DIRS entries -- your proprietary / on-prem chunks -- override marketplace
        chunks installed into the appended GLOBAL_USDCHUNKS dir.

        Returns:
            The registered directories, in precedence order.
        """
        # The bundled chunks are a default, not something the user asked for: of its two
        # locations (checkout, installed package) only one exists, and the other is skipped
        # quietly instead of reported as "directory not found".
        defaults = set(USD_CHUNK_DIRS.default)
        dirs = [d for d in USD_CHUNK_DIRS() if d not in defaults or Path(d).exists()]
        global _warned_no_chunk_dirs
        if not dirs and not _warned_no_chunk_dirs:
            _warned_no_chunk_dirs = True
            logger.warning(
                "Set USD_CHUNK_DIRS! It is empty, so your chunk folders are not loaded. "
                "Set it to your chunk folders, like C:\\path\\to\\usd_chunks\\chunks. "
                "See docs/install.md#environment-variables."
            )
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
        """Add a single directory to the register.

        Args:
            chunk_dir (str | Path): The directory to add
        """
        if (p := Path(chunk_dir)) and not p.exists():
            logger.warning("%s directory not found.", chunk_dir)
            return

        if p in self.registered_directories:
            # directory already loaded
            return

        sys.path.append(str(chunk_dir))
        self.registered_directories.add(p)

    def register_chunk_modules(self, paths: Iterable[Path]):
        """Recursively register the chunk module files (.py) under each directory.

        Args:
            paths (Iterable[Path]): The directories containing the chunk modules to register.
        """
        for working_dir in paths:
            # Loop over all directories of the working_dirs directory
            for module_path in Path(working_dir).glob("**/*" + PY_MODULE_EXT):
                # A chunk's name is its file name alone, never its folder: the name is what a
                # scene saves as info:chunkType, so sorting chunks into category folders must
                # not rename them (and break every scene saved before the move).
                module_name = module_path.stem

                for mod in self._discover_module(module_path, module_name):
                    self.register_module(mod)

    @staticmethod
    def _discover_module(module_path: Path, module_name: str) -> list[ChunkModule]:
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
        """Register a ChunkModule, keeping it alive and accessible.

        Args:
            module (ChunkModule): The module to add to the registry

        Returns:
            ChunkModule | None: The registered module, or None if it was skipped
        """
        if not module.has_main():
            logger.info("Skipping %s. Module has no main function defined", module.module_name)
            return
        if not module.has_config():
            logger.info("Skipping %s. Module has no config function defined", module.module_name)
            return
        if not module.module:
            logger.info("Skipping %s. Module has no valid python source module", module.module_name)
            return
        if not module.should_load_for_plugin(self.plugin):
            logger.info("Skipping %s. Module should not be registered in this context", module.module_name)
            return
        if not module.should_load_for_filter():
            logger.info("Skipping %s. Module is excluded in CHUNK_MODULE_FILTER", module.module_name)
            return
        get_host = getattr(self.scene, "get_host", None)  # a bare test scene may not have one
        host, host_version = get_host() if get_host is not None else ("", "")
        if not module.should_load_for_host(host, host_version):
            logger.info("Skipping %s. Module is for %s, not %s %s", module.module_name,
                        ", ".join(module.config.hosts), host or "this host", host_version)
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
            logger.debug("Skipping %s. Duplicate module: %s", module.module_file, name)
            return

        self.registered_modules[name] = module
        return self.registered_modules[name]

    def _tier_of(self, file: Path) -> str:
        """The precedence tier a module file belongs to.

        That is the appended GLOBAL_USDCHUNKS install dir, or an explicit USD_CHUNK_DIRS entry.
        """
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
                    "shadowed chunk '%s': using %s [%s], ignoring %s [%s]",
                    name, winner, self._tier_of(winner), loser, self._tier_of(loser),
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
                "usd_chunks: %d shadowed chunk name(s) at the same precedence tier (%s) "
                "-- set USD_CHUNK_VERBOSE=1 to list",
                len(same_tier), names,
            )

    def get_chunk_module(self, module_name: str) -> ChunkModule | None:
        """Get the ChunkModule registered under a module name.

        The name is the chunk's file name without its folder or extension, e.g. A/B/C/foo.py is
        'foo'. A folder-qualified name such as 'B.C.foo', as older scenes saved it, resolves to
        the same module.

        Args:
            module_name (str): The name of the module to find

        Returns:
            ChunkModule | None: The module, or None if not found
        """
        if found := self.registered_modules.get(module_name):
            return found
        # Scenes saved while chunks were named by folder ("modelling.box", "core_usd.prune_prim")
        # still resolve: a chunk's name is now its file name alone.
        return self.registered_modules.get(module_name.rpartition(".")[2])

    def get_chunk_modules(self, loaded=False, sort_result=False) -> list[ChunkModule]:
        """Every registered chunk module.

        Args:
            loaded: Only the modules that are loaded.
            sort_result: Sort by module name.

        Returns:
            The modules.
        """
        v = list(self.registered_modules.values())
        if loaded:
            v = [x for x in v if x.loaded]
        if not sort_result:
            return v
        return sorted(v, key=lambda x: x.module_name.split(".")[-1])

    def get_menu_entries(self, sort_result: bool = True) -> list[MenuEntry]:
        """Get every entry a chunk-creation menu should offer, described without reference to any UI.

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
        entries: list[MenuEntry] = []

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

        if sort_result:
            # A menu nests submenus in the order it first meets them, so categories are
            # ordered here, or they come out in the order of their alphabetically-first chunk.
            # Stable, so chunks keep their name order (and variants their place) within one.
            entries.sort(key=_category_order)

        return entries

    def import_chunk(self, path: str | Path):
        """Import a chunk module from path. The file extension of path should be .py.

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
        """Reload every module, recreating as many chunks of each as the scene held."""
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
    a set lookup. The alternative -- deriving it by walking every tracked layer's sublayers --
    has to be redone against live subLayerPaths every time it is asked, which is once per
    reconcile pass and one pass per wire, and the layer stack grows with the graph.

    Deliberately NOT stored on the layer itself, which is the obvious place and is wrong:
    Sdf.Layer.Clear() drops customLayerData along with everything else, and Clear() on its
    own layer is exactly what a procedural chunk does every time it cooks. A mark there
    survives until the chunk first runs and then silently disappears.

    Args:
        layer: The layer to check.

    Returns:
        True for a layer the chunk graph created.
    """
    if not layer:
        return False
    return layer.identifier in ChunkLayerManager().internal_layer_ids


def _without_sublayers(parent: Sdf.Layer, identifiers: set) -> list:
    """Parent's subLayerPaths with identifiers dropped, order preserved, duplicates collapsed.

    Both the "drop every occurrence" and the de-dup matter, because an anonymous layer's
    identifier embeds its address (anon:<address>:<tag>) and addresses get recycled: once a
    tracked layer is freed while its identifier is still listed here, a later anon layer can
    be handed the same address and produce a byte-identical identifier. subLayerPaths is a
    list editor that rejects duplicates outright, so assigning such a list raises rather than
    edits, and the delete never completes. Collapsing them is the only legal state anyway --
    two entries could never have named two different live layers at once.
    """
    seen: set = set()
    return [
        p for p in parent.subLayerPaths
        if p not in identifiers and not (p in seen or seen.add(p))
    ]


class ChunkLayerManager(metaclass=RegistrySingleton):
    """A singleton that creates, tracks and removes the layers chunks author into.

    Every chunk is associated with a layer onto which the edits of its parameters are
    applied. The layers are created and deleted along with the chunks.

    Attributes:
        chunk_layers (dict[ChunkHash, Sdf.Layer]): Maps each chunk to its layer
        scene (SceneSender): A handle to the scene for stage access
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
        """Set up the manager for a stage. Call at least once before using it.

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

        self.chunk_layers: dict[core.ChunkHash, Sdf.Layer] = {}
        # Keyed by (graph_id, target_layer_identifier) -- a graph's chunks get split into a
        # separate sibling group per Edit Target they're actually authored against, since a
        # single graph_layer can only ever be a sublayer of one target at a time.
        self.graph_layers: dict[tuple, Sdf.Layer] = {}
        # Which real layer each graph_layer entry is currently sublayered under -- needed to
        # detect and reparent when the resolved Edit Target changes between calls.
        self._graph_layer_target: dict[tuple, Sdf.Layer] = {}
        # Which real layer each individual chunk's content currently resolves against (same
        # value as its graph_layer's entry above, but keyed per-chunk for easy lookup) -- lets
        # export tooling group a chunk's own bookkeeping prim with whichever layer its driven
        # content nests in (see chunks/core/export_usd.py's per-layer export mode).
        self._chunk_target: dict[core.ChunkHash, Sdf.Layer] = {}
        # The graph_key each chunk's layer was last placed under. get_or_create_layer only
        # has to look for that layer in OTHER graph layers when this has changed since --
        # and it is asked several times per chunk per cook, nearly always with nothing moved.
        self._chunk_graph_key: dict[core.ChunkHash, tuple] = {}
        # Hashes of chunks whose layer has been torn down. Never served a layer again, so a
        # notice fired by the teardown itself can't resurrect one -- see _chunk_deleted.
        self._retired_chunks: set = set()
        # Chunk hash -> identifier of the graph layer get_or_create_layer last saw its layer in.
        self._confirmed_in: dict[core.ChunkHash, str] = {}
        # Layers of chunks deleted inside holding_deletes, taken off the stage at its end.
        self._held_deletes: list[Sdf.Layer] | None = None
        # Chunks whose layer exists but waits to be placed, while defer_placement is on; None
        # when it is off.
        self._deferred_placement: dict[core.ChunkHash, weakref.ReferenceType] | None = None
        # Where those layers sit meanwhile; see defer_placement.
        self._loading_layer: Sdf.Layer | None = None
        # Graph layer identifier -> (layer, its sublayer list as edited so far), while
        # place_deferred_layers writes each graph layer once; None otherwise.
        self._sublayer_batch: dict[str, tuple[Sdf.Layer, list[str]]] | None = None
        # The graph layers that batch may write: this manager's own, which hold no offsets.
        self._batchable: set[str] = set()
        # The cook layer of each file layer that defines chunks, by that layer's identifier;
        # see cook_layer_for.
        self._cook_layers: dict[str, Sdf.Layer] = {}
        # Per cook layer, the layer of `active = false` overs that hides what an asset's
        # published bake makes and this stage's live cooks no longer do; see
        # deactivate_stale_bakes. Taken out of their cook layers before being dropped: a cook
        # layer is a file layer that outlives this, and an anonymous identifier is reused once
        # its layer is freed -- a stale entry would then name an unrelated layer.
        self._drop_deactivations()
        self._deactivations: dict[str, tuple[Sdf.Layer, Sdf.Layer]] = {}
        chunk_cook.after_cook(self._after_cook)
        # Every chunk given a layer, for save_cook_layers to find the ones a bake holds.
        self._placed: dict[core.ChunkHash, weakref.ReferenceType] = {}
        self._chunk_refs = weakref.WeakSet()
        # Identifiers of every layer above that this manager created, so is_chunk_internal_layer
        # can answer in one lookup. Maintained beside the two dicts rather than derived from
        # them on demand, and dropped with them, because an anonymous identifier embeds an
        # address and addresses get recycled: a leftover entry would eventually claim an
        # unrelated layer.
        self.internal_layer_ids: set = set()

        # When initialized, the stage may be None as the chunks are loaded prior to loading the stage
        if getattr(self, "stage", None) is not stage:
            skip_cook.release_cook_stage()  # kept for the stage this manager had before
        self.stage: Usd.Stage = stage

        self.initialized = True

    def reset(self):
        """Return the singleton to its pre-initialize state, dropping every layer mapping.

        Used when switching stages (reload, tests) so state from the old stage's stack doesn't
        leak into the new one. Keeps this class the single owner of its private field list --
        callers must not clear fields directly.
        """
        # The cook stage kept between cooks holds the old stage's layers, and hears them.
        skip_cook.release_cook_stage()
        self.initialized = False
        self.stage = None
        self.chunk_layers = {}
        self.graph_layers = {}
        self._graph_layer_target = {}
        self._chunk_target = {}
        self._chunk_graph_key = {}
        self._retired_chunks = set()
        self._confirmed_in = {}
        self._held_deletes = None
        self._deferred_placement = None
        self._loading_layer = None
        self._sublayer_batch = None
        self._drop_deactivations()
        self._cook_layers = {}
        self._placed = {}
        self.internal_layer_ids = set()

    def get_layer_file_name(self, layer: Sdf.Layer):
        """The file name a layer is saved under: its display name without the unique suffix."""
        return "_".join(layer.GetDisplayName().split("_")[:-1]) + ".usda"

    def save_layer(self, c: core.ChunkHash, dirname: str | Path):
        """Export one chunk's layer into a directory.

        Args:
            c: The chunk.
            dirname: The directory to write to.

        Returns:
            The written file, or None if the chunk has no layer.
        """
        if not (layer := self.get_layer(c)):
            return None

        name = self.get_layer_file_name(layer)
        file = f"{dirname}/{name}"
        layer.Export(file)
        return Path(file)

    def save_layers(self, path: str | Path) -> dict[str, str]:
        """Export every chunk layer into a directory.

        Args:
            path: The directory to write to.

        Returns:
            {chunk: written file}, empty if `path` is not a directory.
        """
        p = Path(path)
        if not p.is_dir():
            logger.warning("Can't save layers. Path is not a directory %s", path)
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

    def get_layer(self, c: core.ChunkHash) -> Sdf.Layer | None:
        """Get the layer associated with a chunk.

        Args:
            c (ChunkHash): The chunk whose layer to get

        Returns:
            Sdf.Layer | None: The chunk's layer, or None if it has none
        """
        return self.chunk_layers.get(c)

    def get_chunk_target(self, c: core.ChunkHash) -> Sdf.Layer | None:
        """The real layer this chunk's content last resolved against.

        That is the session layer by default, or an Edit-Target-chosen layer, as tracked by
        get_or_create_layer. Export tooling uses it to group a chunk's own bookkeeping prim
        (always in the session layer) with whichever real layer its driven content nests in.

        Args:
            c: The chunk.

        Returns:
            The layer, or None if the chunk has none yet.
        """
        return self._chunk_target.get(c)

    def target_of_layer(self, layer: Sdf.Layer) -> Sdf.Layer | None:
        """The real layer a chunk layer's content lands in (get_chunk_target, by layer).

        Args:
            layer: A chunk's own layer.

        Returns:
            Its chunk's target, or None if no tracked chunk owns `layer`.
        """
        for c, owned in self.chunk_layers.items():
            if owned is not None and owned.identifier == layer.identifier:
                return self._chunk_target.get(c)
        return None

    def defer_placement(self) -> bool:
        """Hold chunk layers made from now on in one loading layer, until the graph is wired.

        A load restores each chunk's inputs before it restores the wiring between chunks, and
        restoring an input asks for the chunk's layer. Placed then, every chunk looks like a
        graph of its own: it got a graph layer of its own on the stage, and the wiring moved
        it into the shared one a moment later and threw that graph layer away. Each of those
        sublayer edits recomposes the stage -- a fifth of opening a 70-chunk network.

        The held layers stay on the stage, in one loading layer, so a held layer behaves as a
        placed one does. Off the stage it would not: Usd.EditContext raises for a layer
        outside the stage's layer stack, and chunk_notice declines to cook a chunk whose
        layer is not in it yet. A first version kept them off the stage, and that skipped a
        cook the load used to run -- the same outcome, reached by accident, that a vector's
        premature cook no longer has in the first place (see _set_children_blocked).

        Returns:
            bool: True if this call turned deferral on. Only that caller calls
            place_deferred_layers; a nested caller gets False and leaves it to the outer one.
        """
        if self._deferred_placement is not None:
            return False
        self._deferred_placement = {}
        return True

    @staticmethod
    @contextlib.contextmanager
    def _internal_sublayer_edit():
        """Mark sublayer edits as this manager's own bookkeeping, as get_or_create_layer does.

        A layer moved in or out here holds chunk results, never an Edit Target, so the stage
        sync has nothing to react to -- see _internal_layer_write_active.
        """
        prev_w = chunk_notice._usd_write_active
        prev_internal = chunk_notice._internal_layer_write_active
        chunk_notice._usd_write_active = True
        chunk_notice._internal_layer_write_active = True
        try:
            yield
        finally:
            chunk_notice._usd_write_active = prev_w
            chunk_notice._internal_layer_write_active = prev_internal

    def _holding_layer(self) -> Sdf.Layer:
        """The layer defer_placement holds new chunk layers in, made on first use."""
        if self._loading_layer is None:
            self._loading_layer = Sdf.Layer.CreateAnonymous("loading")
            self.internal_layer_ids.add(self._loading_layer.identifier)
            with self._internal_sublayer_edit():
                self.stage.GetSessionLayer().subLayerPaths.insert(0, self._loading_layer.identifier)
        return self._loading_layer

    def end_deferral(self) -> list[core.ChunkCommon]:
        """Stop deferring, and return the chunks whose layer is still waiting to be placed.

        The loading layer comes off the stage, so the returned chunks' layers are off it
        until they are placed. The caller places them, in the order it wants: the order
        layers go onto a graph layer breaks ties between chunks at the same depth, and a
        load keeps the order it had before placement was deferred.
        """
        pending, self._deferred_placement = self._deferred_placement, None
        if (loading := self._loading_layer) is not None:
            self._loading_layer = None
            self.internal_layer_ids.discard(loading.identifier)
            with self._internal_sublayer_edit():
                session = self.stage.GetSessionLayer() if self.stage else None
                if session and loading.identifier in session.subLayerPaths:
                    session.subLayerPaths.remove(loading.identifier)
                loading.subLayerPaths = []
        return [c for ref in (pending or {}).values() if (c := ref()) is not None]

    def place_deferred_layers(self) -> None:
        """Put every layer held back by defer_placement on the stage, and stop deferring.

        Each graph layer's sublayer list is written once, for all its chunks: every write
        to a list on the stage recomposes the whole layer stack, so placing a layer per
        write made a network of n chunks cost n recomposes of up to n layers each.
        """
        held = self.end_deferral()
        with self._batched_sublayers():
            for c in held:
                self.get_or_create_layer(c)

    @contextlib.contextmanager
    def _batched_sublayers(self):
        """Collect graph-layer sublayer edits made inside, and write each list once at the end.

        Only graph layers are batched, those there already and those made meanwhile: they are
        this manager's own and hold no layer offsets, which a whole-list write would not
        keep. The layers they sit under are edited as before.
        """
        if self._sublayer_batch is not None:
            yield
            return
        self._sublayer_batch = {}
        self._batchable = {g.identifier for g in self.graph_layers.values()}
        try:
            yield
        finally:
            batch, self._sublayer_batch = self._sublayer_batch, None
            self._batchable = set()
            with self._internal_sublayer_edit():
                for layer, paths in batch.values():
                    if list(layer.subLayerPaths) != paths:
                        layer.subLayerPaths = paths

    def _sublayers(self, layer: Sdf.Layer) -> list[str]:
        """layer's sublayer identifiers: the batched list of a graph layer being batched, or a
        copy read off the layer."""
        batch = self._sublayer_batch
        if batch is None or layer.identifier not in self._batchable:
            return list(layer.subLayerPaths)
        if (entry := batch.get(layer.identifier)) is None:
            entry = batch[layer.identifier] = (layer, list(layer.subLayerPaths))
        return entry[1]

    def _insert_sublayer(self, layer: Sdf.Layer, index: int, identifier: str) -> None:
        """Insert into layer's sublayers, through the batch when layer is being batched."""
        if self._sublayer_batch is not None and layer.identifier in self._batchable:
            self._sublayers(layer).insert(index, identifier)
        else:
            layer.subLayerPaths.insert(index, identifier)

    def _remove_sublayer(self, layer: Sdf.Layer, identifier: str) -> None:
        """Remove from layer's sublayers, through the batch when layer is being batched."""
        if self._sublayer_batch is not None and layer.identifier in self._batchable:
            self._sublayers(layer).remove(identifier)
        else:
            layer.subLayerPaths.remove(identifier)

    def get_or_create_layer(self, c: core.ChunkCommon) -> Sdf.Layer | None:
        """Get a chunk's layer as get_layer() does, creating it if it does not exist yet.

        Args:
            c (ChunkCommon): The chunk whose layer to get

        Returns:
            Sdf.Layer | None: The layer, or None when there is no stage or the chunk has been
            deleted
        """
        if not self.stage:
            return

        # A deleted chunk never gets a layer back -- see _chunk_deleted for why re-creating
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

        # While a load is still wiring its chunks, the layer is made in the loading layer;
        # place_deferred_layers puts it where the finished wiring says. See defer_placement.
        if self._deferred_placement is not None:
            self._deferred_placement[c.get_hash()] = weakref.ref(c)
            if layer := self.get_layer(c):
                return layer
            # A chunk layer moving into the stack, like the placed ones below: without the
            # guard the stage sync took each hold for a real layer stack change and dropped
            # what it remembers of the scene, every chunk.
            with self._internal_sublayer_edit():
                return self.add_layer(self._holding_layer(), c)

        # Resolve where this chunk's content should nest: the user's chosen Edit Target,
        # or the session layer by default -- the same resolution chunk-prim redirect
        # opinions already follow (chunk_utils.get_edit_layer). CONTEXT chunks (Tab-like
        # containers) are structural scaffolding, not content -- they always stay pinned
        # to the session layer regardless of Edit Target, same as chunk-prim bookkeeping;
        # only a regular content-producing chunk's own driven data should move.
        if isinstance(c, core.ChunkBase) and c.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            target = self.stage.GetSessionLayer()
        else:
            # Pass c as the owner so the target is resolved from c's own exec-chain position
            # (nearest upstream Edit Target), not from whichever chunk executed last.
            owner_chunk = c if isinstance(c, core.ChunkBase) else None
            target = chunk_utils.get_edit_layer(self.stage, owner_chunk)
            # No Edit Target upstream: the cook belongs with the layer that defines the chunk,
            # just under it, not in the session layer over everything -- a layout procedural
            # must not beat what lighting authors by hand.
            if target == self.stage.GetSessionLayer() and (home := self._home_layer(c)):
                target = self.cook_layer_for(home) or target
        self._chunk_target[c.get_hash()] = target
        self._placed[c.get_hash()] = weakref.ref(c)

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
        # -- seen live in CI as a genuine, if intermittent, "Duplicate item ... not allowed for
        # field 'subLayers'" Sdf error on the exact same graph layer being inserted twice.
        prev_w = chunk_notice._usd_write_active
        prev_internal = chunk_notice._internal_layer_write_active
        chunk_notice._usd_write_active = True
        # Everything sublayered below is a graph layer or a chunk's own procedural layer, so
        # none of it can change which Edit Target a chunk resolves to -- see the flag's own
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
                if self._sublayer_batch is not None:
                    self._batchable.add(parent.identifier)
                # Insert at the top so this graph's layers win over other sublayers already on
                # the target -- e.g. an RBD bake must beat the prim's existing xformOp:transform.
                # Checked rather than unconditional: a freshly minted anon layer's identifier
                # can still collide with one already sitting in target.subLayerPaths -- e.g. a
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
                    # an upstream Edit Target chunk's selection changed) -- reparent rather than
                    # leave it sublayered under a now-stale target.
                    if parent.identifier in prev_target.subLayerPaths:
                        prev_target.subLayerPaths.remove(parent.identifier)
                    self._graph_layer_target[graph_key] = target
                if parent.identifier not in target.subLayerPaths:
                    target.subLayerPaths.insert(0, parent.identifier)

            def insert_index() -> int:
                # The sort_key is used to determine the order of the chunk sublayers
                count = len(self._sublayers(parent))
                index = chunk_scheduler.layer_sort_key(c.get_dag_node())
                if index > count - 1:
                    index = count
                # Reverse the order
                return count - index

            layer = self.get_layer(c)
            chunk_hash = c.get_hash()

            if not layer:
                if self.is_muted(c):
                    return
                layer = self.add_layer(parent, c, insert_index())
            elif self._confirmed_in.get(chunk_hash) != parent.identifier:
                # Only when this manager has not already seen the layer in this parent: every
                # read of subLayerPaths copies the whole list out of C++, a graph layer holds a
                # layer per chunk, and this runs on every get_edit_context -- so checking each
                # time made every call cost the size of the graph. Only this manager moves a
                # chunk layer between graph layers, and each move goes through here.
                if layer.identifier not in self._sublayers(parent):
                    self._insert_sublayer(parent, insert_index(), layer.identifier)
            self._confirmed_in[chunk_hash] = parent.identifier

            # If a chunk connection changed after the chunk layer was created, the layer may have been
            # added to a new graph. This can be cleaned up as to not sublayer the chunk layer twice.
            # Only inspect pl for emptiness right after we've just removed its child above -- never as
            # a blanket sweep over every tracked graph layer, since a graph layer can legitimately be
            # momentarily empty while it's still being constructed (its own first leaf not yet added).
            #
            # Looks only at the graph layers' own direct sublayers, not the whole composed
            # hierarchy iter_chunk_layers() walks. A chunk leaf layer is only ever inserted as a
            # direct sublayer of a graph layer (add_layer), so those are the only pairs that could
            # ever match here -- the deeper levels of that walk are a chunk layer's own imports
            # (a Sublayer chunk's files), which are never this layer. The full walk resolves every
            # sublayer path through Sdf.Find on every call, and this runs on every single
            # get_edit_context: it was ~30% of the cost of opening a saved graph.
            #
            # And only when this chunk's graph_key differs from the one its layer was last
            # placed under: the layer can only be sitting in another graph layer if this
            # function put it there, under the key it had then. Every subLayerPaths read is a
            # C++ list copy, and this loop was two thirds of the call on a warm cache, on a
            # call made ~50 times per chunk over a scene open.
            chunk_hash = c.get_hash()
            moved = self._chunk_graph_key.get(chunk_hash) != graph_key
            self._chunk_graph_key[chunk_hash] = graph_key
            for pl in list(self.graph_layers.values()) if moved else ():
                if pl.identifier != parent.identifier and layer.identifier in self._sublayers(pl):
                    self._remove_sublayer(pl, layer.identifier)
                    if len(self._sublayers(pl)) == 0:
                        for gkey, g_layer in list(self.graph_layers.items()):
                            if g_layer.identifier != pl.identifier:
                                continue
                            gtarget = self._graph_layer_target.pop(gkey, None)
                            if gtarget and pl.identifier in gtarget.subLayerPaths:
                                gtarget.subLayerPaths.remove(pl.identifier)
                            self.graph_layers.pop(gkey, None)
                            self.internal_layer_ids.discard(pl.identifier)
                            # Its address can be handed to a new layer, identifier and all.
                            self._confirmed_in = {
                                h: ident for h, ident in self._confirmed_in.items()
                                if ident != pl.identifier
                            }
                            break
        finally:
            chunk_notice._usd_write_active = prev_w
            chunk_notice._internal_layer_write_active = prev_internal

        return layer

    def _home_layer(self, c: core.ChunkCommon) -> Sdf.Layer | None:
        """The file layer whose cook sublayer c cooks into, or None.

        For a chunk the stage's own layer stack defines, the layer whose turn it cooks in
        (chunk_utils.chunk_pass): a department or an asset layer -- layout.usda, geo.usda,
        mtl.usda -- or the root.

        For a chunk an arc brings in -- a house referenced, payloaded, inherited or switched
        in by a variant -- the layer of the stage's stack that imports it (the outermost
        arc's, chunk_utils.arc_source), never the asset's own: an asset is shared by every
        shot that uses it, so what one shot cooks of it is that shot's. Its cook sublayer is
        in the stage's namespace, so the result needs no mapping; it is stronger than the
        asset (local beats the arc) and weaker than the importing layer's own opinions, and
        it goes wherever that layer goes.

        An anonymous layer is None: it has no file to keep a cook sublayer beside.
        """
        if not isinstance(c, core.ChunkBase):
            return None
        prim = self.stage.GetPrimAtPath(Sdf.Path(chunk_utils.get_chunk_prim_path(c)))
        if not (prim and prim.IsValid()):
            return None
        home = chunk_utils.chunk_pass(prim)
        if home is None and (source := chunk_utils.arc_source(prim)) is not None:
            home = source[1]
        return None if home is None or home.anonymous else home

    def cook_layer_for(self, home: Sdf.Layer) -> Sdf.Layer | None:
        """The layer cooks of chunks defined in home nest under: home's cook sublayer.

        layout.usda lists layout.cook.usda (chunk_layers.find_cook_sublayer), so the cook
        sits just under the layer that defines it: stronger layers -- lighting's, authored or
        cooked -- beat it, and so do home's own hand edits. The graph layers go under it in
        memory; neither file is written, so both may be locked.

        Opt-in by the link: a home listing none is never given one here, since that would
        dirty a file merely by opening it -- its chunks cook into the session layer as
        before. Whoever owns the file adds the link (chunk_layers.add_cook_sublayer).

        Args:
            home: A file layer that defines chunks.

        Returns:
            The cook layer, internal to the chunk system (is_chunk_internal_layer), or None
            when home lists none.
        """
        if (cook := self._cook_layers.get(home.identifier)) is not None:
            return cook
        if (cook := chunk_layers.find_cook_sublayer(home)) is None:
            return None
        # From here on the live cooks are the result: the bake saved in the file was the
        # last one, and left in place it would compose under them -- a prim a cook no
        # longer makes would linger. Cleared in memory only; save_cook_layers writes the
        # next bake. What the file records about itself stays.
        with self._internal_sublayer_edit():
            data = dict(cook.customLayerData)
            cook.Clear()
            cook.customLayerData = data
        self.internal_layer_ids.add(cook.identifier)
        self._cook_layers[home.identifier] = cook
        return cook

    def save_cook_layers(self) -> list[str]:
        """Write every cook layer's bake to its file: what its chunks cooked, flattened.

        At runtime a cook layer only lists the layers its chunks cook into, all in memory.
        The bake is that stack flattened (UsdUtils.FlattenLayerStack, honouring the stage's
        mutes) and written beside the layer that lists it, so a consumer without usd_chunks
        sees the result. The layer in memory keeps listing the live ones; it is exported,
        never saved.

        Each chunk the bake holds is stamped in the file's customLayerData under
        usdchunks:cooks, by its prim path in the file's own namespace: its module hash, a
        hash of its inputs, and whether its result is partial (muted, blocked outside the
        active view, or never cooked). A bake whose stamps no longer match is stale.

        Returns:
            The files written.
        """
        self.deactivate_stale_bakes()
        written = []
        for cook in self._cook_layers.values():
            if cook.anonymous or not self.stage:
                continue
            reader = Usd.Stage.Open(cook, Usd.Stage.LoadNone)
            if muted := [i for i in self.stage.GetMutedLayers() if i != cook.identifier]:
                reader.MuteAndUnmuteLayers(muted, [])
            flat = UsdUtils.FlattenLayerStack(reader)
            flat.customLayerData = {
                **dict(cook.customLayerData),
                "usdchunks:derived": True,
                "usdchunks:cooks": self._bake_stamps(cook),
            }
            flat.Export(cook.realPath)
            written.append(cook.realPath)
        return written

    def _after_cook(self, c: core.ChunkCommon) -> None:
        """Note which prims a cook started or stopped defining, then -- once the solve it was
        part of has drained -- hide what its asset's bake left (deactivate_stale_bakes).

        Only a change in what the live cooks define can change what only a bake defines, so
        a cook that defines the same prims as last time (a drag: new values, same prims)
        adds nothing to check.
        """
        chunk_hash = c.get_hash()
        if chunk_hash not in self._placed or (layer := self.chunk_layers.get(chunk_hash)) is None:
            return
        defined = chunk_layers.defined_paths(layer)
        changed = defined ^ self._chunk_defs.get(chunk_hash, frozenset())
        self._chunk_defs[chunk_hash] = defined
        if changed:
            self._dirty_paths |= changed
        if not chunk_cook.defer_to_batch_end(
                ("deactivate_stale_bakes", id(self)), self.deactivate_stale_bakes):
            self.deactivate_stale_bakes()

    def deactivate_stale_bakes(self) -> None:
        """Hide the prims an asset's published bake makes that this stage's live cooks don't.

        A shot cooks an asset's chunks into its own cook layer and never touches the asset,
        so the bake the asset was published with still composes underneath -- with every prim
        that network made then. A prim the live cook no longer makes would linger. So for
        each copy of an asset whose chunks have all cooked live here, every prim that only
        the asset's bake defines gets an `over` with `active = false`, the topmost one of a
        subtree only, in a layer under this stage's cook layer. It is written into the shot's
        bake with the rest (save_cook_layers), so a consumer without usd_chunks sees the same.

        A copy whose chunks have not all cooked -- disabled, blocked outside the active view,
        never cooked -- keeps its bake whole: hiding it would leave the bare asset.

        Incremental, for large assets. A copy that turns live is walked once in full. After
        that only the bake subtrees at paths whose definition changed (_after_cook) are
        walked again, and only the overs that change are written, so a prim that stays
        hidden is never recomposed.
        """
        if not (self.initialized and self.stage):
            return
        copies = self._live_copies()
        dirty, self._dirty_paths = self._dirty_paths, set()
        drop: dict[tuple, set] = {}
        walk: dict[tuple, list] = {}
        for key, state in self._copies.items():
            if key not in copies:
                drop[key] = set(state["hidden"])
        for key, (cook, bake, map_fn, live) in copies.items():
            state = self._copies.setdefault(
                key, {"cook": cook, "bake": bake, "map": map_fn, "live": False, "hidden": set()})
            hidden = state["hidden"]
            if not live:
                drop[key] = set(hidden)
            elif not state["live"]:
                drop[key] = set(hidden)
                walk[key] = [Sdf.Path.absoluteRootPath]
            else:
                drop[key], walk[key] = self._dirty_roots(hidden, dirty, map_fn, bake)
            state["live"] = live
        with self._internal_sublayer_edit():
            with Sdf.ChangeBlock():
                for key, paths in drop.items():
                    if not paths:
                        continue
                    state = self._copies[key]
                    state["hidden"] -= paths
                    if (entry := self._deactivations.get(state["cook"].identifier)) is not None:
                        for path in paths:
                            if (spec := entry[1].GetPrimAtPath(path)) is not None:
                                spec.ClearInfo("active")
                        entry[1].RemoveInertSceneDescription()
            for key in list(drop):
                if key not in copies:
                    del self._copies[key]
            found = {key: self._only_in_bake(self._copies[key], roots)
                     for key, roots in walk.items() if roots}
            with Sdf.ChangeBlock():
                for key, paths in found.items():
                    if not paths:
                        continue
                    state = self._copies[key]
                    state["hidden"] |= paths
                    overs = self._overs_layer(state["cook"])
                    for path in paths:
                        Sdf.CreatePrimInLayer(overs, path).active = False

    def _live_copies(self) -> dict[tuple, tuple]:
        """{copy: (cook layer, asset's bake, arc map, all its chunks live)} for every copy of
        an asset with a published bake whose chunks cook into one of this stage's cook layers.
        A copy is keyed by the stage path its arc is introduced at."""
        copies: dict[tuple, list] = {}
        cooks = {cook.identifier for cook in self._cook_layers.values()}
        for chunk_hash, ref in list(self._placed.items()):
            if (c := ref()) is None or chunk_hash in self._retired_chunks:
                continue
            # A chunk with nothing to run (an Output) neither replaces a bake nor holds one up.
            if c._module_hook("execute") is None:
                continue
            cook = self._chunk_target.get(chunk_hash)
            if cook is None or cook.identifier not in cooks:
                continue
            prim = self.stage.GetPrimAtPath(Sdf.Path(chunk_utils.get_chunk_prim_path(c)))
            if not (prim and prim.IsValid()) or (node := chunk_utils.arc_def_node(prim)) is None:
                continue
            home = node.layerStack.identifier.rootLayer
            if (bake := chunk_layers.find_cook_sublayer(home)) is None:
                continue
            copy = chunk_utils.arc_source(prim)
            key = (cook.identifier, bake.identifier, str(copy[0]) if copy else "")
            entry = copies.setdefault(key, [cook, bake, node.mapToRoot, True])
            if self.is_muted(chunk_hash) or c.is_blocked() or chunk_cook.is_cook_stale(c):
                entry[3] = False
        return {key: tuple(entry) for key, entry in copies.items()}

    @staticmethod
    def _dirty_roots(hidden: set, dirty: set, map_fn, bake: Sdf.Layer) -> tuple[set, list]:
        """For a live copy: the hidden paths a change in definitions at dirty touches, and
        the stage paths to walk again from.

        A changed path p decides again for itself and what is below it, so hidden paths at
        or below it are dropped and p is walked. A hidden path above it kept its children
        out of the walks so far -- they were not composed -- so it is dropped too, and the
        walk starts there instead. A path the bake has no prim at holds nothing to hide.
        """
        drop: set = set()
        roots: set = set()
        for path in sorted(dirty, key=lambda p: p.pathElementCount):
            source = map_fn.MapTargetToSource(path)
            if source.isEmpty or bake.GetPrimAtPath(source) is None:
                continue
            above = [h for h in hidden if path.HasPrefix(h) and h != path]
            drop.update(h for h in hidden if h.HasPrefix(path))
            drop.update(above)
            roots.add(min(above, key=lambda p: p.pathElementCount) if above else path)
        # A root inside another root is walked with it.
        roots = {r for r in roots if not any(r != o and r.HasPrefix(o) for o in roots)}
        return drop, sorted(roots)

    def _only_in_bake(self, state: dict, roots: list) -> set:
        """Stage paths of the topmost prims under roots that state's bake defines and nothing
        else on the stage does -- skipping what is hidden already."""
        bake, map_fn, hidden = state["bake"], state["map"], state["hidden"]
        found = set()

        def visit(spec):
            path = map_fn.MapSourceToTarget(spec.path)
            if path.isEmpty or path in hidden:
                return
            prim = self.stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                defs = [s.layer for s in prim.GetPrimStack() if s.specifier == Sdf.SpecifierDef]
                if defs and all(layer == bake for layer in defs):
                    found.add(path)
                    return
            for child in spec.nameChildren:
                visit(child)

        for root in roots:
            if root == Sdf.Path.absoluteRootPath:
                for spec in bake.rootPrims:
                    visit(spec)
            elif (spec := bake.GetPrimAtPath(map_fn.MapTargetToSource(root))) is not None:
                visit(spec)
        return found

    def _overs_layer(self, cook: Sdf.Layer) -> Sdf.Layer:
        """The layer of deactivating overs under cook, made on first use."""
        if (entry := self._deactivations.get(cook.identifier)) is None:
            entry = (cook, Sdf.Layer.CreateAnonymous("deactivate"))
            self.internal_layer_ids.add(entry[1].identifier)
            self._deactivations[cook.identifier] = entry
            cook.subLayerPaths.append(entry[1].identifier)
        return entry[1]

    def _drop_deactivations(self) -> None:
        """Take every layer of deactivating overs out of its cook layer, and forget them."""
        with self._internal_sublayer_edit():
            for cook, overs in getattr(self, "_deactivations", {}).values():
                if overs.identifier in cook.subLayerPaths:
                    cook.subLayerPaths.remove(overs.identifier)
                self.internal_layer_ids.discard(overs.identifier)
        self._deactivations = {}
        self._copies: dict[tuple, dict] = {}
        self._chunk_defs: dict[core.ChunkHash, frozenset] = {}
        self._dirty_paths: set = set()

    def _bake_stamps(self, cook: Sdf.Layer) -> dict:
        """{chunk prim path in cook's namespace: stamp} for the chunks cook holds."""
        stamps = {}
        for chunk_hash, ref in self._placed.items():
            if (c := ref()) is None or chunk_hash in self._retired_chunks:
                continue
            # A chunk with nothing to run (an Output) adds nothing to a bake.
            if c._module_hook("execute") is None:
                continue
            if self._chunk_target.get(chunk_hash) != cook:
                continue
            module = c.get_chunk_module()
            stamps[chunk_utils.get_chunk_prim_path(c)] = {
                "module": (module.source_hash if module is not None else None) or "",
                "inputs": chunk_utils.chunk_inputs_hash(
                    self.stage.GetPrimAtPath(Sdf.Path(chunk_utils.get_chunk_prim_path(c)))),
                "partial": bool(self.is_muted(chunk_hash) or c.is_blocked()
                                or chunk_cook.is_cook_stale(c)),
            }
        return stamps

    def is_muted(self, c: core.ChunkHash) -> bool:
        """Check if a chunk's layer is muted.

        A chunk layer can be muted stage-scoped (the disable path) or at the Sdf level (node
        graph view slots); either counts.

        Args:
            c (ChunkHash): The chunk whose layer to check

        Returns:
            bool: True when the layer is muted, False otherwise
        """
        layer = self.chunk_layers.get(c)
        if not (self.stage and layer):
            return False
        # A chunk layer can be muted stage-scoped (disable path) or Sdf-level (node graph
        # view slots); treat either as muted.
        return self.stage.IsLayerMuted(layer.identifier) or layer.IsMuted()

    def add_layer(self, target_layer: Sdf.Layer, c: core.ChunkCommon, index: int = 0) -> Sdf.Layer:
        """Add a layer to the stage and add a mapping from chunk c to the new layer.

        Args:
            target_layer (Sdf.Layer): The target layer onto which we append the new layer
            c (ChunkUsdLayerSupport): The chunk that can modify the new layer. The new layer's identifier
                will be created from the chunks get_layer_identifier()
            index (int): Where in target_layer's subLayerPaths to insert the new layer

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
        self._insert_sublayer(target_layer, index, layer.identifier)
        # An empty layer holds no result, so the chunk must cook before recook_for_view can
        # count on its layer. Covers a load, which makes every layer afresh for chunk objects
        # that may have cooked into the old ones.
        chunk_cook.mark_cook_stale(c)

        # We must make sure to delete the layer of the chunk AFTER all other notices are sent.
        # For this reason, we can't rely on callbacks. Instead we can register a finalizer, so we delete
        # the layer once the chunk is actually deleted
        weakref.finalize(c, self._chunk_deleted, h)

        return layer

    def clear_layer(self, layer_id: str | core.ChunkBase):
        """Clear a layer's contents.

        Args:
            layer_id (str | ChunkBase): The layer identifier, or the chunk whose layer to clear
        """
        if isinstance(layer_id, str):
            layer = Sdf.Layer.Find(layer_id)
        elif _layer_id := self.get_layer(layer_id):
            layer = Sdf.Layer.Find(_layer_id.identifier)
        else:
            return
        layer.Clear()

    def _edit_target_changed(self, notice: Usd.Notice.StageEditTargetChanged, sender):
        """Adopt the stage whose edit target moved -- but only when there is none yet.

        This listens globally, so it hears every stage in the process, and it adopts one
        only when it has none -- the case it exists for is chunks loaded before the host
        had a stage. Adopting whichever stage spoke last would be wrong for every other
        case: a second stage in the same process (a test exporting an asset, a tool
        opening a scratch stage) would pull every chunk's layer under THAT stage's session,
        and the chunks' opinions would compose over a stage they were never meant for.
        A stage the host replaces reaches this manager through initialize()
        (chunk_stage.reload_for_stage), so an occupied slot never needs a notice to move.
        """
        stage = notice.GetStage()
        if stage and not self.stage:
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

    def iter_chunk_layers(self) -> Generator[tuple[Sdf.Layer, Sdf.Layer], None, None]:
        """Every tracked graph layer and chunk layer, with the layer it sits under.

        Covers every graph layer regardless of which real layer (the session layer, or an
        Edit-Target-chosen sublayer) it is attached under.

        Yields:
            (parent, sublayer) pairs.
        """
        # Don't pre-mark g_layer as visited here -- _iter_sublayers does that itself as
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
        # a GC finalizer (add_layer), but a lingering reference -- a node/widget or a reference
        # cycle -- can defer collection, leaving the layer and any baked edits (e.g. an RBD
        # sim's time samples) composed into the scene. Idempotent with the finalizer.
        get_hash = getattr(sender, "get_hash", None)
        if callable(get_hash):
            self._chunk_deleted(get_hash())

    def _chunk_deleted(self, chunk_hash):
        # Retire the chunk before touching any layer. The subLayerPaths edits below fire
        # StageContentsChanged, and a chunk script listening for it (layer's and
        # edit_target's menu-refresh callbacks) re-executes off that notice -- landing back in
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
        self._chunk_graph_key.pop(chunk_hash, None)
        self._confirmed_in.pop(chunk_hash, None)
        if not (layer := self.get_layer(chunk_hash)):
            return
        self.chunk_layers.pop(chunk_hash, None)
        if self._held_deletes is not None:
            # Held, not dropped: the list keeps the layer alive, so its address -- and with it
            # its identifier -- cannot be recycled before holding_deletes takes it off.
            self._held_deletes.append(layer)
        else:
            self._remove_layers([layer])

    @contextlib.contextmanager
    def holding_deletes(self):
        """Take the layers of chunks deleted inside this block off the stage together, at its end.

        Each layer taken off on its own recomposes the whole stage, twice with the mute, so
        deleting every chunk of a big network one by one cost the square of its size -- most
        of the time closing a 900-chunk scene took. Nested blocks join the outermost.
        """
        if self._held_deletes is not None:
            yield
            return
        self._held_deletes = []
        try:
            yield
        finally:
            held, self._held_deletes = self._held_deletes, None
            if held:
                self._remove_layers(held)

    def _remove_layers(self, layers: list) -> None:
        """Take deleted chunks' layers off the stage: one mute, one write per parent, one unmute."""
        ids = {layer.identifier for layer in layers}

        # Muted first, as a disable would, and outside the guard below: a mute reports each
        # property it takes away, so what the layer held on other chunk prims -- an Override
        # Chunk Prim's `over`s -- goes back through the stage mirror like any edit. Dropping
        # it from subLayerPaths reports only the root, which names nothing.
        muted_here = [
            layer.identifier for layer in layers
            if self.stage and not layer.empty and not self.stage.IsLayerMuted(layer.identifier)
        ]
        if muted_here:
            self.stage.MuteAndUnmuteLayers(muted_here, [])

        # subLayerPaths edits below are plain Sdf writes that fire StageContentsChanged
        # like any other stage edit. Without this guard, each one re-enters
        # ChunkStageSync._on_contents_changed -> load_chunks mid-cascade (e.g. while
        # request_delete() is still working through a context's other children),
        # causing a full stage rescan per chunk deleted instead of once.
        prev_w = chunk_notice._usd_write_active
        chunk_notice._usd_write_active = True
        try:
            layer_hierarchy: dict[Sdf.Layer, Sdf.Layer] = {}
            parents: list[Sdf.Layer] = []
            for parent, sublayer in self.iter_chunk_layers():
                layer_hierarchy[sublayer] = parent
                if sublayer.identifier in ids and parent not in parents:
                    parents.append(parent)
            for parent in parents:
                sublayer_paths = _without_sublayers(parent, ids)

                if len(sublayer_paths) == 0 and (p := layer_hierarchy.get(parent)):
                    if parent.identifier in p.subLayerPaths:
                        p.subLayerPaths.remove(parent.identifier)

                parent.subLayerPaths = sublayer_paths  # type: ignore
            if muted_here:
                # Out of the stage's muted set too: the identifier embeds an address, and a
                # recycled one would bring some later layer up muted.
                self.stage.MuteAndUnmuteLayers([], muted_here)
        finally:
            chunk_notice._usd_write_active = prev_w
        # Dropped with the mapping: the identifier embeds an address, and addresses are
        # recycled, so a leftover entry would eventually claim an unrelated layer.
        self.internal_layer_ids -= ids

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
            # this handler runs on EVERY state change (incl. selection) -- without the is_blocked
            # guard, selecting an out-of-view node would un-mute it and show its output.
            self.stage.UnmuteLayer(layer.identifier)
