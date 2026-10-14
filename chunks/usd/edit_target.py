from __future__ import annotations
from pxr import Sdf

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_utils
from usd_chunks import chunk_stage

# Selecting this instead of a specific layer identifier follows whatever layer the
# upstream Layer chunk (found via the exec-chain dependency already wiring the
# two nodes together) currently owns -- so a rename downstream of it (which swaps in a
# new Sdf identifier) doesn't require coming back to manually re-pick the dropdown.
_FOLLOW_UPSTREAM = "(Follow Upstream Layer)"


def _find_upstream_create_layer(chunk_obj):
    """Return the nearest upstream chunk that owns a _new_layer_id param (i.e. a
    Layer chunk), found via the exec-chain dependency graph -- the same
    connection that already sequences the two nodes -- or None if there isn't one.

    Exec edges only, which is why this asks chunk_stage.exec_dependencies rather than
    chunk_obj.get_dependencies(): the Python DAG also mirrors param wires, and a param
    wire means a value flows, not that one chunk precedes another. Following one here
    would let a Layer wired for some unrelated param silently decide where every
    downstream chunk authors -- and get_dependencies() returns both kinds in no
    particular order, so with both present it would be a coin toss which won.

    A Layer whose Layer Name port *is* wired into this chunk needs no walk at
    all: the param already holds the layer name, which is what _resolve_layer resolves.
    """
    for dep in chunk_stage.exec_dependencies(chunk_obj):
        if dep is not None and dep.get_param("_new_layer_id") is not None:
            return dep
    return None


def _resolve_layer(stage, selection):
    """Resolve `selection` to a layer that's actually part of stage's current layer
    stack -- normally a plain display name (the dropdown-picked/stored case, and also
    what a node-graph connection to a Layer chunk's Layer Name port feeds in
    live), matched via GetDisplayName() so it keeps resolving correctly across a
    rename *and* across a save/reload (unlike a raw Sdf identifier -- see below).
    Falls back to an exact identifier match for the rare item with no display name
    (e.g. an anonymous root layer, listed by identifier in _refresh_edit_target).

    Deliberately does *not* fall back to Sdf.Layer.Find(selection): that searches
    USD's whole process-global layer registry, not just this stage, so it can return
    a layer that's technically still alive somewhere (e.g. referenced by a different,
    unrelated stage, or orphaned after being removed from this one) but not actually
    in stage's local layer stack -- stage.SetEditTarget() on such a layer raises
    ("Layer ... is not in the local LayerStack"). Only ever matching against
    stage.GetLayerStack() guarantees whatever this returns is safe to set as an edit
    target right now.

    `selection` may also be a *stale* anon: identifier -- from a session saved before
    this function stored display names instead, or a stray manual edit -- left with no
    live layer of that exact identifier, since anon layers never survive a save/reload
    (a fresh random hex address every time). Extract its tag ("anon:<hex>:<tag>" ->
    "<tag>") and match on that too, so an old save still resolves once the layer with
    that name exists again (e.g. a downstream Layer chunk recreating it,
    possibly later in the same load if it executes after this chunk).
    """
    tag = selection.rsplit(":", 1)[-1] if selection.startswith("anon:") else selection
    for name, layer in _targetable_layers(stage):
        if layer.identifier == selection or name in (selection, tag):
            return layer
    return None


def _targetable_layers(stage):
    """The stage's layers, then the ones a Layer chunk made for an asset to bring in by arc.

    Those are not in the layer stack: authored through the payload or reference that
    brings them in (chunk_utils.arc_edit_target), so a network can fill Bottle.geom.
    """
    lm = chunk_registry.ChunkLayerManager()
    arc = lm.arc_layers() if lm.initialized else []
    return list(chunk_utils.user_layers(stage)) + arc


# Outside the chunk's edit context: this only refreshes a menu, and resolving the context
# makes or moves the chunk's layer -- a sublayer edit, from inside the stage's change notice,
# which a cook stage of the same layers hears before the change it answers (USD refuses it).
@chunk_notice.without_edit_context
def _refresh_edit_target(owner, notice, sender):
    """Rebuild the edit-target menu from the current stage's layer stack.

    Registered with listen_globally=True -- safe with any number of Edit Target
    instances since NoticeConnection now shares one real Tf.Notice.RegisterGlobally()
    per alias across all of them (see chunk_notice._register_global).
    """
    _chunk = owner.get_chunk()
    stage = _chunk.get_stage() if _chunk else None
    if not stage:
        return
    if _chunk.is_blocked():
        # Same reasoning as layer.py's _refresh_after_layer: this is
        # listen_globally=True, so it fires for EVERY Edit Target instance on ANY
        # stage content change. A blocked chunk (not reachable from any view yet, or
        # genuinely hidden) can't usefully act on a fresh menu -- skip the work below
        # rather than pay for it on every sibling's edit.
        return

    internal_ids = chunk_utils.chunk_internal_layer_ids()
    session_id = stage.GetSessionLayer().identifier

    # includeSessionLayers=True so sublayers authored under the session layer (e.g.
    # by a Layer chunk) are included too; the session layer itself is
    # represented by the friendly "Session Layer" entry instead of its raw identifier.
    #
    # Listed (and stored, once picked) by display name, not raw Sdf identifier: an
    # anon: identifier's hex address is regenerated every time that layer is recreated
    # (a fresh session, or a downstream Layer chunk re-running on reload) -- an
    # exact identifier saved to disk can never resolve again, whereas the display name
    # (the tag a Layer chunk gave it) is stable across all of that, and
    # _resolve_layer already matches on it. Falls back to the identifier only for a
    # layer with no display name at all (e.g. an anonymous root layer).
    target_items = ["Session Layer"] + [
        name for name, layer in _targetable_layers(stage)
        if layer.identifier != session_id and layer.identifier not in internal_ids
    ]
    if _find_upstream_create_layer(_chunk) is not None:
        target_items.insert(0, _FOLLOW_UPSTREAM)

    target_menu = _chunk.get_param("edit_target")
    if target_menu is None:
        # Deleting this chunk tears its params down, but the layer-stack edits that teardown
        # makes are themselves stage content changes -- so this global handler still fires for
        # the half-dismantled chunk. Nothing left to refresh.
        return
    # Only assign when the items actually changed -- this handler fires on essentially
    # every authoring op, and reassigning identical menu_items itself authors attr
    # changes that re-fire this same notice (feedback churn).
    if list(getattr(target_menu, "menu_items", [])) != target_items:
        target_menu.menu_items = target_items
    # A connected value (e.g. a Layer chunk's Layer Name wired straight in)
    # is a plain name, not one of these menu items/sentinels -- don't stomp it.
    prop = target_menu.get_usd_prop()
    is_connected = bool(prop and prop.GetConnections())
    if not is_connected and target_menu.value not in target_items:
        # Not literally one of the current menu items -- but it may still be a stale
        # anon: identifier that _resolve_layer can match by tag (e.g. a downstream
        # Layer chunk that hasn't (re-)created its layer yet at this exact
        # moment during load, but will later in the same load). Only actually give up
        # and reset to the default once it's genuinely unresolvable -- resetting
        # unconditionally here would permanently overwrite the stored selection with
        # "Session Layer" before that chunk ever gets a chance to run.
        #
        # During a bulk load specifically, this global handler fires on every stage
        # edit from every chunk being restored, so "unresolved so far" is a normal,
        # transient state there (the layer shows up moments later, from a chunk still
        # ahead in the same load) rather than proof the selection is genuinely gone.
        # Resetting on that transient reading would silently revert chained Edit
        # Target chunks to "Session Layer" on reload, depending on whether each was
        # checked before or after its own upstream layer existed -- so skip the reset
        # entirely while loading and let a later refresh pass, triggered by that same
        # layer's own creation, resolve it correctly instead.
        if chunk_notice._loading_stage:
            return
        if target_menu.value == _FOLLOW_UPSTREAM or not _resolve_layer(stage, target_menu.value):
            target_menu.value = target_items[0]


def execute(node):
    # menu_items/value assignments above (and target_layer's own attr writes below)
    # can reactively re-trigger this same chunk's execute() before this call returns
    # (e.g. via another chunk's refresh cascading back here). We must NOT suppress
    # that reactivity at the notice level -- chunk_utils.suppress_reexecution's
    # set_blocked() would also block the notice the node-graph widget needs to know
    # it should repaint, silently breaking the UI. Guard the
    # reentrant *call* directly instead, so widgets still refresh normally.
    if getattr(node, "_edit_target_executing", False):
        return
    node._edit_target_executing = True
    try:
        stage = node.get_stage()

        selection = node.get_param("edit_target").value
        if selection == _FOLLOW_UPSTREAM:
            upstream = _find_upstream_create_layer(node)
            layer_id = upstream.get_param("_new_layer_id").value if upstream else None
            target_layer = Sdf.Layer.Find(layer_id) if layer_id else None
        elif selection == "Session Layer":
            target_layer = stage.GetSessionLayer()
        else:
            target_layer = _resolve_layer(stage, selection)

        if target_layer:
            # Publish the resolved layer onto this chunk's prim and re-home everything
            # downstream to it (chunk_stage.publish_edit_target -> reconcile_edit_targets).
            # There's no stage.SetEditTarget here: the effective target is resolved per
            # chunk from graph topology (nearest upstream Edit Target), not from the
            # stage's ambient edit target, which the exec dispatch loop rewrites on every
            # run anyway. Every chunk downstream of this one -- its own bookkeeping
            # (inputs:/outputs:/exec connections, node position) and its driven content --
            # lands in target_layer, until another Edit Target chunk downstream changes it.
            chunk_stage.publish_edit_target(node, stage, target_layer)
    finally:
        node._edit_target_executing = False


def main():
    editTargetChunk = chunk.Chunk("Edit Target")

    edit_target = chunk.Menu(
        "edit_target", "Edit Target",
        menu_items=["Session Layer"], default_value="Session Layer",
        # The items are the stage's layers as last seen, so a saved choice is set before the
        # layer it names is listed; _refresh_edit_target drops it if it never resolves.
        dynamic_items=True,
        tooltip="The layer to make the stage's current edit target. Kept in sync "
        "automatically as the stage's layer stack changes. If a Layer chunk "
        "feeds this one directly, \"" + _FOLLOW_UPSTREAM + "\" appears — pick it to "
        "track that chunk's layer live, so renaming it there doesn't require coming "
        "back to re-pick this dropdown.",
    )
    editTargetChunk.add_param(edit_target)

    editTargetChunk.add_callback(
        _refresh_edit_target,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
        send_on_create=True,
    )

    return editTargetChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_LAYER,
        description="""
        Edit Target chunk — sets the stage's current edit target, mirroring usdview's
        Edit Target dropdown as a chunk.

        Edit Target lists Session Layer, the root layer, and any sublayer — including
        one authored by a Layer chunk elsewhere in the graph — and refreshes
        itself automatically whenever the stage's layer stack changes, so there's no
        manual refresh step.

        When a Layer chunk feeds this one via the exec chain, an extra
        "(Follow Upstream Layer)" entry appears. Unlike picking that chunk's
        layer by its (frozen) identifier, this tracks whatever layer it currently
        owns — so renaming it later doesn't leave this dropdown pointed at a stale,
        dead identifier that has to be manually re-picked.

        The chunk graph's own internal layers (the "chunks" root layer and every
        per-graph / per-chunk procedural layer nested under it) are excluded — they're
        implementation details Clear()ed on every execute() and were never meant to
        be a manual edit target.
        """,
    )
