"""Cache chunk -- keeps everything above it in one .usdc file, and reads that instead.

    Mesh ── Fracture ── RBD Solver ── Cache ──▶   reads cache/Cache.usdc; the three above
                                                   do not cook while the file is there

Press Recache: the chunks above cook, their layers are flattened into one .usdc file (one
layer for the rest of the network to compose instead of many), and from then on the Cache
reads that file and keeps the chunks above it switched off, the way a Switch keeps the
branches it does not pick. Opening the network reads the file again; nothing above cooks.

Something above changing does not cook it: the Cache keeps showing its file and turns
stale (the node's stale badge, and its Status says so) until Recache is pressed. Whether
the file is out of date is told by a key of everything above, as open_cache keys a
chunk's result, written into the file.

Known limits:

- A chunk above that writes outside its own layer (a MaterialX Shader's input wires go to
  the session layer) loses that part in the file. Recache warns when one does.
- Switching off is a layer mute, so a chunk above that also feeds something outside the
  Cache is switched off for that too -- the same limit a Switch has.
- An edit to the network file's own prims, or updating usd_chunks or a chunk module, can
  make the file look stale or not; the key covers the chunks above, not the scene around.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import logging
import os
import time

from pxr import Sdf, Usd, UsdGeom, UsdUtils

from usd_chunks import asset_paths, chunk, chunk_cook, chunk_notice, chunk_registry, chunk_stage, cook_cache, open_cache
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

#: customLayerData of the file: the key of what was above when it was written, and who wrote it.
_KEY_FIELD = "usdChunks:cacheKey"
_BY_FIELD = "usdChunks:cachedBy"

#: Set on the chunk while Recache runs, so its cook lets the chunks above through.
_RECACHING = "_cache_recaching"

#: Stage metadata copied onto the file, so it reads in the same units and time range.
_STAGE_INFO = ("upAxis", "metersPerUnit", "startTimeCode", "endTimeCode", "timeCodesPerSecond",
               "framesPerSecond")


def _file(node) -> str:
    """The cache file as an absolute path, or "" when File is empty."""
    param = node.get_param("file")
    value = (param.value or "").strip() if param is not None else ""
    if not value:
        return ""
    if os.path.isabs(value):
        return os.path.normpath(value)
    folder = asset_paths.network_folder(node.get_scene_stage())
    return os.path.normpath(os.path.join(folder, value)) if folder else ""


def _default_file(node) -> str:
    """Where Recache writes when File is empty: beside the network, else the cook cache folder.

    Beside the network as a ./ path, so the network and its caches move together, named
    after both: two networks in one folder each with a Cache called Cache wrote one file.
    A network not saved yet has no folder, so its cache goes in the cook cache folder.
    """
    stage = node.get_scene_stage()
    root = stage.GetRootLayer() if stage is not None else None
    network = os.path.splitext(root.GetDisplayName())[0] if root else ""
    name = f"{network}_{node.name}.usdc" if network else f"{node.name}.usdc"
    if asset_paths.network_folder(stage):
        return f"./cache/{name}"
    return str(cook_cache.cache_dir() / "caches" / name)


def _written_key(path: str) -> str | None:
    """The key the file at path was written with, or None if it has none or will not open."""
    layer = Sdf.Layer.FindOrOpen(path)
    if layer is None:
        return None
    return layer.customLayerData.get(_KEY_FIELD)


def _all_slots(node) -> set[int]:
    return set(range(len(chunk_stage.exec_dependencies(node))))


def _set_status(node, text: str) -> None:
    param = node.get_param("status")
    if param is not None and param.value != text:
        param.value = text


def gate_before_cook(node):
    """With its file on disk, the Cache keeps every input off before anything cooks.

    Called by the framework before a cook pass (chunk_stage.gate_ahead), so the chunks above
    do not cook on open only to be switched off when the Cache cooks after them.
    """
    if getattr(node, _RECACHING, False):
        return None
    path = _file(node)
    return set() if path and os.path.isfile(path) else None


def execute(node):
    path = _file(node)
    if getattr(node, _RECACHING, False) or not path or not os.path.isfile(path):
        # No file yet, or Recache running: the chunks above reach the stage as they would
        # without a Cache.
        chunk_stage.gate_to_branches(node, _all_slots(node))
        node.unset_state(ChunkFlags.State.STALE)
        if not getattr(node, _RECACHING, False):
            _set_status(node, "No cache yet: press Recache to write one.")
        return

    chunk_stage.gate_to_branches(node, set())
    layer = node.get_edit_context()
    if layer is not None:
        layer.subLayerPaths = [path]
    # A load starts a cook from every chunk above that nothing feeds, and each reaches this one
    # (never_skip): the key of everything above, once per start, was most of opening a
    # 300-chunk village. Checked once when the load is done (_loaded).
    if chunk_notice._loading_stage is None:
        _check_stale(node, path)


def _check_stale(node, path: str) -> None:
    """Mark the Cache stale when what is above it is no longer what its file was written from."""
    written, now = _written_key(path), open_cache.inputs_key(node)
    if written and now and written != now:
        node.set_state(ChunkFlags.State.STALE)
        _set_status(node, "Stale: something above changed since the cache was written. "
                          "It still shows the file; press Recache to update it.")
    else:
        node.unset_state(ChunkFlags.State.STALE)
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        _set_status(node, f"Reading {os.path.basename(path)}, written {stamp}.")


def _upstream(node) -> list:
    """Every chunk above node, through every input."""
    found: set = set()
    for dep in chunk_stage.exec_dependencies(node):
        found |= chunk_stage.exec_upstream(dep)
    return [c for c in node.get_scene().get_chunks() if c in found]


def _flatten(stage: Usd.Stage, layers: list[Sdf.Layer]) -> Sdf.Layer:
    """One layer holding what layers compose to, strongest first as given."""
    root = Sdf.Layer.CreateAnonymous("cache_flatten")
    root.subLayerPaths = [layer.identifier for layer in layers]
    scene_root = stage.GetRootLayer()
    for key in _STAGE_INFO:
        if scene_root.pseudoRoot.HasInfo(key):
            root.pseudoRoot.SetInfo(key, scene_root.pseudoRoot.GetInfo(key))
    root.pseudoRoot.SetInfo("upAxis", UsdGeom.GetStageUpAxis(stage))
    root.pseudoRoot.SetInfo("metersPerUnit", UsdGeom.GetStageMetersPerUnit(stage))
    return UsdUtils.FlattenLayerStack(Usd.Stage.Open(root))


def recache(node) -> str | None:
    """Cook everything above node, write it to the cache file, and read that from now on.

    Args:
        node: The Cache chunk.

    Returns:
        The file written, or None when there is nothing above it to cache.
    """
    stage = node.get_scene_stage()
    upstream = _upstream(node)
    if stage is None or not upstream:
        _set_status(node, "Nothing above to cache: wire a chunk into it.")
        return None
    if not (node.get_param("file").value or "").strip():
        node.get_param("file").value = _default_file(node)
    path = _file(node)

    setattr(node, _RECACHING, True)
    try:
        # Let the chunks above through, then cook the ones that changed while they were off.
        chunk_stage.gate_to_branches(node, _all_slots(node))
        with chunk.change_block():
            for c in upstream:
                if chunk_cook.is_cook_stale(c):
                    c.execute()
        manager = chunk_registry.ChunkLayerManager()
        own = {layer.identifier: layer for c in upstream if (layer := manager.get_layer(c)) is not None}
        layers = [layer for layer in stage.GetLayerStack(includeSessionLayers=True)
                  if layer.identifier in own]
        outside = [c.name for c in upstream if not open_cache.wrote_own_layer_only(c)]
        if outside:
            logger.warning("%s: %s also wrote outside its own layer, and that part is not in the cache.",
                           node.name, ", ".join(outside))
        flat = _flatten(stage, layers)
        flat.customLayerData = {_KEY_FIELD: open_cache.inputs_key(node) or "",
                                _BY_FIELD: chunk_stage.get_chunk_prim_path(node)}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp.usdc"
        if not flat.Export(tmp):
            raise OSError(f"could not write {tmp}")
        os.replace(tmp, path)
        if (held := Sdf.Layer.Find(path)) is not None:
            held.Reload(force=True)
    finally:
        setattr(node, _RECACHING, False)
    node.execute()
    return path


def _recache_pressed(owner, _notice, _sender):
    recache(owner)


def _loaded(owner, _notice, _sender):
    """The network is open: compare what is above with the file, once."""
    path = _file(owner)
    if path and os.path.isfile(path) and not getattr(owner, _RECACHING, False):
        _check_stale(owner, path)


def main():
    chunk_api = chunk.Chunk("Cache")
    chunk_api.add_param(chunk.FileBrowser(
        "file", "File", extensions=[".usdc", ".usd", ".usda"],
        tooltip="The cache file. Empty: Recache writes ./cache/<name>.usdc beside the network "
        "(the cook cache folder while the network is not saved yet).",
    ))
    button = chunk.Button("recache", "Recache",
                          tooltip="Cook everything above, write it to File, and read that from now on.")
    chunk_api.add_param(button)
    chunk_api.add_callback(_recache_pressed, sender=button)
    chunk_api.add_callback(_loaded, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)
    chunk_api.add_param(chunk.Label("status", "No cache yet", default_value="No cache yet",
                                    tooltip="Whether the file is there and up to date."))
    muted = chunk.TextEdit(
        chunk_stage.GATE_RECORD_PARAM,
        tooltip="Bookkeeping: the chunks this Cache keeps off, so one that stops being above "
        "it is switched back on rather than left off.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        icon="database",
        # Its cook compares the key of everything above with the file's: a change above that
        # writes nothing (everything above is switched off) still has to reach it.
        never_skip=True,
        description="""Caches everything above it to a .usdc file and reads that instead (freeze, bake).

Press Recache: the chunks above cook, their result is flattened into one .usdc file
(File, by default ./cache/<name>.usdc beside the network), and from then on the Cache reads
that file and keeps the chunks above switched off, so they do not cook, also on open. When
something above changes it keeps the file and turns stale until Recache is pressed. A chunk
above that writes outside its own layer (MaterialX Shader) loses that part in the file.""",
    )
