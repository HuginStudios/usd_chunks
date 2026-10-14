from __future__ import annotations
from pxr import Sdf
from usd_chunks import chunk, chunk_stage, chunk_utils, sdf_authoring


def file_changed(owner, notice, sender):
    """Callback for changing payload paths: suggest a prim location from the file name."""
    chunk_stage.suggest_location_from_file(notice.data)


def execute(node):
    payload_path = chunk_utils.arc_source_path(node)
    if not payload_path:
        return

    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    import_path = node.get_param("import_primpath").value

    # A file whose target is a chunkgroup is a network, not scene content: it goes onto
    # this chunk's own prim as a payload, and this node becomes the group -- dive in and
    # the network is there. Same as the Reference chunk, with a payload arc, so the
    # network unloads with the stage's other payloads.
    network = None if chunk_utils.arc_from_layer(node) else chunk_stage.network_target(payload_path, import_path)
    if network is not None:
        chunk_stage.host_network(node, payload_path, network, arc="payload", namespace=raw_path or "")
        if raw_path:  # the namespace root, else only an untyped parent of the network's prims
            for target_path in chunk_utils.author_in_variant_contexts(stage, node.get_param("primpath_location")):
                _define(stage, target_path, node.get_param("prim_type").value or "Xform")
        return
    chunk_stage.unhost_network(node)

    if import_path:
        ref = Sdf.Payload(payload_path, import_path)
    else:
        layer = Sdf.Layer.FindOrOpen(payload_path)
        if layer and not layer.defaultPrim:
            roots = list(layer.rootPrims)
            ref = Sdf.Payload(payload_path, f"/{roots[0].name}") if len(roots) == 1 else Sdf.Payload(payload_path)
        else:
            ref = Sdf.Payload(payload_path)

    prim_type = node.get_param("prim_type").value or "Xform"
    primpath_param = node.get_param("primpath_location")
    targets = []
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        targets.append(target_path)
        _define(stage, target_path, prim_type).GetPayloads().AddPayload(ref)
    chunk_utils.set_payloads_loaded(node, node.get_scene_stage(), targets, node.get_param("load").value)


def _define(stage, path, prim_type):
    """The prim at path, defined as prim_type in this chunk's own layer."""
    prim = stage.DefinePrim(path, prim_type)  # defines any missing parents too
    # Into this chunk's own layer even when something already defines the prim -- another
    # Payload chunk's file, an upstream chunk: DefinePrim writes nothing then, and the prim
    # was left an `over` without the chosen type.
    sdf_authoring.Writer(stage).define(path, prim_type)
    return prim


def _source_params():
    """Existing Layer, and its Layer menu: a layer a Layer chunk upstream made to bring in."""
    from_layer = chunk.Checkbox(
        "from_layer", "Existing Layer", default_value=False,
        tooltip="Unchecked (default): bring in File Path. Checked: bring in a layer a Layer "
        "chunk made with Sublayer off, picked in Layer -- an Edit Target downstream of this "
        "then authors into it through the arc, and Export USD writes it as its own file, "
        "with the arc pointing at that file.",
    )
    from_layer.add_callback(chunk_utils.arc_source_changed, notice_alias=chunk.ChunkNoticeAlias.VALUE_CHANGED)
    menu = chunk.Menu(
        "layer", "Layer",
        # The layers are the network's as last seen, so a saved choice is set before the
        # Layer chunk making it has cooked and listed it.
        dynamic_items=True,
        tooltip="A layer a Layer chunk made with Sublayer off, to bring in by this arc.",
    )
    return from_layer, menu


def main():
    PayloadChunk = chunk.Chunk("Payload")

    # import
    Payload_fb = chunk.FileBrowser("file_path", extensions=[".usd*", ".usd", ".usda", ".usdc", ".usdz"])
    Payload_fb.set_flag(chunk.ChunkFlags.ParamFlags.FILE_EXISTING)
    Payload_fb.add_callback(file_changed)
    import_primpath = chunk.LineEdit("import_primpath", "Import Prim Path", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    from_layer, layer_menu = _source_params()
    PayloadChunk.add_param(from_layer)
    PayloadChunk.add_param(Payload_fb)
    PayloadChunk.add_param(layer_menu)
    PayloadChunk.add_param(import_primpath)

    # prim location
    primpath = chunk.LineEdit("primpath_location", "Insert to location", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    PayloadChunk.add_param(primpath)
    # dynamic_items: a saved or scripted type outside the list (Cube, say) is kept.
    PayloadChunk.add_param(chunk.Menu("prim_type", "Prim Type", menu_items=["Xform", "Scope", "Material"], default_value="Xform",
                                      dynamic_items=True))
    PayloadChunk.add_param(chunk.Checkbox(
        "load", "Load", default_value=True,
        tooltip="Unchecked: the payload is unloaded, for a lighter stage. Checked loads back "
        "what this unloaded; it never loads a payload unloaded by hand.",
    ))

    PayloadChunk.add_callback(
        chunk_utils.refresh_arc_layer_menu,
        notice_alias=chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED,
        listen_globally=True,
        send_on_create=True,
    )

    chunk_utils.show_arc_source(PayloadChunk)
    PayloadChunk.add_callback(chunk_utils.arc_source_changed, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)

    return PayloadChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Payload chunk: Lets the user pick an external USD file and insert it as a
        Usd.Payload into the active stage.

        When a file is selected, the asset name is extracted and used to suggest a
        unique prim path (e.g. /MyAsset) under which the Payload will live.
        On import, the chunk defines a prim of the chosen Prim Type (Xform by default)
        at that location in its own layer, overriding the type of any weaker definition,
        and calls AddPayload on it.

        The target prim inside the payloaded file is resolved in order:
          1. The explicit "Import Prim Path" field, if filled in.
          2. The layer's defaultPrim, if one is set.
          3. The single root prim, if the layer has exactly one.
          4. No prim path (USD picks the defaultPrim at runtime).

        When the target is a chunkgroup (a saved chunk network), the node hosts that
        network as a payload on its own prim instead of placing it in the scene: dive
        into the node to see and tweak the network, as with a chunkgroup. Tweaks are
        overrides in the current file; the network file is never written to.

        Import is also variant-context-aware via author_in_variant_contexts, so the
        Payload is authored inside any active variant selection on the stage.
        """)
