from __future__ import annotations
import weakref

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk, chunk_notice, chunk_registry, chunk_utils

#: Place Below's default: the layer goes in this chunk's own layer, placed by the wiring.
_WIRED = "(Wired)"


def _file_sublayers(node, target) -> list[str]:
    """target's own sublayer paths, as written: the files it lists, not the chunks' layers.

    What Place Below offers. Chunk layers follow the wiring; these are what a file brought,
    with no chunk to be wired against -- a shot opened with fx, anim and layout in it.
    This chunk's own layer is left out.
    """
    internal_ids = chunk_utils.chunk_internal_layer_ids()
    own = node.get_param("_new_layer_id").value
    return [p for p in target.subLayerPaths if p not in internal_ids and p != own]


# Outside the chunk's edit context: this only refreshes a menu, and resolving the context
# makes or moves the chunk's layer -- a sublayer edit, from inside the stage's change notice,
# which a cook stage of the same layers hears before the change it answers (USD refuses it).
@chunk_notice.without_edit_context
def _refresh_place_menu(owner, notice, sender):
    """Keep Place Below listing the target's file sublayers.

    Only the items: a picked one that is not listed is kept, since the file may not be in
    the stack yet (an upstream chunk still to cook). And only when they change -- the write
    re-runs this chunk, and this runs for every Layer chunk on every stage change.
    """
    node = owner.get_chunk()
    stage = node.get_stage() if node is not None else None
    menu = node.get_param("place_below") if node is not None else None
    if stage is None or menu is None or node.is_blocked():
        return
    items = [_WIRED] + _file_sublayers(node, chunk_utils.get_edit_layer(stage, node))
    if list(getattr(menu, "menu_items", [])) != items:
        menu.menu_items = items


def _is_on(node, stage) -> bool:
    """Whether this chunk's output is part of the stage now: not deleted, disabled or gated off.

    The engine switches a chunk off by muting its own layer. A layer placed below a file is
    not in it, so this chunk mutes that one itself, the way Mute Layer holds its mutes.
    """
    if getattr(node, "_deleting", False) or node.is_blocked():
        return False
    if node.has_state(chunk.ChunkFlags.State.DISABLED):
        return False
    own = chunk_registry.ChunkLayerManager().get_layer(node)
    return own is None or not stage.IsLayerMuted(own.identifier)


def _sync_placed_mute(node, stage) -> None:
    """Mute the layer placed below a file while this chunk is off, and unmute it once it is on."""
    placed = getattr(node, "_placed_layer", None)
    if placed is None:
        return
    off = not _is_on(node, stage)
    if stage.IsLayerMuted(placed.identifier) != off:
        stage.MuteAndUnmuteLayers([placed.identifier] if off else [], [] if off else [placed.identifier])


def _watch_placement(node, stage) -> None:
    """Keep a placed layer's mute in step with the chunk, from the stage's own notices.

    The chunk's notices are held back while it is off, so a USD listener on the stage is the
    one signal that still arrives -- as for Mute Layer, whose watcher this follows: the
    listener holds the node weakly, the node holds the callback, and it mutes rather than
    edits a layer, which is safe from inside the notice. Taking the layer out when the chunk
    is deleted is the layer manager's (add_outside_layer).
    """
    if getattr(node, "_place_watch_stage", None) is stage:
        return
    ref = weakref.ref(node)

    def changed(_notice, _sender):
        owner = ref()
        if owner is not None:
            _sync_placed_mute(owner, stage)

    node._place_watch_callback = changed
    node._place_watch = Tf.Notice.Register(Usd.Notice.ObjectsChanged, changed, stage)
    node._place_watch_stage = stage


def _update_visibility(owner):
    # A Default Prim only means something for a layer that is not sublayered; where to place
    # it, only for one that is.
    sublayered = owner.get_param("sublayer").value
    for name, hidden in (("default_prim", sublayered), ("place_below", not sublayered)):
        param = owner.get_param(name)
        if hidden:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)


def _sublayer_changed(owner, notice, sender):
    _update_visibility(owner.get_chunk())


_USD_EXTENSIONS = (".usd", ".usda", ".usdc")


def _anon_tag(name: str) -> str:
    """The tag to make name's anonymous layer with.

    The name itself, unless it has a dot in it that is not a USD extension: an anonymous
    layer named Bottle.geom can be sublayered, but a reference or payload to it fails --
    the arc opens the layer by its identifier, which reads ".geom" as a file format. Tagged
    Bottle.geom.usda it opens, and Export USD writes it as Bottle.geom.usda.
    """
    if "." in name and not name.lower().endswith(_USD_EXTENSIONS):
        return name + ".usda"
    return name


def _author_default_prim(layer, name: str) -> None:
    """Define /name in layer and make it the default prim -- or clear both when name is empty.

    An arc to a layer needs a prim to target. Left empty until something authors into it
    through the arc, the payload or reference resolved to nothing, so nothing could. The
    prim is the layer's own, as Bottle_payload.usd's `def "Bottle"` is.
    """
    if not name:
        if layer.defaultPrim:
            layer.ClearDefaultPrim()
        return
    if not Sdf.Path.IsValidIdentifier(name):
        return
    spec = layer.GetPrimAtPath(f"/{name}") or Sdf.CreatePrimInLayer(layer, f"/{name}")
    spec.specifier = Sdf.SpecifierDef
    if layer.defaultPrim != name:
        layer.defaultPrim = name


def execute(node):
    """Find or (re-)create the chunk's anon layer, and sublayer it into this chunk's own layer.

    This chunk's layer is under the session layer by default, or under whichever layer an
    upstream Edit Target chunk has pointed at -- so a Layer downstream of an Edit Target
    nests its new layer inside that target. Among that target's sublayers it sits where
    this chunk sits in the wiring, as every chunk's output does: stronger than what is
    upstream of it, weaker than what is downstream.

    Anon layers don't survive a stage reload, so the stored identifier can go
    stale between sessions; if Find() misses, a fresh layer is created and the
    stored id is overwritten, matching the regenerate-on-load behavior other
    procedural chunks rely on.

    The anon layer's tag (Layer Name) is user-facing -- it's what shows up for this
    layer in e.g. Export USD's layer picker, or an Edit Target chunk's dropdown. Sdf
    identifiers are immutable once created, so renaming means creating a fresh anon
    layer under the new tag and transferring the old one's content across before
    swapping it into subLayerPaths.
    """
    # The id_param/name_param/parent_param writes below can reactively re-trigger this same chunk's execute()
    # before this call returns. We must NOT suppress that reactivity at the notice
    # level (e.g. via set_blocked()) -- that also blocks the notice the node-graph
    # widget needs to know it should repaint, silently breaking the UI. Guard the
    # reentrant *call* directly instead, so widgets still refresh normally.
    if getattr(node, "_create_layer_executing", False):
        return
    node._create_layer_executing = True
    try:
        id_param = node.get_param("_new_layer_id")
        name_param = node.get_param("_layer_name_used")
        parent_param = node.get_param("_parent_layer_id")
        desired_name = node.get_param("layer_name").value.strip() or node.name

        previous_id = id_param.value or None
        layer = Sdf.Layer.Find(previous_id) if previous_id else None

        if layer is None:
            layer = Sdf.Layer.CreateAnonymous(_anon_tag(desired_name))
        elif name_param.value != desired_name:
            renamed = Sdf.Layer.CreateAnonymous(_anon_tag(desired_name))
            renamed.TransferContent(layer)
            layer = renamed

        # Held by the chunk: the cook layer it is sublayered into is cleared before every cook,
        # and an anonymous layer nothing else holds dies with that -- taking whatever was
        # authored into it through an Edit Target with it.
        node._held_layer = layer

        lm = chunk_registry.ChunkLayerManager()
        if not node.get_param("sublayer").value:
            # Made for an asset to bring in by payload or reference: held by the layer
            # manager, which nothing else does until an arc composes it, and taken out of
            # wherever it was sublayered. Held first: in between, an Edit Target on it found
            # it in neither list and reset itself to the session layer.
            _author_default_prim(layer, node.get_param("default_prim").value.strip())
            lm.add_arc_layer(node, layer)
            node._placed_layer = None
            lm.add_outside_layer(node, None)
            if previous_parent_id := parent_param.value or None:
                if old_parent := Sdf.Layer.Find(previous_parent_id):
                    old_paths = [p for p in old_parent.subLayerPaths if p not in (previous_id, layer.identifier)]
                    if len(old_paths) != len(old_parent.subLayerPaths):
                        old_parent.subLayerPaths = old_paths
            id_param.value = layer.identifier
            name_param.value = desired_name
            parent_param.value = ""
            return

        # Into this chunk's own cook layer, like everything a chunk does: that layer sits among
        # the other chunks' by its place in the wiring, so the new layer is stronger than
        # everything upstream of this chunk and weaker than everything downstream. Sublayered
        # straight into the Edit Target's layer instead, it sat beside the graph's cook layers
        # as a whole and lost to an upstream chunk's output.
        own_layer = node.get_edit_context()
        if own_layer is None:
            return
        parent_layer, paths = own_layer, [layer.identifier]
        below = node.get_param("place_below").value
        if below and below != _WIRED:
            # Placed among the files the target lists, just under the one picked -- a shot
            # opened with fx, anim and layout, a layer slotted in under anim. There it is
            # weaker than all the chunks' output on that target, as those files are. Not
            # listed (yet), the layer stays where the wiring puts it.
            target = chunk_utils.get_edit_layer(node.get_stage(), node)
            others = [p for p in target.subLayerPaths if p not in (previous_id, layer.identifier)]
            if target.identifier not in (layer.identifier, previous_id) and below in others:
                others.insert(others.index(below) + 1, layer.identifier)
                parent_layer, paths = target, others
                if own_layer.subLayerPaths:
                    own_layer.subLayerPaths = []
        placed = parent_layer is not own_layer
        node._placed_layer = layer if placed else None
        lm.add_outside_layer(node, layer if placed else None, parent_layer if placed else None)
        if placed and (scene_stage := node.get_scene_stage()) is not None:
            _watch_placement(node, scene_stage)

        previous_parent_id = parent_param.value or None
        if previous_parent_id and previous_parent_id != parent_layer.identifier:
            # The cook layer moved since the last run (an upstream Edit Target changed, or one
            # was added later), or Place Below changed -- drop the stale entry from the OLD
            # parent so the layer doesn't end up sublayered twice.
            if old_parent := Sdf.Layer.Find(previous_parent_id):
                old_paths = [p for p in old_parent.subLayerPaths if p not in (previous_id, layer.identifier)]
                if len(old_paths) != len(old_parent.subLayerPaths):
                    old_parent.subLayerPaths = old_paths

        # Insert into the composed stack BEFORE updating id_param/name_param/parent_param
        # below: those value assignments can reactively re-execute downstream dependents
        # (e.g. an Edit Target chunk tracking this one), and if that happens before the
        # layer is actually part of the stack, SetEditTarget on it fails with "not in
        # the local LayerStack".
        if list(parent_layer.subLayerPaths) != paths:
            parent_layer.subLayerPaths = paths
        # No longer held for an arc -- dropped only now it is in the stack, so an Edit Target
        # on it finds it all the way through.
        lm.add_arc_layer(node, None)

        id_param.value = layer.identifier
        name_param.value = desired_name
        parent_param.value = parent_layer.identifier
    finally:
        node._create_layer_executing = False


def main():
    layerChunk = chunk.Chunk("Layer")

    layer_name = chunk.LineEdit(
        "layer_name", "Layer Name",
        tooltip="Display name/tag for the created layer (e.g. shown in an Edit Target chunk's "
        "dropdown, or Export USD's layer picker). Leave blank to use the chunk's own name. "
        "Changing this after creation renames the layer in place, carrying over anything "
        "already authored into it.",
    )

    # All three are bookkeeping for the layer this chunk owns *in this session*: two of them
    # hold an anonymous layer's identifier, which names an in-memory object and nothing else.
    # TRANSIENT keeps them off the chunk prim, so a saved scene does not come back holding the
    # address of a layer that died with the session that made it -- a fresh one is created on
    # reload, which is what the empty value means.
    new_layer_id = chunk.LineEdit("_new_layer_id", flags=chunk.ChunkFlags.ParamFlags.TRANSIENT)
    new_layer_id.set_state(chunk.ChunkFlags.State.HIDDEN)

    layer_name_used = chunk.LineEdit("_layer_name_used", flags=chunk.ChunkFlags.ParamFlags.TRANSIENT)
    layer_name_used.set_state(chunk.ChunkFlags.State.HIDDEN)

    parent_layer_id = chunk.LineEdit("_parent_layer_id", flags=chunk.ChunkFlags.ParamFlags.TRANSIENT)
    parent_layer_id.set_state(chunk.ChunkFlags.State.HIDDEN)

    sublayer = chunk.Checkbox(
        "sublayer", "Sublayer", default_value=True,
        tooltip="Checked (default): the layer is sublayered into the current Edit Target, "
        "stronger than what is upstream of this chunk and weaker than what is downstream. "
        "Unchecked: it is made but not sublayered -- an asset's layer for a Payload or "
        "Reference chunk to bring in (pick it in their Layer menu), and an Edit Target "
        "downstream of that to author into, the way Bottle.usd payloads Bottle_payload.usd.",
    )

    default_prim = chunk.LineEdit(
        "default_prim", "Default Prim",
        tooltip="Only when Sublayer is off: the layer's root prim, defined in it and set as "
        "its defaultPrim -- what a Payload or Reference to the layer brings in (\"Bottle\").",
    )

    sublayer.add_callback(_sublayer_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)

    place_below = chunk.Menu(
        "place_below", "Place Below",
        menu_items=[_WIRED], default_value=_WIRED,
        # The files are the target's as last seen, so a saved choice is set before they are
        # in the stack and listed.
        dynamic_items=True,
        tooltip="(Wired) (default): the layer is as strong as this chunk's place in the "
        "wiring -- stronger than what is upstream, weaker than what is downstream. Or one of "
        "the files the current Edit Target's layer sublayers (a shot opened with fx, anim "
        "and layout): the layer goes just under it, weaker than all the chunks' output on "
        "that layer, as those files are.",
    )

    layerChunk.add_params([
        layer_name,
        sublayer,
        default_prim,
        place_below,
        new_layer_id,
        layer_name_used,
        parent_layer_id,
    ])

    _update_visibility(layerChunk)

    layerChunk.add_callback(
        _refresh_place_menu,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
        send_on_create=True,
    )

    return layerChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_LAYER,
        icon="layers",
        description="""
        Adds a new layer for edits to go into, e.g. department layers like anim or layout.

        Layer chunk — authors a fresh anonymous layer and sublayers it into the
        session layer, or into whichever layer an upstream Edit Target chunk has
        explicitly pointed at — so chaining Layer → Edit Target → Layer
        nests each new layer inside the previous one instead of stacking them all as
        siblings under the session layer. Purely about creating/positioning the layer;
        use an Edit Target chunk downstream to actually redirect authoring into it.

        Layer Name sets the anon layer's tag — the human-readable name shown for it
        elsewhere (e.g. an Edit Target chunk's dropdown, or Export USD's layer picker);
        leave blank to use the chunk's own name.

        Where it lands follows the wiring, as every chunk's output does: the layer is
        sublayered into this chunk's own layer, so it is stronger than what is upstream
        of this chunk and weaker than what is downstream. If the effective parent
        changes between runs (e.g. an upstream Edit Target's selection changes), the
        stale entry in the old parent is cleaned up automatically.

        Place Below slots it in among the files the Edit Target's layer already
        sublayers instead -- a shot opened with fx, anim and layout, a layer just under
        anim. There it is weaker than all the chunks' output on that layer, as those
        files are.

        The created anonymous layer is idempotent across re-executions: its identifier
        is cached on the chunk, and each run repositions the same layer rather than
        creating a new one. Anonymous layers don't survive a stage reload, so on the
        next load a new one is created automatically, matching the regenerate-on-load
        behavior of other procedural chunks. Changing Layer Name after the layer
        already exists renames it in place — a new anon layer is created under the
        new tag, the old layer's content is transferred across, and it swaps into
        the stack in its place.
        """,
    )
