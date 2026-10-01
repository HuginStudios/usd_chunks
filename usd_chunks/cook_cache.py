"""Cooked results kept on disk, for chunk modules that declare `cache_cook=True`.

A cook starts from an empty layer and authors its whole result, every time -- which is what
makes a chunk's layer honest, and what makes an expensive chunk expensive on every open. A
STEP tessellation, a URDF's meshes, a decimation of a four-million-triangle mesh: the same
inputs give the same layer, so the second time there is nothing to compute, only to read.

A module opts in, because only the module can vouch for what its result depends on:

``` py
def config():
    return chunk.DefaultConfig(cache_cook=True)

def cache_key(node):            # optional: inputs the framework cannot see
    return [mesh_signature(p) for p in input_meshes(node)]
```

The key is everything the module vouched for: its own source (ChunkModule.source_hash), the
framework's and its sibling modules' files, every param value except the ones the cook writes
(labels and OUTPUT params), the size and mtime of any file a param names, the stage's up axis
and metersPerUnit, and what the
module's `cache_key(node)` returns -- the hook is for inputs that are not params, like the
geometry a Poly Reduce reads from upstream, or the meshes a URDF names. A hook returning None
means "cannot say this time", and the chunk cooks.

An entry is the cooked layer (`.usdc`) and the params the cook set, with what it hid or
showed. Only a cook that changed nothing but those is stored: one that writes its own inputs,
or adds params, depends on more than its key can see. Nor is a fast one -- reading an entry
back costs time too, and a cook quicker than that is better left a cook.

Each chunk of such a module gets a `cache_result` checkbox (ChunkModule.create), on by
default, to opt one node out. The environment decides for all of them:

- `USD_CHUNKS_COOK_CACHE` -- unset or `1`: on. `0`: off everywhere. `refresh`: cook every
  time and rewrite the entries.
- `USD_CHUNKS_COOK_CACHE_DIR` -- where entries live. Defaults to
  `%LOCALAPPDATA%/usd_chunks/cook_cache`, beside the render-settings cache.
- `USD_CHUNKS_COOK_CACHE_MAX_GB` -- the size cap, default 2.

Every write prunes: entries unread for 30 days go, then the least recently read until the
folder is under the cap. Reading an entry counts as using it.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Any

from pxr import Sdf, Usd, UsdGeom

from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

#: The switch, and where entries live. See the module docstring.
ENV = "USD_CHUNKS_COOK_CACHE"
ENV_DIR = "USD_CHUNKS_COOK_CACHE_DIR"
ENV_MAX_GB = "USD_CHUNKS_COOK_CACHE_MAX_GB"

_DEFAULT_MAX_GB = 2.0

#: The param ChunkModule.create adds to every chunk of a cache_cook module.
TOGGLE = "cache_result"

#: Bumped when an entry's layout or the key's contents change, so old entries miss.
_FORMAT = 2

#: A cook quicker than this is not stored: reading an entry back is not free either.
_MIN_COOK_S = 0.05

_MAX_AGE_S = 30 * 24 * 3600

#: Params a cook writes rather than reads -- a readout, a count for others to read.
_OUTPUT_TYPES = (ChunkFlags.ParamType.LABEL,)

#: What a cook may do to a param's or group's presentation, and a read-back must repeat.
_STATES = (ChunkFlags.State.HIDDEN, ChunkFlags.State.DISABLED)


def mode() -> str:
    """"off", "refresh" or "on", from USD_CHUNKS_COOK_CACHE."""
    value = os.environ.get(ENV, "").strip().lower()
    if value in ("0", "off", "false", "no"):
        return "off"
    if value == "refresh":
        return "refresh"
    return "on"


def cache_dir() -> Path:
    """Where entries live: USD_CHUNKS_COOK_CACHE_DIR, else %LOCALAPPDATA%/usd_chunks/cook_cache."""
    if custom := os.environ.get(ENV_DIR):
        return Path(custom)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return Path(base) / "usd_chunks" / "cook_cache"


def lookup(chunk) -> CookEntry | None:
    """The cache entry for this chunk's next cook, or None when its cook is not cached.

    None when the cache is off, the module did not declare cache_cook, the node's
    cache_result is unticked, or its cache_key() hook could not say.
    """
    if mode() == "off":
        return None
    config = chunk.get_config()
    if not (config and config.cache_cook):
        return None
    toggle = chunk.get_param(TOGGLE)
    if toggle is not None and not toggle.value:
        return None
    module = chunk.get_chunk_module()
    if module is None or not module.source_hash:
        return None
    try:
        key = _key(chunk, module)
    except Exception:  # noqa: BLE001 - a key that cannot be made is a cook, not a failure
        logger.exception("cook cache: could not key %s; cooking it", chunk.name)
        return None
    return None if key is None else CookEntry(key)


class CookEntry:
    """One cook's slot in the cache: read it back, or record the cook that fills it."""

    def __init__(self, key: str):
        self.key = key
        self._before: list | None = None
        self._started = 0.0
        self._restored = False

    @property
    def _layer_path(self) -> Path:
        return cache_dir() / f"{self.key}.usdc"

    @property
    def _params_path(self) -> Path:
        return cache_dir() / f"{self.key}.params"

    def restore(self, chunk, layer: Sdf.Layer) -> bool:
        """Fill layer from the entry and set the params its cook set. False on a miss."""
        if mode() == "refresh":
            return False
        layer_path, params_path = self._layer_path, self._params_path
        if not layer_path.is_file() or not params_path.is_file():
            return False
        try:
            with open(params_path, "rb") as f:
                written = pickle.load(f)
            items = _items(chunk)
            # Checked before touching anything: an entry for a differently shaped chunk
            # (a param added since) would set the wrong params.
            for index, name, _has_value, _value, _bits in written:
                if index >= len(items) or items[index].name != name:
                    return False
            # Not layer.Import: that reads the file as the target's own format, and a
            # chunk's layer is anonymous usda while the entry is crate.
            source = Sdf.Layer.OpenAsAnonymous(str(layer_path))
            if source is None:
                raise OSError(f"could not open {layer_path}")
            layer.TransferContent(source)
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss, and goes
            logger.warning("cook cache: dropping unreadable entry %s", self.key, exc_info=True)
            _remove(layer_path, params_path)
            return False
        for index, _name, has_value, value, bits in written:
            item = items[index]
            if has_value and item.value != value:
                item.value = value
            for flag in _STATES:
                if bool(bits & flag) != item.has_state(flag):
                    (item.set_state if bits & flag else item.unset_state)(flag)
        for path in (layer_path, params_path):
            try:
                os.utime(path)  # read, so not pruned
            except OSError:
                pass
        self._restored = True
        logger.debug("cook cache: %s read back from %s", chunk.name, self.key[:12])
        return True

    def before_cook(self, chunk) -> None:
        """Note the params as they are, so after_cook can tell what the cook changed."""
        self._before = _snapshot(chunk)
        self._started = time.perf_counter()

    def after_cook(self, chunk, layer: Sdf.Layer) -> None:
        """Store what the cook just made, if it is something the key accounts for."""
        if self._restored or self._before is None:
            return
        elapsed = time.perf_counter() - self._started
        if elapsed < _MIN_COOK_S:
            return
        after = _snapshot(chunk)
        if [name for _, name, *_ in after] != [name for _, name, *_ in self._before]:
            logger.debug("cook cache: %s added or removed params while cooking; not stored", chunk.name)
            return
        written = []
        items = _items(chunk)
        for (index, name, before_value, before_bits), (_, _, value, bits) in zip(self._before, after):
            if before_value == value and before_bits == bits:
                continue
            item = items[index]
            has_value = hasattr(item, "value")
            if before_value != value and not _is_output(item):
                logger.debug(
                    "cook cache: %s set its own input %r while cooking; not stored", chunk.name, name
                )
                return
            written.append((index, name, has_value, item.value if has_value else None, bits))
        try:
            _store(self.key, layer, written)
        except Exception:  # noqa: BLE001 - a cache that cannot be written is only slower
            logger.warning("cook cache: could not store %s", chunk.name, exc_info=True)

    def store(self, chunk, layer: Sdf.Layer) -> None:
        """Store a result made outside a cook, such as a bake run from a button.

        Stored under this entry's key, so take the entry (lookup) when the work starts: a
        param changed while it ran must not relabel the result. The chunk's outputs (its
        readout) are stored as they are now, and come back with the layer.
        """
        mask = int(_STATES[0] | _STATES[1])
        written = [
            (i, p.name, True, p.value, int(p.state) & mask)
            for i, p in enumerate(_items(chunk))
            if hasattr(p, "value") and _is_output(p)
        ]
        try:
            _store(self.key, layer, written)
        except Exception:  # noqa: BLE001 - a cache that cannot be written is only slower
            logger.warning("cook cache: could not store %s", chunk.name, exc_info=True)


def _store(key: str, layer: Sdf.Layer, written: list) -> None:
    folder = cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    layer_path = folder / f"{key}.usdc"
    params_path = folder / f"{key}.params"
    # The params first and the layer last, each through a temporary: restore wants both, so
    # an entry with a layer is a whole one.
    tmp_params = folder / f"{key}.{os.getpid()}.params.tmp"
    with open(tmp_params, "wb") as f:
        pickle.dump(written, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_params, params_path)
    tmp_layer = folder / f"{key}.{os.getpid()}.tmp.usdc"
    if not layer.Export(str(tmp_layer)):
        raise OSError(f"could not export {layer.identifier}")
    os.replace(tmp_layer, layer_path)
    # After, so the cap counts what was just written -- and a single entry larger than the
    # cap is dropped again rather than left to push the folder over it.
    _prune(folder)


def max_bytes() -> int:
    """The cache's size cap: USD_CHUNKS_COOK_CACHE_MAX_GB, default 2 GB."""
    try:
        return int(float(os.environ.get(ENV_MAX_GB, "") or _DEFAULT_MAX_GB) * 1024**3)
    except ValueError:
        return int(_DEFAULT_MAX_GB * 1024**3)


def _prune(folder: Path) -> None:
    """Drop entries unread for 30 days, then the least recently read until under the cap.

    Reading an entry touches it, so "least recently read" is oldest mtime. Both files of an
    entry go together, keyed by the name before the first dot.
    """
    entries: dict[str, list] = {}
    for path in folder.iterdir():
        try:
            st = path.stat()
        except OSError:
            continue
        entry = entries.setdefault(path.name.split(".", 1)[0], [0.0, 0, []])
        entry[0] = max(entry[0], st.st_mtime)
        entry[1] += st.st_size
        entry[2].append(path)
    cutoff = time.time() - _MAX_AGE_S
    total = sum(size for _, size, _ in entries.values())
    cap = max_bytes()
    for mtime, size, paths in sorted(entries.values(), key=lambda e: e[0]):
        if mtime >= cutoff and total <= cap:
            break
        _remove(*paths)
        total -= size


def _remove(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _value_params(chunk) -> list:
    """Every param under the chunk that holds a value, depth first, in a stable order.

    Groups hold no value and are walked into; a vector holds one -- the whole tuple -- and is
    taken as it is.
    """
    return [p for _, p in chunk.iter_hierarchy() if p is not chunk and hasattr(p, "value")]


def _items(chunk) -> list:
    """Everything under the chunk, groups included, depth first, in a stable order.

    Groups too, because a cook may show or hide a whole one -- Mesh shows only the group of
    the shape it builds.
    """
    return [p for _, p in chunk.iter_hierarchy() if p is not chunk]


def _is_output(param) -> bool:
    return param.has_flag(ChunkFlags.ParamFlags.OUTPUT) or param.get_type() in _OUTPUT_TYPES


def _snapshot(chunk) -> list:
    """(index, name, value repr or None, the _STATES bits) for everything under the chunk."""
    mask = int(_STATES[0] | _STATES[1])
    return [
        (i, p.name, _stable_repr(p.value) if hasattr(p, "value") else None, int(p.state) & mask)
        for i, p in enumerate(_items(chunk))
    ]


def _stable_repr(value: Any) -> str:
    return repr(value)


def _key(chunk, module) -> str | None:
    h = hashlib.sha256()

    def add(*parts):
        for part in parts:
            h.update(repr(part).encode())
            h.update(b"\0")

    add(_FORMAT, sys.version_info[:2], Usd.GetVersion(), module.source_hash)
    add(_code_fingerprint(str(Path(module.module_file).parent)))
    add(str(chunk.get_config().module))

    stage = chunk.get_stage()
    root = stage.GetRootLayer() if stage else None
    # Stage-wide settings any cook may scale or orient by (CAD Import divides by
    # metersPerUnit, House builds Y- or Z-up), and no param carries.
    if stage:
        add(UsdGeom.GetStageUpAxis(stage), UsdGeom.GetStageMetersPerUnit(stage))
    for param in _value_params(chunk):
        if param.name == TOGGLE or _is_output(param):
            continue
        value = param.value
        add(param.name, _stable_repr(value))
        if isinstance(value, str) and value:
            add(_file_stat(value, root))

    hook = chunk._module_hook("cache_key")
    if hook is not None:
        extra = hook(chunk)
        if extra is None:
            return None
        add(extra)
    return h.hexdigest()


def _file_stat(value: str, root: Sdf.Layer | None):
    """(path, size, mtime) of the file value names, or None if it names none."""
    if len(value) > 1024 or "\n" in value:
        return None
    path = value
    if not os.path.isabs(path):
        if root is None or root.anonymous:
            return None
        path = root.ComputeAbsolutePath(value)
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(path):
        return None
    return (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)


@functools.cache
def _code_fingerprint(module_dir: str) -> str:
    """Sizes and mtimes of the framework's files and the module's siblings.

    A module's result also depends on the helpers it imports, which its own hash does not
    see. Once per directory per process: code edited mid-session needs a restart anyway.
    """
    h = hashlib.sha256()
    framework = Path(__file__).parent
    files = sorted(framework.rglob("*.py")) + sorted(Path(module_dir).glob("*.py"))
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        h.update(f"{path}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()
