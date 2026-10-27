"""Mute Layer chunk -- mutes the layers you pick from a list or match with a pattern.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import weakref

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk, chunk_registry, chunk_utils

_NONE = "(none)"


def _layer_name(layer: Sdf.Layer) -> str:
    return layer.GetDisplayName() or layer.identifier


def _candidate_layers(stage: Usd.Stage) -> dict[str, str]:
    """Every layer this chunk may mute, as identifier -> name.

    That is what the stage uses, plus what is muted now. Muted layers drop out of GetUsedLayers, so they are added back. Otherwise a layer
    would leave the list the moment this chunk muted it. Left out: the root and session
    layers (USD cannot mute the root layer, and the session layer holds the chunk graph),
    and every layer the chunk graph owns (graph_* and per-chunk layers), as in Export USD.

    Plain strings, read right away: a layer handle from the stage is weak, and muting can
    free the layer behind it.
    """
    skip = {stage.GetRootLayer().identifier, stage.GetSessionLayer().identifier}
    muted = [Sdf.Layer.Find(i) for i in stage.GetMutedLayers()]
    used = [*stage.GetLayerStack(includeSessionLayers=True), *stage.GetUsedLayers(), *muted]
    layers: dict[str, str] = {}
    for layer in used:
        if not layer:
            continue  # gone, or an expired handle: a layer USD has freed since it was listed
        if layer.identifier in skip or layer.identifier in layers:
            continue
        if chunk_registry.is_chunk_internal_layer(layer):
            continue
        layers[layer.identifier] = _layer_name(layer)
    return layers


def _wanted(query: tuple[str, str], layers: dict[str, str]) -> set[str]:
    """The identifiers of the layers to mute.

    A layer is muted when it is the one picked in the list, or when one of the patterns
    matches its name or its identifier (the full path, for a file). The patterns are the
    same globs prim path fields take (chunk_utils.glob_to_regex), split the same way.
    """
    picked, pattern = query
    globs = [chunk_utils.glob_to_regex(g) for g in chunk_utils.split_patterns(pattern)]
    return {
        i for i, name in layers.items()
        if (picked != _NONE and name == picked)
        or any(rx.match(name) or rx.match(i) for rx in globs)
    }


def _is_active(node, stage: Usd.Stage) -> bool:
    """Whether this chunk should hold its mutes right now.

    Not when it is being deleted, disabled, or gated off by the view. Its mutes are stage
    state, not content in its own layer, so the engine muting its layer does not undo
    them. This chunk has to.

    Deleting is read from `_deleting`, which request_delete sets first. A chunk cannot
    hear its own CHUNK_DELETED: request_delete clears its callbacks before sending it. The
    teardown then edits the layer stack, and _watch_muting's listener sees that.
    """
    if getattr(node, "_deleting", False) or node.is_blocked():
        return False
    if node.has_state(chunk.ChunkFlags.State.DISABLED):
        return False
    own = chunk_registry.ChunkLayerManager().get_layer(node)
    return own is None or not stage.IsLayerMuted(own.identifier)


def _reconcile(node, query: tuple[str, str] | None = None) -> None:
    """Mute what this chunk wants muted and unmute what it muted before but no longer wants.

    Only touches layers this chunk muted itself, so it never unmutes a layer someone else
    muted. Every change goes through one MuteAndUnmuteLayers call: the stage recomposes once.

    Args:
        node: The Mute Layer chunk.
        query: (picked layer, patterns), read from the params. Only execute() passes it, and
            the listeners reuse the last one. They also run while the engine is still
            building the chunk, when its params read their defaults, and an empty pattern
            read then unmuted everything.
    """
    stage = node.get_scene_stage()
    if stage is None or getattr(node, "_mute_reconciling", False):
        return
    _watch_muting(node, stage)
    if query is not None:
        node._mute_query = query
    query = getattr(node, "_mute_query", None)
    if query is None:
        return  # not cooked yet, so nothing asked for
    node._mute_reconciling = True
    try:
        # identifier -> name, of the layers this chunk muted. Muted layers can drop out of
        # every list the stage keeps, so these keep them findable.
        held: dict[str, str] = getattr(node, "_muted_by_me", {})
        layers = _candidate_layers(stage)
        layers.update({i: name for i, name in held.items() if i not in layers})

        node._mute_was_active = _is_active(node, stage)
        wanted = _wanted(query, layers) if node._mute_was_active else set()
        mute = sorted(i for i in wanted if not stage.IsLayerMuted(i))
        unmute = sorted(i for i in held.keys() - wanted if stage.IsLayerMuted(i))
        # Taken before muting. An anonymous layer (a Layer chunk's) that nothing else holds
        # is freed once muted, and could then never be unmuted. A file layer needs no
        # holding: unmuting opens it again from disk.
        keep = getattr(node, "_mute_keepalive", {})
        keep.update({i: layer for i in mute if (layer := Sdf.Layer.Find(i)) and layer.anonymous})
        if mute or unmute:
            stage.MuteAndUnmuteLayers(mute, unmute)
        # Only what this chunk muted: a layer someone else had already muted is not ours
        # to unmute later.
        node._muted_by_me = {i: layers[i] for i in wanted if (i in held or i in mute) and stage.IsLayerMuted(i)}
        node._mute_keepalive = {i: layer for i, layer in keep.items() if i in node._muted_by_me}
        if getattr(node, "_deleting", False):
            node._mute_watch = None  # all unmuted: stop listening
            return
        _show_status(node, layers)
    finally:
        node._mute_reconciling = False


def _show_status(node, names: dict[str, str]) -> None:
    """Refresh the layer list and the read-only line saying what is muted."""
    picker = node.get_param("layer")
    status = node.get_param("muted")
    if picker is None or status is None:
        return
    items = list(dict.fromkeys([_NONE, *names.values()]))
    if picker.value not in items:
        # Kept, not reset: a saved pick names a layer that may only show up later in a load.
        items.append(picker.value)
    if list(picker.menu_items) != items:
        picker.menu_items = items
    text = ", ".join(sorted(node._muted_by_me.values())) or "nothing"
    if status.value != text:
        status.value = text


def _watch_muting(node, stage: Usd.Stage) -> None:
    """Reconcile when the chunk is switched off or on, or when the stage's layers change.

    The engine switches a chunk off (disabled, or gated off by the view) by muting its
    layer, and deleting one edits the layer stack. The chunk's own notices are held back
    while it is off, so a USD listener on the stage is the one signal that still arrives.
    A layer coming or going resyncs `/`, which is how a new layer that matches gets muted,
    also during a load. Any other change costs a few lookups.

    ObjectsChanged, not LayerMutingChanged: muting from inside a LayerMutingChanged notice
    crashes USD. USD also sends ObjectsChanged for a mute, and muting from there is safe.

    Same shape as skel_pose's watcher: the listener holds the node weakly, so it does not
    keep a deleted chunk alive, and the node holds the callback, because Tf does not.
    """
    if getattr(node, "_mute_watch_stage", None) is stage:
        return
    ref = weakref.ref(node)

    def changed(notice, _sender):
        owner = ref()
        if owner is None:
            return
        layers_changed = Sdf.Path.absoluteRootPath in notice.GetResyncedPaths()
        if layers_changed or _is_active(owner, stage) != getattr(owner, "_mute_was_active", None):
            _reconcile(owner)

    node._mute_watch_callback = changed
    node._mute_watch = Tf.Notice.Register(Usd.Notice.ObjectsChanged, changed, stage)
    node._mute_watch_stage = stage


def execute(node) -> None:
    picker, pattern = node.get_param("layer"), node.get_param("pattern")
    if picker is None or pattern is None:
        return  # the engine is still building the chunk
    _reconcile(node, (picker.value, pattern.value.strip()))


def _on_stage_changed(owner, notice, sender) -> None:
    """Start the stage watcher as soon as the chunk exists, and keep the list current.

    Listens globally, and send_on_create runs it once at creation. It matters for a chunk
    that is created switched off: it does not cook, so execute() never starts the watcher.
    """
    node = owner.get_chunk()
    if node is not None:
        _reconcile(node)


def main():
    chunk_api = chunk.Chunk("Mute Layer")

    chunk_api.add_param(
        chunk.Menu(
            "layer",
            "Layer",
            menu_items=[_NONE],
            default_value=_NONE,
            tooltip="Pick one layer to mute. The list is every layer on the stage, except "
            "the root layer, the session layer and the chunk graph's own layers.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "pattern",
            "Pattern",
            tooltip="Also mute every layer whose name or path matches, with the same patterns "
            "as prim paths: `*_cfx*` mutes every layer named with _cfx in it, `**/cfx/**` "
            "every layer in a cfx folder. `*` stays within one folder, `**` crosses them, `?` "
            "is one character. Separate several with spaces or commas. Case matters. Works "
            "together with Layer above.",
        )
    )
    muted = chunk.LineEdit(
        "muted",
        "Muted",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.TRANSIENT,
        tooltip="The layers this chunk mutes now.",
    )
    muted.set_state(chunk.ChunkFlags.State.DISABLED)
    chunk_api.add_param(muted)

    chunk_api.add_callback(
        _on_stage_changed,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
        send_on_create=True,
    )
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_LAYER,
        icon="layers",
        description="""
        Mute Layer chunk -- mutes layers on the stage.

        Pick one layer from Layer, or type a pattern in Pattern to mute every layer whose
        name or path it matches. Patterns work as in prim path fields: `*_cfx*` mutes every
        layer named with _cfx in it, and `**/cfx/**` every layer in a cfx folder. Several
        patterns go in one field, separated by spaces or commas. Both work at once. Muted
        shows what is muted now.

        The list holds every layer the stage uses, also ones brought in by a reference
        or payload. It leaves out the root layer, the session layer, and the chunk
        graph's own layers (graph_* and per-chunk layers).

        Muting is stage state, not an edit in a layer: it affects the whole stage, not
        just the chunks after this one, and it is not saved in any file. The chunk mutes
        again when the scene loads. Disable it, gate it off with the view, or delete it,
        and it unmutes what it muted. It never unmutes a layer it did not mute.
        """,
    )
