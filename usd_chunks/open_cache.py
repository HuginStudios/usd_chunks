"""Opening a network without cooking it: each chunk's result, kept when the network is saved.

A saved network is chunk prims and wires, never their results, so opening one cooks every
chunk again -- most of the time an open takes. When the network is saved, each chunk's
layer goes in the cook cache (cook_cache) under a key for everything its result can depend
on. When it is opened, a chunk's first cook reads its layer back instead, if the key
still matches. A miss is only a cook.

The key (open_key) is the chunk's own key, as cook_cache makes it (its module's code, its
params as the file stores them and the files they name, the stage's units), with its chunk
prim's bookkeeping (its exec wires, its xform op id), the keys of the chunks one step above
it (skip_cook.direct_upstream_chunks), which hold theirs in turn, so everything a cook can
read, and the scene outside the chunks (scene_fingerprint): the root layer's content and
metadata, its sublayers and the files it references, by size and mtime, and which layers
are muted. A change to any of it is a miss. Each chunk records its key as it cooks, so the
chunks below it, cooking after it, do not work out everything above them again.

Saving copies the layers and writes them to disk on a thread (wait() waits for it). Each
entry also keeps what its cook read (skip_cook.memo), so a result read back can still be
skipped at the first edit after opening, as a cooked one would be.

Only a result that lies wholly in the chunk's own layer is kept: a chunk whose last cook
wrote into the session layer as well (a shader's input redirect, a variant selection), or
changed the layer stack or chunk prims (chunk_cook's _diff_ok), cooks on every open. So does
a module whose config sets cook_on_open: one that reads what no key can see, such as the
clock, an unseeded random or a web request.

`USD_CHUNKS_OPEN_CACHE=0` turns it off; so does `USD_CHUNKS_COOK_CACHE=0`, and
`USD_CHUNKS_COOK_CACHE=refresh` keeps storing but never reads back.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import threading
import weakref
from collections.abc import Callable, Iterable

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk_naming, cook_cache, skip_cook

logger = logging.getLogger(__name__)

ENV = "USD_CHUNKS_OPEN_CACHE"

#: Bumped when the key's contents change, so old entries miss.
_FORMAT = 1

#: Root layer metadata a cook may read: the time range and rate, the units and up axis.
_ROOT_INFO = ("startTimeCode", "endTimeCode", "timeCodesPerSecond", "framesPerSecond",
              "metersPerUnit", "upAxis")

#: Set on a chunk after each cook: whether that cook wrote nothing outside its own layer.
_OWN_LAYER_ONLY = "_open_cache_own_layer_only"


def enabled() -> bool:
    """Whether opening reads results back: USD_CHUNKS_OPEN_CACHE and the cook cache's switch."""
    if os.environ.get(ENV, "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return cook_cache.mode() != "off"


# ------------------------------------------------------------------------------------------
# What a cook wrote besides its own layer
# ------------------------------------------------------------------------------------------


#: Per layer identifier: (the layer, weakly; a one-item list counting the change rounds that
#: edited it; the callback doing the counting and its Tf listener). Counted through the list, not a method of
#: an object holding the listener, so nothing here is a reference cycle left for the garbage
#: collector; an entry whose layer has gone is dropped as the next one is added.
_counts: dict[str, tuple[weakref.ref, list[int], object]] = {}


def _counter(tally: list[int]) -> Callable:
    """A Tf callback adding one to tally[0] per notice: it takes exactly (notice, sender)."""
    def heard(_notice, _sender) -> None:
        tally[0] += 1
    return heard


def _count(layer: Sdf.Layer) -> int:
    """How many change rounds have edited layer. Sdf says nothing of a write that changes nothing."""
    held = _counts.get(layer.identifier)
    # The entry must be this layer's, not only one under its name: an anonymous layer's
    # identifier holds its address, so a new one can take a dead one's, while the dead one's
    # Python object lives on (an expired handle) with a listener on a layer that is gone.
    known = held[0]() if held is not None else None
    if known is None or getattr(known, "expired", False) or known != layer:
        for ident in [i for i, entry in _counts.items() if entry[0]() is None]:
            del _counts[ident]
        tally = [0]
        heard = _counter(tally)
        # The callback is kept here as well: Tf holds it only weakly.
        held = (weakref.ref(layer), tally,
                (heard, Tf.Notice.Register(Sdf.Notice.LayersDidChangeSentPerLayer, heard, layer)))
        _counts[layer.identifier] = held
    return held[1][0]


def shared_layer_edits(stage: Usd.Stage) -> tuple[int, int]:
    """How often the root and session layers have been edited, to compare around a cook."""
    return _count(stage.GetRootLayer()), _count(stage.GetSessionLayer())


def note_cook(chunk, stage: Usd.Stage, before: tuple[int, int]) -> None:
    """Record whether the cook that just ran left the root and session layers alone.

    Once a chunk has been seen writing there, it stays marked for the session: a later cook
    writing the same opinion again changes nothing, Sdf says nothing, and the result it needs
    on reopening is still out there and not in its layer.

    Args:
        chunk: The chunk that cooked.
        stage: The scene's stage.
        before: shared_layer_edits(stage) from before the cook.
    """
    before_now = getattr(chunk, _OWN_LAYER_ONLY, None)
    setattr(chunk, _OWN_LAYER_ONLY, before_now is not False and shared_layer_edits(stage) == before)


def wrote_own_layer_only(chunk) -> bool:
    """Whether chunk's last cook wrote nothing outside its own layer (note_cook)."""
    return bool(getattr(chunk, _OWN_LAYER_ONLY, False))


# ------------------------------------------------------------------------------------------
# The key
# ------------------------------------------------------------------------------------------


def _stat(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)


def scene_fingerprint(stage: Usd.Stage, root: Sdf.Layer | None = None) -> str:
    """What a cook can read from the scene that is not a chunk's: see the module docstring.

    Read from the root layer as the file holds it, so a save, which reads the file it just
    wrote, and the open that reads it back agree. The session layer is left out: what is in
    it when a network opens is what the chunks write there as they cook, and a chunk that
    writes there is not kept.

    Args:
        stage: The scene's stage.
        root: The root layer as it will be opened; the stage's own when omitted.

    Returns:
        A digest.
    """
    h = hashlib.sha256()

    def add(*parts):
        for part in parts:
            h.update(repr(part).encode())
            h.update(b"\0")

    root = root if root is not None else stage.GetRootLayer()
    for key in _ROOT_INFO:
        add(key, root.pseudoRoot.GetInfo(key) if root.pseudoRoot.HasInfo(key) else None)
    # The root's own prims but the chunk prims, which the chunks' own keys cover.
    content = Sdf.Layer.CreateAnonymous("open_cache_root")
    content.TransferContent(root)
    chunks_name = chunk_naming.CHUNKS_ROOT.lstrip("/")
    if chunks_name in content.pseudoRoot.nameChildren:
        del content.pseudoRoot.nameChildren[chunks_name]
    for key in list(content.pseudoRoot.ListInfoKeys()):
        content.pseudoRoot.ClearInfo(key)
    add(content.ExportToString())
    # What the root brings in from files, as they are on disk now.
    # And every file those bring in, and theirs: a layer nested under a sublayer is read too.
    for path in sorted(content.GetCompositionAssetDependencies()):
        add(path, None if root.anonymous else cook_cache.file_tree_stat(root.ComputeAbsolutePath(path)))
    for path in root.subLayerPaths:
        layer = Sdf.Layer.FindRelativeToLayer(root, path)
        real = layer.realPath if layer is not None else (
            "" if root.anonymous else root.ComputeAbsolutePath(path))
        add(cook_cache.file_tree_stat(real) if real else path)
    # Anonymous layers are the chunks' own, muted by gates; their names change every session.
    add(sorted(layer for layer in stage.GetMutedLayers() if not Sdf.Layer.IsAnonymousLayerIdentifier(layer)))
    return h.hexdigest()


def _bookkeeping(chunk, stage: Usd.Stage) -> list:
    """The chunk prim's own attributes but its params and node-graph layout.

    Its exec wires (which chunk feeds which slot) and anything the framework keeps there,
    such as the id of a Transform's xform op.
    """
    path = getattr(chunk, "_exec_prim_path", None)
    prim = stage.GetPrimAtPath(path) if path else None
    if not (prim and prim.IsValid()):
        return []
    found = []
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if name.startswith("ui:"):
            continue
        if name.startswith(chunk_naming.INPUTS_PREFIX) and not chunk_naming.is_exec_input_name(name):
            continue
        found.append((name, repr(attr.Get()), [str(c) for c in attr.GetConnections()]))
    return found


def _own_key(chunk, stage: Usd.Stage, hook: bool = True) -> str | None:
    # A module that cooks on every open reads what no key can see, so its result can change
    # on open: none here, and so none for any chunk below it either.
    if not _may_keep(chunk):
        return None
    own = cook_cache.input_key(chunk, chunk.get_chunk_module(), hook=hook)
    if own is None:
        return None
    h = hashlib.sha256()
    h.update(repr((own, getattr(chunk, "_exec_prim_path", None), _bookkeeping(chunk, stage))).encode())
    return h.hexdigest()


#: Set on a chunk after each cook: (scene fingerprint, its key then), for the chunks below it.
_KEY = "_open_cache_key"

#: What every key here starts with, apart from cook_cache's own.
_KEY_PREFIX = "open_"


def open_key(chunk, stage: Usd.Stage, fingerprint: str, memo: dict | None = None,
             recorded: Callable | None = None, hook: bool = True) -> str | None:
    """The key a chunk's result is kept under for the next open, or None if it cannot have one.

    Its own key, and the key of each chunk one step above it (skip_cook.direct_upstream_chunks),
    which is theirs and so on: everything above it, keyed once each.

    Args:
        chunk: The chunk.
        stage: The scene's stage.
        fingerprint: scene_fingerprint(stage).
        memo: Keys already made, by id(chunk), to share across chunks.
        recorded: Given a chunk, whether the key its last cook recorded still holds. A key
            recorded then is used instead of being made again: in an open, every chunk above
            one about to cook has cooked already.
        hook: Include each module's cache_key hook (cook_cache.input_key).

    Returns:
        A digest, or None when it or a chunk above it has no key, or the chunks loop.
    """
    memo = {} if memo is None else memo
    if id(chunk) in memo:
        return memo[id(chunk)]
    if recorded is not None and recorded(chunk):
        held = getattr(chunk, _KEY, None)
        if held is not None and held[0] == fingerprint:
            memo[id(chunk)] = held[1]
            return held[1]
    memo[id(chunk)] = None  # a loop back here has no key
    own = _own_key(chunk, stage, hook)
    if own is None:
        return None
    parts = []
    for dep in skip_cook.direct_upstream_chunks(chunk):
        key = open_key(dep, stage, fingerprint, memo, recorded, hook)
        if key is None:
            return None
        parts.append((getattr(dep, "_exec_prim_path", None) or "", key))
    h = hashlib.sha256()
    h.update(repr((_FORMAT, fingerprint, own, sorted(parts))).encode())
    memo[id(chunk)] = key = _KEY_PREFIX + h.hexdigest()
    return key


def inputs_key(chunk) -> str | None:
    """A key for what flows into chunk: the keys of the chunks one step above it.

    For a Cache chunk telling whether what it wrote is still what is above it. Without the
    scene fingerprint: compared across saves and opens, and the network's root layer reads
    differently live and saved, so every cache would look out of date after a save. And
    without the modules' cache_key hooks, which read the stage: once the Cache has switched
    the chunks above off, what a Scatter's hook reads is not what it scattered on.

    Args:
        chunk: The chunk.

    Returns:
        A digest, or None when a chunk above it has no key (or with nothing above it).
    """
    stage = chunk.get_scene_stage()
    if stage is None:
        return None
    memo: dict = {}
    parts = []
    for dep in skip_cook.direct_upstream_chunks(chunk):
        key = open_key(dep, stage, "", memo, hook=False)
        if key is None:
            return None
        parts.append((getattr(dep, "_exec_prim_path", None) or "", key))
    if not parts:
        return None
    return hashlib.sha256(repr((_FORMAT, sorted(parts))).encode()).hexdigest()


def record(chunk, stage: Usd.Stage, recorded: Callable, first: bool) -> None:
    """Note the key of the result a chunk just cooked, for the first cooks of the chunks below it.

    Only on its first cook, which is when the chunks below it look theirs up: an open, or a
    chunk just made. Any later cook only forgets the key, so one it no longer matches is never
    used; working it out again on every cook made each edit read the whole root layer.

    Args:
        chunk: The chunk that cooked.
        stage: The scene's stage.
        recorded: As open_key takes it.
        first: Whether this was the chunk's first cook.
    """
    if not first:
        setattr(chunk, _KEY, None)
        return
    if not enabled():
        return
    try:
        fingerprint = _fingerprint_for(stage)
        setattr(chunk, _KEY, None)
        key = open_key(chunk, stage, fingerprint, recorded=recorded)
    except Exception:  # noqa: BLE001 - only the chunks below cook instead of reading back
        logger.debug("open cache: could not key %s", chunk.name, exc_info=True)
        return
    setattr(chunk, _KEY, None if key is None else (fingerprint, key))


def _may_keep(chunk) -> bool:
    """Whether this chunk's module lets its result be read back on open."""
    config = chunk.get_config()
    return not (config is None or getattr(config, "cook_on_open", False))


# ------------------------------------------------------------------------------------------
# Kept on save, read back on open
# ------------------------------------------------------------------------------------------


def store(chunks: Iterable, stage: Usd.Stage, saved: Sdf.Layer, layer_of: Callable, ready: Callable) -> int:
    """Keep each chunk's layer for the next open of the network just saved.

    Args:
        chunks: The scene's chunks.
        stage: The scene's stage.
        saved: The file the network was saved to, as it will be opened.
        layer_of: The layer of a chunk (ChunkLayerManager.get_layer).
        ready: Whether a chunk's layer holds the result of its current inputs.

    Returns:
        How many results are being written; one already kept is not written again. They
        are copied now and written to disk on a thread of their own, so the save does not
        wait for hundreds of small files (wait() does).
    """
    if not enabled():
        return 0
    try:
        fingerprint = scene_fingerprint(stage, saved)
    except Exception:  # noqa: BLE001 - the save stands; the next open cooks
        logger.warning("open cache: could not read the scene to key its results", exc_info=True)
        return 0
    memo: dict = {}
    jobs = []
    for chunk in chunks:
        if chunk._module_hook("execute") is None or not _may_keep(chunk):
            continue
        if getattr(chunk, "_diff_ok", None) is not True or not wrote_own_layer_only(chunk) or not ready(chunk):
            continue
        layer = layer_of(chunk)
        if layer is None or not layer.anonymous:
            continue
        try:
            key = open_key(chunk, stage, fingerprint, memo)
            if key is None or cook_cache.has_entry(key):
                continue
            copy = Sdf.Layer.CreateAnonymous("open_cache_entry")
            copy.TransferContent(layer)
            jobs.append((key, copy, cook_cache.output_values(chunk), skip_cook.memo(chunk), chunk.name))
        except Exception:  # noqa: BLE001 - a result that cannot be kept only cooks next time
            logger.warning("open cache: could not keep %s", chunk.name, exc_info=True)
    if jobs:
        _write_later(jobs)
    return len(jobs)


_writers: list[threading.Thread] = []


def _write_later(jobs: list) -> None:
    """Write the entries on a thread: a few milliseconds a file, hundreds of files per save.

    Not a daemon, so a script that saves and exits still finishes writing them. Each entry
    is written to a temporary name and moved into place, so one cut short is only missing.
    """
    def write() -> None:
        for key, layer, written, reads, name in jobs:
            try:
                # Before the entry, which is whole once its layer is written.
                if reads is not None:
                    _write_reads(key, reads)
                cook_cache.store_entry(key, layer, written, prune=False)
            except Exception:  # noqa: BLE001 - only cooks next time
                logger.warning("open cache: could not keep %s", name, exc_info=True)
        cook_cache.prune()
        logger.debug("open cache: kept %d results", len(jobs))

    _writers[:] = [t for t in _writers if t.is_alive()]
    thread = threading.Thread(target=write, name="open_cache_writer", daemon=False)
    _writers.append(thread)
    thread.start()


def _reads_path(key: str):
    """Where an entry's skip_cook.memo is kept: beside it, pruned with it (same stem)."""
    return cook_cache.cache_dir() / f"{key}.reads"


def _write_reads(key: str, reads: dict) -> None:
    path = _reads_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{key}.{os.getpid()}.reads.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(reads, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _read_reads(key: str) -> dict | None:
    try:
        with open(_reads_path(key), "rb") as f:
            return pickle.load(f)
    except (OSError, pickle.PickleError, EOFError):
        return None


def wait() -> None:
    """Wait for the results a save is still writing."""
    for thread in list(_writers):
        thread.join()


def lookup(chunk, stage: Usd.Stage, recorded: Callable) -> cook_cache.CookEntry | None:
    """The kept result a chunk's first cook may read back, or None to cook it.

    Args:
        chunk: The chunk about to cook for the first time.
        stage: The scene's stage.
        recorded: As open_key takes it.

    Returns:
        The entry; restoring it tells whether it was there.
    """
    if not enabled() or cook_cache.mode() == "refresh" or not _may_keep(chunk):
        return None
    try:
        key = open_key(chunk, stage, _fingerprint_for(stage), recorded=recorded)
    except Exception:  # noqa: BLE001 - a key that cannot be made is a cook
        logger.debug("open cache: could not key %s", chunk.name, exc_info=True)
        return None
    if key is None or not cook_cache.has_entry(key):
        return None
    return cook_cache.CookEntry(key, store=False)


#: The scene fingerprint, kept while the root layer, the layer stack and the mutes are as
#: they were: an open asks it once per chunk.
_fingerprint: tuple | None = None


def _fingerprint_for(stage: Usd.Stage) -> str:
    global _fingerprint
    root = stage.GetRootLayer()
    state = (root.identifier, _count(root), tuple(root.subLayerPaths), tuple(sorted(stage.GetMutedLayers())))
    if _fingerprint is not None and _fingerprint[0]() is stage and _fingerprint[1] == state:
        return _fingerprint[2]
    digest = scene_fingerprint(stage)
    _fingerprint = (weakref.ref(stage), state, digest)
    return digest


def is_open_entry(entry: cook_cache.CookEntry) -> bool:
    """Whether entry is one of these (read back on open) rather than a cache_cook module's."""
    return entry.key.startswith(_KEY_PREFIX)


def restored(chunk, entry: cook_cache.CookEntry, layer: Sdf.Layer, stage: Usd.Stage) -> None:
    """A chunk's result was read back: it may be kept again, its key is the entry's, and
    what its cook read comes back too, so the first edit after opening can skip it.

    Args:
        chunk: The chunk.
        entry: The entry read back.
        layer: The chunk's layer, now holding the result.
        stage: The scene's stage.
    """
    setattr(chunk, _OWN_LAYER_ONLY, True)
    if _fingerprint is not None:
        setattr(chunk, _KEY, (_fingerprint[2], entry.key))
    reads = _read_reads(entry.key)
    if reads is not None:
        try:
            skip_cook.adopt(chunk, reads, layer, stage)
        except Exception:  # noqa: BLE001 - only means its next cook is not skipped
            logger.debug("open cache: could not take back what %s read", chunk.name, exc_info=True)
