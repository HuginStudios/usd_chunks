"""Export USD chunk -- exports every real layer in the stage to its own file, or,
with Flatten into one file checked, merges everything into a single file instead.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import os

from pxr import Sdf, Usd, UsdUtils

from usd_chunks import asset_paths, chunk, chunk_registry, chunk_utils
from usd_chunks import chunk_naming, chunk_notice, chunk_stage
from usd_chunks.chunk_stage import get_chunk_prim_path


def _same_file(a: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(a)))


def _network_files(stage) -> set[str]:
    """The files on disk holding a chunk network this stage has open -- never export targets.

    Opened with `usdchunks run net.usda`, the network file is the root layer, and so one of
    the real layers this chunk writes out. With Output Directory set to the network's own
    folder its row's file name is the network file itself, and exporting replaced the
    network with its stripped, chunk-less result.
    """
    layers = list(stage.GetLayerStack(includeSessionLayers=True)) + list(stage.GetUsedLayers())
    return {_same_file(lyr.realPath) for lyr in layers
            if not lyr.anonymous and lyr.realPath and lyr.GetPrimAtPath(chunk_naming.CHUNKS_ROOT)}


def _would_overwrite_network(path: str, networks: set[str], row: str) -> bool:
    if _same_file(path) not in networks:
        return False
    print(f"[ExportUSD] Not exporting {row} to {path}: that file is the chunk network itself, and "
          f"writing it would replace the network. Give the row another File Name, untick its "
          f"Export, or pick another Output Directory.")
    return True


_USD_EXTENSIONS = (".usd", ".usda", ".usdc")


def _discover_export_layers(stage):
    """Return [(name, layer), ...] for every real layer Export All Layers would write,
    excluding Session Layer -- it's handled separately and always holds full bookkeeping.

    The layers a Layer chunk made for an asset to bring in by payload or reference come
    last: in no layer stack, so only the layer manager knows them, and each is written as
    its own file, which the arcs to it are rewritten to point at.
    """
    internal_ids = chunk_utils.chunk_internal_layer_ids()
    session_id = stage.GetSessionLayer().identifier
    lm = chunk_registry.ChunkLayerManager()
    arc = lm.arc_layers() if lm.initialized else []
    return [(name, layer) for name, layer in list(chunk_utils.user_layers(stage)) + arc
            if layer.identifier != session_id and layer.identifier not in internal_ids]


#: Set once CHUNK_LOADED has fired -- the saved rows are in, the list may rebuild.
_IN_SCENE = "_export_usd_in_scene"


def _chunk_loaded(owner, notice, sender):
    setattr(owner.get_chunk(), _IN_SCENE, True)
    _refresh_layer_rows(owner, notice, sender)


def _refresh_layer_rows(owner, notice, sender):
    """Rebuild the per-layer export list from the real layers currently in the stage.

    Live: rebuilt whenever the stage's contents change (a Layer chunk adding a
    new sublayer, etc.), carrying each surviving layer's own checkbox state across the
    resize by name -- a rebuild fires for any layer-set change at all, so dropping that
    state would mean one new layer wiping every choice already made about the others.
    Registered globally so this updates automatically, no manual refresh step needed.

    Must no-op when the layer set hasn't actually changed: this list's own row params
    live on the Export USD chunk prim like any other param, so toggling a checkbox here
    authors a USD attribute change, which re-fires this same STAGE_CONTENTS_CHANGED
    listener -- without this guard, checking a box would immediately trigger a rebuild
    that resets it straight back to the default before it could ever be read.
    """
    if chunk_notice._loading_stage is not None:
        # The rows are being restored from the saved scene, one array at a time, while the
        # stage is still filling in. A rebuild now pairs half-restored values with layers
        # that are not all there yet; the first refresh after the load does it properly.
        return
    _chunk = owner.get_chunk()
    stage = _chunk.get_stage() if _chunk else None
    if not stage:
        return
    if not getattr(_chunk, _IN_SCENE, False):
        # Held off until CHUNK_LOADED: the saved rows, and the checkboxes on them, are
        # restored after main() registers this, and a rebuild before then would race them.
        return

    rows = _chunk.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return

    layers = _discover_export_layers(stage)
    name_t = rows.get_param("layer_name")
    bake_t = rows.get_param("bake")
    export_t = rows.get_param("export_enabled")
    file_t = rows.get_param("file_name")

    new_names = [name for name, _layer in layers]
    # Compared against the names this handler itself last wrote, not against a fresh read
    # back out of the row proxies. This fires on every authoring op -- including the writes
    # a rebuild is itself in the middle of making -- and a read taken mid-cascade can come
    # back with a row still holding its previous name, a phantom difference that rebuilds
    # the list and silently resets every checkbox the user had set (which row won came
    # down to notice ordering, so it only bit sometimes). rows.size is still read live, so
    # a list resized behind this handler's back (restored from USD on load) still rebuilds.
    if getattr(_chunk, "_export_layer_names", None) == new_names and rows.size == len(new_names):
        return
    # Every write below authors an attribute change that re-fires this same listener, and
    # a half-written list looks like a changed one -- so a rebuild would kick off nested
    # rebuilds of itself. Guard the reentrant call rather than blocking the notices, same
    # discipline as edit_target.py's execute().
    if getattr(_chunk, "_export_refreshing", False):
        return
    _chunk._export_refreshing = True
    try:
        # Carried across the resize, keyed by layer name: a rebuild is triggered by ANY
        # change to the stage's layer set, so without this a new layer appearing anywhere
        # silently reset every checkbox the user had already set on the layers that are
        # still there. A layer that has gone (or one that has just appeared) simply isn't
        # in this map and falls back to the defaults below.
        #
        # Remembered for the session, not only across this one rebuild: a layer can drop out
        # of the stage for a moment (a recook moves layers around) and come back on the next
        # refresh. Kept only from the rows still there, it came back with the defaults --
        # an Export the user had unchecked was checked again, a File Name was gone.
        kept = getattr(_chunk, "_export_row_settings", {})
        kept.update({
            rows.get_indexed_param(name_t, i).value: (
                rows.get_indexed_param(export_t, i).value,
                rows.get_indexed_param(bake_t, i).value,
                rows.get_indexed_param(file_t, i).value,
            )
            for i in range(rows.size)
        })
        _chunk._export_row_settings = kept

        # Only indices >= old_size get a genuinely new ChunkGroupBase (and therefore a
        # fresh export_enabled proxy) from ChunkListGroupBase.value_changed's row-creation
        # loop -- rows below that persist across the resize. Wiring the per-row callback
        # only for those new indices avoids piling up duplicate NoticeConnections on rows
        # that survive multiple refreshes.
        old_size = rows.size
        rows.size = len(layers)
        bake_all = _chunk.get_param("bake_all")
        bake_default = bool(bake_all.value) if bake_all is not None else False
        for i, (name, _layer) in enumerate(layers):
            prev_export, prev_bake, prev_file = kept.get(name, (True, bake_default, ""))
            rows.get_indexed_param(name_t, i).value = name
            rows.get_indexed_param(bake_t, i).value = prev_bake
            rows.get_indexed_param(file_t, i).value = prev_file
            export_row = rows.get_indexed_param(export_t, i)
            export_row.value = prev_export
            if i >= old_size:
                # add_callback must be called on this specific row's proxy, not the
                # export_t template -- a proxy's own value change never replays through
                # callbacks registered on the template (see _export_enabled_changed).
                export_row.add_callback(
                    _export_enabled_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED
                )
        _chunk._export_layer_names = new_names
    finally:
        _chunk._export_refreshing = False


def _row_values(_chunk, param_name: str) -> list[tuple[str, object]]:
    """(layer name, value of `param_name`) for every layer row."""
    rows = _chunk.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return []
    name_t, value_t = rows.get_param("layer_name"), rows.get_param(param_name)
    return [
        (rows.get_indexed_param(name_t, i).value, rows.get_indexed_param(value_t, i).value)
        for i in range(rows.size)
    ]


def _unchecked_layer_names(_chunk, checkbox_name: str) -> set:
    """Return the set of layer names whose row checkbox `checkbox_name` is unchecked."""
    return {name for name, checked in _row_values(_chunk, checkbox_name) if not checked}


def _read_file_names(_chunk) -> dict:
    """Layer name -> the File Name typed on its row, for the rows that have one."""
    return {name: given for name, value in _row_values(_chunk, "file_name") if (given := (value or "").strip())}


def _read_procedural_layer_names(_chunk) -> set:
    """Layer names whose "Bake chunk outputs" checkbox is unchecked (Procedural)."""
    return _unchecked_layer_names(_chunk, "bake")


def _read_export_disabled_layer_names(_chunk) -> set:
    """Layer names whose "Export" checkbox is unchecked -- these are skipped entirely
    by Export All Layers, not just baked differently.
    """
    return _unchecked_layer_names(_chunk, "export_enabled")


def _collect_internal_layers(layer, internal_ids: set, out: list) -> None:
    """Walk `layer` and the internal layers under it, strongest first, into `out`.

    Adds a content-only copy of each internal layer (its sublayer list cleared, so
    composing the copies never pulls a real file in), and every real sublayer path the walk
    meets, each where it stands -- so `out` keeps which content is stronger than which file.
    """
    copy = Sdf.Layer.CreateAnonymous()
    copy.TransferContent(layer)
    copy.subLayerPaths = []
    out.append(copy)
    for path in layer.subLayerPaths:
        if path not in internal_ids:
            out.append(path)
        elif (sub := Sdf.Layer.Find(path)) is not None:
            _collect_internal_layers(sub, internal_ids, out)


def _real_sublayers(layer, internal_ids: set) -> list:
    """layer's sublayer paths, each internal layer replaced by the real ones under it, in order.

    A Layer chunk sublayers its layer into its own cook layer, an internal one: the real
    layer it made is found beneath, and stays in the file as a sublayer where the internal
    layer stood.
    """
    real = []
    for path in layer.subLayerPaths:
        if path not in internal_ids:
            real.append(path)
        elif (sub := Sdf.Layer.Find(path)) is not None:
            real.extend(_real_sublayers(sub, internal_ids))
    return real


def _flatten_layers(layers: list) -> Sdf.Layer:
    """layers, strongest first, flattened into one: each one's opinions over the next's."""
    scratch = Sdf.Layer.CreateAnonymous()
    scratch.subLayerPaths = [layer.identifier for layer in layers]
    return UsdUtils.FlattenLayerStack(Usd.Stage.Open(scratch))


def _inline_internal_sublayers(dst_layer, internal_ids: set) -> list:
    """Replace any reference to a ChunkLayerManager-internal layer in dst_layer's own
    subLayerPaths with that layer's own content, inlined directly -- rather than dropping
    the reference outright.

    Internal layers (graph_layer / per-chunk procedural layers) are pure implementation
    detail with no meaning outside this session, so they were never meant to be their own
    output file -- but a chunk's own real driven content (e.g. an actual DiskLight prim)
    can live nested *inside* one, so simply dropping the reference would silently discard
    real content along with it. Sibling references to other layers being exported for real (light.usda, etc.) are
    untouched here -- those get rewritten to the sibling filename separately, not inlined.

    A real file an internal layer sublayers (a Sublayer chunk's path, a Layer chunk's
    layer) is not content to inline: it becomes a sublayer of dst_layer, where the internal
    layer stood. Flattening it in would pull the whole file into dst_layer, and turn its
    relative paths absolute.

    Only content stronger than every such file can be inlined: dst_layer's own opinions
    beat all its sublayers. Content weaker than one -- a model's chunks, upstream of the
    Layer chunk that made a look layer -- stays below it, in a layer of its own listed
    after it. Those are returned, strongest first; their identifiers stand in dst_layer's
    subLayerPaths, for the caller to write and point at.
    """
    # dst_layer's own content first (strongest), then every internal layer under it, in
    # its sublayer order -- flattened together, once. Flattened one internal layer at a
    # time, each copy replaced the root prims the last one wrote: with two (a chunkgroup
    # brings its own graph layer) a prim's purpose from one was lost under the other's /ball.
    own_content = Sdf.Layer.CreateAnonymous()
    own_content.TransferContent(dst_layer)
    own_content.subLayerPaths = []
    stream = [own_content]
    for path in dst_layer.subLayerPaths:
        if path not in internal_ids:
            stream.append(path)
        elif (internal_layer := Sdf.Layer.Find(path)) is not None:
            _collect_internal_layers(internal_layer, internal_ids, stream)

    # Runs of content between the real files, strongest first: the first is dst_layer's.
    runs, paths = [[]], []
    for item in stream:
        if isinstance(item, str):
            paths.append(item)
            runs.append([])
        else:
            runs[-1].append(item)
            if len(runs) > 1 and len(runs[-1]) == 1:
                paths.append(None)  # where this run's layer goes, once it has content

    if len(runs[0]) > 1:
        flat = _flatten_layers(runs[0])
        for name in list(dst_layer.rootPrims.keys()):
            del dst_layer.rootPrims[name]
        for prim_spec in flat.rootPrims.values():
            Sdf.CopySpec(flat, prim_spec.path, dst_layer, prim_spec.path)

    below = []
    weaker = iter(runs[1:])
    for i, path in enumerate(paths):
        if path is not None:
            continue
        run = next(r for r in weaker if r)
        flat = _flatten_layers(run)
        if flat.rootPrims:
            below.append(flat)
            paths[i] = flat.identifier
    dst_layer.subLayerPaths = [p for p in paths if p is not None]
    return below


def _strip_chunk_prims(flat_layer, only_paths=None) -> None:
    """Remove Chunk-typed prim specs from flat_layer, in place.

    Chunk-prim bookkeeping now follows Edit Target (see chunk_utils.get_bookkeeping_layer),
    so it's directly inside whichever real layer was active when each chunk last
    authored -- a real layer's export (or a full UsdUtils.FlattenLayerStack(stage)) can
    carry chunk-graph prims mixed in alongside real content. This turns that into
    "Baked": real content only, no chunk graph. The work is chunk_stage.strip_chunks,
    the same code scene.export() runs.

    only_paths, if given, restricts stripping to those specific prim paths -- used by
    "Based on Settings" to strip only chunks whose target layer is marked Baked,
    leaving Procedural-marked chunks' bookkeeping in place. Otherwise every Chunk-typed
    prim found in flat_layer is stripped.
    """
    chunk_stage.strip_chunks(flat_layer, only_paths)


def _baked_chunk_prim_paths(_chunk, stage) -> list:
    """Chunk prim paths (get_chunk_prim_path) whose current target layer's row in
    Layers to Export is checked (Bake chunk outputs; off by default) -- used by "Based on
    Settings" to strip those chunks' bookkeeping from a full flatten, leaving
    Procedural-marked chunks' bookkeeping in place (it's already there, wherever that
    chunk's Edit Target put it -- see chunk_utils.get_bookkeeping_layer).
    """
    procedural_names = _read_procedural_layer_names(_chunk)
    layer_name_by_id = {layer.identifier: name for name, layer in _discover_export_layers(stage)}
    scene = chunk_registry.ChunkRegistry().scene
    lm = chunk_registry.ChunkLayerManager()

    paths = []
    for c in scene.get_chunks():
        target = lm.get_chunk_target(c.get_hash())
        target_name = layer_name_by_id.get(target.identifier) if target else None
        if target_name is not None and target_name not in procedural_names:
            paths.append(get_chunk_prim_path(c))
    return paths


def _export_flattened(_chunk, stage) -> None:
    """Export the whole stage merged into a single file (the "Flatten into one file"
    path), per Mode:
    - Procedural: only the chunk graph, no driven content. Chunk-prim bookkeeping now
      follows Edit Target, so it's scattered across whichever real layers were active
      when each chunk last authored -- the session layer alone no longer holds the
      complete graph, so this delegates to the same rebuild-from-scratch mechanism as
      the Export ChunkGroup chunk (chunk_stage.export_chunks) instead.
    - Baked: a full flatten with every Chunk-typed prim stripped back out (a real
      layer's own composed content can carry chunk-graph prims mixed in with real
      content now -- see _strip_chunk_prims).
    - Based on Settings: a full flatten with only the Baked-marked chunks' bookkeeping
      stripped out -- Procedural-marked chunks' bookkeeping is already there, wherever
      their Edit Target put it, so nothing needs to be added back in. Unlike the
      per-layer path's Procedural (recipe only, real content left in its own private
      layer, not copied in), a Procedural-marked chunk's real content is still present
      here too -- FlattenLayerStack composes everything in regardless, and there's no
      per-chunk manifest of "which prims are this chunk's real content" to selectively
      exclude just theirs from one shared flat layer.
    """
    is_s = chunk.is_param_t(str)
    output_path = _chunk.get_param("output_file", is_s).value.strip()
    if not output_path:
        print("[ExportUSD] No output file set.")
        return

    _, ext = os.path.splitext(output_path)
    if ext.lower() not in (".usd", ".usda", ".usdc", ".usdz"):
        output_path += ".usda"

    if _would_overwrite_network(output_path, _network_files(stage), "the flattened stage"):
        return

    mode = _chunk.get_param("export_mode", is_s).value
    if mode == "Procedural":
        scene = chunk_registry.ChunkRegistry().scene
        chunk_stage.export_chunks(scene, output_path)
        print(f"[ExportUSD] Chunk graph (Procedural) exported to {output_path}")
        return

    flat = UsdUtils.FlattenLayerStack(stage)
    if mode == "Based on Settings":
        _strip_chunk_prims(flat, only_paths=_baked_chunk_prim_paths(_chunk, stage))
    else:
        _strip_chunk_prims(flat)
    chunk_stage.stamp_stage_metadata(flat, stage)
    _write(_chunk, flat, output_path)
    print(f"[ExportUSD] Stage exported to {output_path} ({mode})")


def _write(_chunk, layer, path) -> None:
    """Write layer to path, as the host's scene has it; with Relative Paths, the files it
    uses named relative to path (chunk_utils.relative_to_file). A chunk authors them
    absolute, so written as they are they named where they sat on this machine."""
    out = chunk_utils.export_layer(layer)
    relative = _chunk.get_param("relative_paths")
    if relative is None or relative.value:
        chunk_utils.relative_to_file(out, path)
    out.Export(path)


def commit(node):
    """Export every real layer in the composed stack to its own file, named after
    the layer's own display name (e.g. an anon layer created with tag "MyEdits"
    exports as "MyEdits.usda") -- or, if Flatten into one file is checked, merge
    everything into a single file instead (see _export_flattened).

    This is the chunk's reserved commit() hook -- the write-to-disk counterpart to
    execute(), run once by chunk_scheduler.commit_all after the DAG solve has settled
    (`usdchunks run` drives it). This chunk has no execute() at all: exporting is not a
    cook, it is the one thing this chunk exists to do, and doing it on every param touch
    would rewrite the output directory continuously. The Export button calls straight
    into here rather than keeping its own copy, so the GUI and headless paths cannot
    drift apart.

    Anon layer identifiers (anon:0x...:tag) only resolve within this session -- Export()
    copies subLayerPaths verbatim, so without rewriting them the output files would
    reference sublayers that don't exist once the session is gone. Every cross-reference
    between the layers we're exporting gets rewritten to the sibling filename we assign
    it below; references to the chunk graph's own internal layers (excluded from export
    entirely -- see chunk_internal_layer_ids) are dropped rather than left dangling.

    Layers to Export lists every real layer currently in the stage, each with an Export
    checkbox (checked by default) and a Bake chunk outputs checkbox (unchecked by
    default; the Bake all switch flips every row at once). Export
    unchecked skips that layer entirely -- it isn't written, and any other exported
    layer that sublayers it has that reference dropped, not inlined. Bake chunk outputs
    checked brings each chunk's real content in from its own private layer and
    strips the chunk-graph bookkeeping prims (chunk-prim bookkeeping follows Edit
    Target now, so it can be directly inside this layer's own composed content -- see
    chunk_utils.get_bookkeeping_layer) -- the baked result. Unchecked (Procedural)
    exports the bookkeeping only, leaving each chunk's real content where it already
    is (its own private layer, not copied in) -- a re-editable recipe; reloading it
    re-runs execute() to regenerate the content, rather than the file already holding
    a baked-out copy.
    """
    _chunk = node
    stage = _chunk.get_stage() if _chunk else None
    if not stage:
        return

    if _chunk.get_param("flatten", chunk.is_param_t(bool)).value:
        _export_flattened(_chunk, stage)
        return

    is_s = chunk.is_param_t(str)
    output_dir = _chunk.get_param("output_dir", is_s).value.strip()
    if not output_dir:
        print("[ExportUSD] No output directory set.")
        return
    os.makedirs(output_dir, exist_ok=True)

    ext = _chunk.get_param("layer_format", is_s).value or ".usda"

    internal_ids = chunk_utils.chunk_internal_layer_ids()
    session_layer = stage.GetSessionLayer()

    disabled_names = _read_export_disabled_layer_names(_chunk)
    procedural_names = _read_procedural_layer_names(_chunk)
    discovered = _discover_export_layers(stage)
    real_layers = [(n, lyr) for n, lyr in discovered if n not in disabled_names]
    # A skipped layer that lives in a file (a department's published layer) is still
    # there to point at, so only the in-session ones have their references dropped.
    skipped_ids = {lyr.identifier for n, lyr in discovered if n in disabled_names and lyr.anonymous}
    baked_names = {n for n, _l in discovered if n not in procedural_names}

    layers = []
    # Only where the host counts it as the user's: Houdini's session layer is its own.
    if (_chunk.get_param("keep_session_layer", chunk.is_param_t(bool)).value
            and any(lyr.identifier == session_layer.identifier for _n, lyr in chunk_utils.user_layers(stage))):
        layers.append(("SessionLayer", session_layer))
    layers.extend(real_layers)

    # Assign every exported layer its output filename up front, so cross-references
    # between them can be rewritten regardless of which order they're processed in.
    # A row's File Name, when set, replaces the layer's name; a USD extension in it wins
    # over Format, for that one file.
    given_names = _read_file_names(_chunk)
    filenames = {}
    used_names = set()
    for name, layer in layers:
        base, given_ext = os.path.splitext(given_names.get(name) or name or "layer")
        base = base or "layer"
        file_ext = given_ext if name in given_names and given_ext in _USD_EXTENSIONS else ext
        candidate = base
        i = 1
        while candidate in used_names:
            candidate = f"{base}_{i}"
            i += 1
        used_names.add(candidate)
        filenames[layer.identifier] = candidate + file_ext

    # A relative path is found from the file it is written in, so one written into another
    # folder than the network's is fitted to it, as saving the network elsewhere does: a
    # search path where the file is under that folder, else absolute -- which Relative Paths
    # then writes relative to the file (_write).
    out_folder = os.path.abspath(output_dir)
    # A file layer this export writes too is named by its new file, however it is spelled.
    exported_files = {_same_file(lyr.realPath): filenames[lyr.identifier] for _n, lyr in layers if lyr.realPath}

    def _relocate(path):
        if path in filenames:
            return filenames[path]
        if path in internal_ids or path in skipped_ids:
            return ""  # drop -- either has no meaning outside this session, or its Export
            # checkbox was unchecked, so it was never written as a file to reference
        found = asset_paths.resolve(path, stage)
        if found and _same_file(found) in exported_files:
            return exported_files[_same_file(found)]
        return asset_paths.refit(path, stage, out_folder, make_relative=False)

    networks = _network_files(stage)
    for name, layer in layers:
        out_path = os.path.join(output_dir, filenames[layer.identifier])
        if _would_overwrite_network(out_path, networks, f"layer '{name}'"):
            continue
        baked = name in baked_names

        # Rewritten on a throwaway copy -- never mutate the live session's own layers.
        # Always: a payload or reference to a layer the network made names it by its
        # session-only identifier, in either mode, and must point at its file instead.
        # Every file gets the stage's up-axis and units: any of them may be the one opened
        # on its own, and without them it reads as USD's global default rather than this
        # scene's. A sublayer's own, though, are its own: the stage never reads them.
        export_layer = Sdf.Layer.CreateAnonymous()
        export_layer.TransferContent(layer)
        chunk_stage.stamp_stage_metadata(export_layer, stage, keep_own=layer != stage.GetRootLayer())
        if baked:
            # Baked: bring each chunk's real content in from its own private layer
            # (only on choosing to bake does it get copied into this edit target),
            # then strip the now-redundant chunk-graph bookkeeping.
            below = _inline_internal_sublayers(export_layer, internal_ids)
            # Chunk content weaker than a file under this layer: each run its own file
            # beside it (model.cook.usda), listed where it stands.
            base, file_ext = os.path.splitext(filenames[layer.identifier])
            for i, run in enumerate(below):
                filenames[run.identifier] = f"{base}.cook{i or ''}{file_ext}"
            # Stripped while the arcs still name the session's layers: stripping opens a
            # stage on this copy, and relocated, an arc names a file beside one that is
            # not written yet, which that stage cannot open.
            _strip_chunk_prims(export_layer)
            UsdUtils.ModifyAssetPaths(export_layer, _relocate)
            for run in below:
                _strip_chunk_prims(run)
                UsdUtils.ModifyAssetPaths(run, _relocate)
                chunk_stage.stamp_stage_metadata(run, stage)
                run_path = os.path.join(output_dir, filenames[run.identifier])
                if _would_overwrite_network(run_path, networks, f"layer '{name}' chunk output"):
                    continue
                _write(_chunk, run, run_path)
                print(f"[ExportUSD] Layer '{name}' chunk output under its sublayers exported to {run_path}")
        else:
            # Procedural: the recipe only -- leave each chunk's real content where it
            # already lives (its own private layer), dropping the reference to it
            # here rather than inlining, via _relocate's internal_ids handling below.
            # A real layer under one (a Layer chunk's) is lifted out to stand in its place.
            export_layer.subLayerPaths = _real_sublayers(layer, internal_ids)
            UsdUtils.ModifyAssetPaths(export_layer, _relocate)

        _write(_chunk, export_layer, out_path)
        print(f"[ExportUSD] Layer '{name}' exported to {out_path}")


def export_all(owner, notice, sender):
    """Export button callback -- delegates to commit(), the same entry point
    `usdchunks run` uses, so pressing Export produces exactly the file the headless
    path would write.
    """
    commit(owner.get_chunk())


def _update_export_visibility(owner):
    """Toggle Output File/Mode (flatten path) vs Output Directory/Format (per-layer
    path) to match the Flatten into one file checkbox. Layers to Export stays visible
    either way -- Based on Settings (flatten) and per-layer Procedural both read it.
    """
    flatten = owner.get_param("flatten", chunk.is_param_t(bool)).value

    def _set_hidden(name, hidden):
        param = owner.get_param(name)
        if hidden:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)

    _set_hidden("output_file", not flatten)
    _set_hidden("export_mode", not flatten)
    _set_hidden("output_dir", flatten)
    _set_hidden("layer_format", flatten)
    _set_hidden("keep_session_layer", flatten)


def _flatten_changed(owner, notice, sender):
    _update_export_visibility(owner.get_chunk())


def _bake_all_changed(owner, notice, sender):
    """Set every row's Bake chunk outputs checkbox to the Bake all switch's value.
    Rows created later inherit it too -- _refresh_layer_rows seeds new rows from the
    switch rather than a hardcoded default.
    """
    _chunk = owner.get_chunk()
    rows = _chunk.get_param("export_layers", chunk.is_inst(chunk.ListGroup)) if _chunk else None
    if rows is None:
        return
    bake_t = rows.get_param("bake")
    for i in range(rows.size):
        rows.get_indexed_param(bake_t, i).value = bool(sender.value)


def _export_enabled_changed(owner, notice, sender):
    """Clear that row's Bake chunk outputs checkbox when its own Export checkbox is
    unchecked -- the layer isn't written at all, so there is nothing left to bake -- and
    restore it from the Bake all switch (the same seed _refresh_layer_rows gives a new
    row) when Export comes back on.

    Must be add_callback'd directly on each row's own export_enabled proxy (done in
    _refresh_layer_rows, once per new row) rather than on the export_enabled template --
    a proxy's own value change fires with sender=that exact proxy, and a callback
    registered on the template (sender=template) never matches it; only template-level
    changes (name, label, flags, ...) replay down to every row. sender.get_owner() is
    that same row, letting this reach only its sibling fields.

    Writes the sibling's .value, NOT its DISABLED state: state is a group-level flag
    that a ProxyParam forwards to the shared template (only .value is per-row -- same
    reason layer_name is disabled once on the template in main()), so graying out "this
    row's" Bake box actually grayed out every row's, and which way the column ended up
    came down to whichever row's callback happened to fire last.
    """
    if chunk_notice._loading_stage is not None:
        # Restoring a saved Export checkbox is not the user ticking it: resetting Bake from
        # Bake all here threw away the Bake the scene was saved with.
        return
    row = sender.get_owner()
    if row is None:
        return
    bake = row.get_param("bake")
    if bake is None:
        return
    if sender.value:
        _chunk = owner.get_chunk()
        bake_all = _chunk.get_param("bake_all") if _chunk else None
        bake.value = bool(bake_all.value) if bake_all is not None else False
    else:
        bake.value = False


def main():
    chunk_api = chunk.Chunk("Export USD")

    flatten = chunk.Checkbox(
        "flatten",
        "Flatten into one file",
        default_value=False,
        tooltip="Off (default): Export writes every real layer to Output Directory, one "
        "file per layer — the common case. On: Export merges everything into the single "
        "Output File instead, per Mode below.",
    )
    flatten.add_callback(_flatten_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)
    chunk_api.add_param(flatten)

    chunk_api.add_param(
        chunk.FileBrowser(
            "output_file",
            "Output File",
            extensions=["usd", "usda", "usdc", "usdz"],
            tooltip="Destination file when Flatten into one file is checked. The extension "
            "picks the format: .usda (text), .usdc (binary), .usd (either), or .usdz "
            "(package).",
        )
    )

    chunk_api.add_param(
        chunk.Menu(
            "export_mode",
            "Mode",
            menu_items=["Baked", "Procedural", "Based on Settings"],
            default_value="Baked",
            tooltip="Only shown when Flatten into one file is checked.\n\n"
            "Baked: every chunk's real driven content, merged into one file, no chunk "
            "graph at all.\n"
            "Procedural: only the chunk graph, no driven content — same rebuild-from-"
            "scratch mechanism as the Export ChunkGroup chunk, since chunk-prim bookkeeping "
            "follows Edit Target now and is no longer all in one place to export directly. "
            "Reloading it regenerates everything.\n"
            "Based on Settings: Baked, but leaving in the bookkeeping of chunks whose "
            "target layer is marked Procedural in Layers to Export below (it's already "
            "there, wherever their Edit Target put it).",
        )
    )

    output_dir = chunk.FileBrowser(
        "output_dir",
        "Output Directory",
        tooltip="Folder to export every real layer into, one file per layer, named after "
        "each layer's own name (e.g. a Layer chunk's Layer Name). Includes the root "
        "layer under its own file name — pick a folder that won't collide with existing "
        "scene files unless that's what you want.\n\nShown when Flatten into one file is "
        "unchecked.",
    )
    output_dir.set_flag(chunk.ChunkFlags.ParamFlags.DIRECTORY)
    chunk_api.add_param(output_dir)
    chunk_api.add_param(
        chunk.Menu(
            "layer_format",
            "Format",
            menu_items=[".usda", ".usdc", ".usd"],
            default_value=".usda",
            tooltip="File extension used for every exported layer. Shown when Flatten "
            "into one file is unchecked.",
        )
    )
    chunk_api.add_param(
        chunk.Checkbox(
            "keep_session_layer",
            "Keep Session Layer",
            default_value=True,
            tooltip="Checked (default): also write SessionLayer.usda. Chunk-prim bookkeeping "
            "follows Edit Target now, so this is only whatever wasn't redirected (CONTEXT "
            "chunks like Edit Target itself, plus anything authored before any Edit Target "
            "ran) — not the complete chunk graph; use the Export ChunkGroup chunk for that. "
            "Unchecked: skip it — write only the real, driven-content layers.\n\nShown when "
            "Flatten into one file is unchecked.",
        )
    )
    chunk_api.add_param(
        chunk.Checkbox(
            "relative_paths",
            "Relative Paths",
            default_value=True,
            tooltip="Checked (default): each file is written with the files it uses (a "
            "shot's set sublayer, a texture) relative to where it is written, so the "
            "exported files and their inputs can move together. Unchecked: absolute paths "
            "are written as they are.",
        )
    )

    bake_all = chunk.Checkbox(
        "bake_all",
        "Bake all",
        default_value=False,
        tooltip="Master switch for the per-layer Bake chunk outputs checkboxes below: "
        "toggling it sets every row at once, and newly discovered layers default to it.",
    )
    bake_all.add_callback(_bake_all_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)
    chunk_api.add_param(bake_all)

    export_layers = chunk.ListGroup(
        "export_layers",
        "Layers to Export",
        # Drop the LIST flag (ListGroup's default) so no "+"/"-" controls are shown --
        # this list is entirely auto-managed from the stage, never user-added-to/removed-from.
        flags=chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED,
        tooltip="Every real layer currently in the stage (one row per Edit Target — the "
        "output of Export All Layers), refreshed automatically as the stage changes.\n\n"
        "Export (checked by default): whether this layer is exported at all. Unchecked "
        "skips it entirely and clears Bake chunk outputs below.\n\n"
        "Bake chunk outputs (unchecked by default — use Bake all above to flip every "
        "row): checked brings each chunk's real content in "
        "from its own private layer and strips the chunk-graph bookkeeping prims — the "
        "baked result. Unchecked (Procedural): bookkeeping only, leaving each chunk's "
        "real content where it already is (not copied in) — a re-editable recipe; "
        "reloading it re-runs execute() to regenerate the content. To keep some chunks "
        "baked and others procedural within what would otherwise be one layer, route "
        "them through two separate Edit Target layers instead.",
    )
    layer_name = chunk.LineEdit(
        "layer_name",
        "Layer",
        # Every row would otherwise show the same static "Layer" caption above its
        # actual value (ProxyParam only lets .value differ per row, not the label) --
        # pure clutter since the value below it already says which layer this is.
        # Unset LABELED entirely rather than passing an empty label: get_label() falls
        # back to the param's internal name ("layer_name") whenever label is falsy.
        flags=chunk.ChunkFlags.ParamFlags.NONE,
    )
    # chunk.Label displays its static configured caption text, not its value -- the wrong
    # type for a dynamic per-row name. LineEdit shows .value correctly; DISABLED (a group
    # flag, so it applies to every row's proxy, unlike .value which is per-row) makes it
    # read-only so it doesn't look like something the user should type into.
    layer_name.set_state(chunk.ChunkFlags.State.DISABLED)
    export_layers.add_param(layer_name)

    export_enabled = chunk.Checkbox(
        "export_enabled",
        "Export",
        default_value=True,
        tooltip="Checked (default): this layer is written as its own file. Unchecked: "
        "skip it entirely — any other exported layer that sublayers it has that "
        "reference dropped, not inlined, so its content isn't written anywhere. A layer "
        "loaded from a file is the exception: it stays referenced by its own path. Bake "
        "chunk outputs below is cleared while this is unchecked, since it has "
        "nothing to apply to, and comes back from Bake all when this is re-checked.",
    )
    # Not wired here: a callback registered on this template never fires for a row's
    # own value change (see _export_enabled_changed's docstring). Each row's own proxy
    # gets add_callback'd individually, once, in _refresh_layer_rows.
    export_layers.add_param(export_enabled)

    export_layers.add_param(chunk.Checkbox("bake", "Bake chunk outputs", default_value=False))
    export_layers.add_param(
        chunk.LineEdit(
            "file_name",
            "File Name",
            tooltip="The file this layer is written to, in Output Directory. Blank: the "
            "layer's own name. Without an extension, Format picks it.",
        )
    )
    chunk_api.add_param(export_layers)
    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    chunk_api.add_callback(
        _refresh_layer_rows,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
    )

    export_all_btn = chunk.Button(
        "export_all",
        "Export",
        tooltip="Unchecked Flatten (default): export every real layer in the composed stack "
        "(session layer, root layer, and every sublayer — excluding the chunk graph's own "
        "internal layers) to Output Directory, one file per layer named after that layer's "
        "own name.\n\n"
        "Each layer's own Bake chunk outputs checkbox (Layers to Export) decides whether "
        "its real content is baked in and the bookkeeping stripped (Baked), or "
        "the bookkeeping is exported alone with content left in its own private layer "
        "(Procedural, the default; Bake all flips every row).\n\n"
        "Checked Flatten: merge everything into the single Output File instead, per Mode.",
    )
    export_all_btn.add_callback(export_all)
    chunk_api.add_param(export_all_btn)

    _update_export_visibility(chunk_api)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        icon="download",
        description=(
            """
        Writes each layer of the scene to its own file, or flattens everything into one file.

        Exports every real layer in the composed stack to Output Directory, one file
        per layer, named after that layer's own name — e.g. a Layer chunk with
        Layer Name "MyEdits" exports as MyEdits.usda. Name collisions get a _1, _2...
        suffix. Format picks the extension used for all of them. This is the common
        case and needs no other settings — just pick Output Directory and press Export.

        It never writes over the network itself: opened from a file, the network is the
        stage's root layer and so one of the layers listed, but a target that is a file
        holding a chunk network is skipped, with a message saying what to change. So
        Output Directory can be the network's own folder.

        Relative Paths [relative_paths], on by default, writes the files each exported file
        uses (a shot's set sublayer, a texture) relative to where it is written, so the
        files and their inputs can move together; off, absolute paths stay absolute.

        Check Flatten into one file to merge everything into a single Output File
        instead, per Mode: Baked (default) is every chunk's real driven content merged
        into one file, no chunk graph. Procedural is just the chunk graph, no driven
        content, via the same rebuild-from-scratch mechanism as the Export ChunkGroup
        chunk (chunk-prim bookkeeping follows Edit Target now, so it's no longer all in
        the session layer to export directly); reloading it regenerates everything by
        re-running each chunk's execute(). Based on Settings is Baked, but leaving in
        the bookkeeping of chunks whose target layer is marked Procedural in Layers to
        Export below.

        Layers to Export lists every real layer in the stage (refreshed automatically),
        each with an Export checkbox and a Bake chunk outputs checkbox (off by default;
        the Bake all switch sets every row at once). Export unchecked
        skips that layer entirely. Bake chunk outputs checked brings each
        bookkeeping — the baked result. Unchecked (Procedural) exports the bookkeeping
        only, leaving each chunk's real content where it already is (not copied in) —
        a re-editable recipe; reloading it re-runs execute() to regenerate the content.

        Pressing Export is not the only way to run this: the same export happens
        headlessly via this chunk's commit() hook, so `usdchunks run` on a network
        containing an Export USD chunk writes exactly the same files.
        """
        ),
    )
