from __future__ import annotations
from pxr import Sdf

from usd_chunks import chunk, chunk_registry



def _update_visibility(owner):
    # A Default Prim only means something for a layer that is not sublayered.
    default_prim = owner.get_param("default_prim")
    if owner.get_param("sublayer").value:
        default_prim.set_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        default_prim.unset_state(chunk.ChunkFlags.State.HIDDEN)


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
        parent_layer = node.get_edit_context()
        if parent_layer is None:
            return
        previous_parent_id = parent_param.value or None
        if previous_parent_id and previous_parent_id != parent_layer.identifier:
            # The cook layer moved since the last run (an upstream Edit Target changed, or one
            # was added later) -- drop the stale entry from the OLD one so the layer doesn't
            # end up sublayered twice.
            if old_parent := Sdf.Layer.Find(previous_parent_id):
                old_paths = [p for p in old_parent.subLayerPaths if p not in (previous_id, layer.identifier)]
                if len(old_paths) != len(old_parent.subLayerPaths):
                    old_parent.subLayerPaths = old_paths

        # Insert into the composed stack BEFORE updating id_param/name_param/parent_param
        # below: those value assignments can reactively re-execute downstream dependents
        # (e.g. an Edit Target chunk tracking this one), and if that happens before the
        # layer is actually part of the stack, SetEditTarget on it fails with "not in
        # the local LayerStack".
        if list(parent_layer.subLayerPaths) != [layer.identifier]:
            parent_layer.subLayerPaths = [layer.identifier]
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

    layerChunk.add_params([
        layer_name,
        sublayer,
        default_prim,
        new_layer_id,
        layer_name_used,
        parent_layer_id,
    ])

    _update_visibility(layerChunk)

    return layerChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_LAYER,
        description="""
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
