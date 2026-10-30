from __future__ import annotations
from pxr import Sdf
from usd_chunks import chunk, chunk_stage, chunk_utils, sdf_authoring


def file_changed(owner, notice, sender):
    """Callback for changing reference paths: suggest a prim location from the file name."""
    chunk_stage.suggest_location_from_file(notice.data)


def execute(node):
    reference_path = chunk_utils.arc_source_path(node)
    if not reference_path:
        return

    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    import_path = node.get_param("import_primpath").value

    # A file whose target is a chunkgroup is a network, not scene content: it goes onto
    # this chunk's own prim, and this node becomes the group -- dive in and the network
    # is there, its edits landing in the shot as overrides. Nothing is put at
    # primpath_location; the network's own chunks author the content when they cook.
    network = None if chunk_utils.arc_from_layer(node) else chunk_stage.network_target(reference_path, import_path)
    if network is not None:
        chunk_stage.host_network(node, reference_path, network, namespace=raw_path or "")
        if raw_path:  # the namespace root, else only an untyped parent of the network's prims
            for target_path in chunk_utils.author_in_variant_contexts(stage, node.get_param("primpath_location")):
                _define(stage, target_path, node.get_param("prim_type").value or "Xform")
        return
    chunk_stage.unhost_network(node)

    if import_path:
        ref = Sdf.Reference(reference_path, import_path)
    else:
        layer = Sdf.Layer.FindOrOpen(reference_path)
        if layer and not layer.defaultPrim:
            roots = list(layer.rootPrims)
            ref = Sdf.Reference(reference_path, f"/{roots[0].name}") if len(roots) == 1 else Sdf.Reference(reference_path)
        else:
            ref = Sdf.Reference(reference_path)

    prim_type = node.get_param("prim_type").value or "Xform"
    primpath_param = node.get_param("primpath_location")
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        _define(stage, target_path, prim_type).GetReferences().AddReference(ref)


def _define(stage, path, prim_type):
    """The prim at path, defined as prim_type in this chunk's own layer."""
    prim = stage.DefinePrim(path, prim_type)  # defines any missing parents too
    # Into this chunk's own layer even when something weaker (an upstream chunk, the file
    # itself) already defines the prim: DefinePrim writes nothing then, and the chosen type
    # was lost to an `over`.
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
        tooltip="A layer a Layer chunk made with Sublayer off, to bring in by this arc. Pick "
        "it, or wire the Layer chunk's Layer Name in: then the Layer chunk need not sit in "
        "the chain before this one.",
    )
    return from_layer, menu


def menu_items(node, param_name):
    """The network's arc layers for the Layer menu, asked for as it opens (chunk.Menu.refresh_items).

    Not kept current by a stage listener: every Reference and Payload heard every USD change
    to do that, which on a network with a hundred of them was most of the time spent opening it.
    """
    return chunk_utils.arc_layer_items() if param_name == "layer" else None


def main():
    referenceChunk = chunk.Chunk("Reference")

    reference_fb = chunk.FileBrowser("file_path", extensions=[".usd*", ".usd", ".usda", ".usdc", ".usdz"])
    reference_fb.set_flag(chunk.ChunkFlags.ParamFlags.FILE_EXISTING)
    reference_fb.add_callback(file_changed)
    import_primpath = chunk.LineEdit("import_primpath", "Import Prim Path")
    primpath = chunk.LineEdit("primpath_location", "Insert to location")
    # dynamic_items: a saved or scripted type outside the list (Cube, say) is kept.
    prim_type = chunk.Menu("prim_type", "Prim Type", menu_items=["Xform", "Scope", "Material"], default_value="Xform",
                           dynamic_items=True)

    from_layer, layer_menu = _source_params()
    referenceChunk.add_param(from_layer)
    referenceChunk.add_param(reference_fb)
    referenceChunk.add_param(layer_menu)
    referenceChunk.add_param(import_primpath)
    referenceChunk.add_param(primpath)
    referenceChunk.add_param(prim_type)

    chunk_utils.show_arc_source(referenceChunk)
    referenceChunk.add_callback(chunk_utils.arc_source_changed, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)

    return referenceChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="link",
        description="""
        Reference chunk: Lets the user pick an external USD file and insert it as a
        Usd.Reference into the active stage.

        When a file is selected, the asset name is extracted and used to suggest a
        unique prim path (e.g. /MyAsset) under which the reference will live.
        On import, the chunk defines a prim of the chosen Prim Type at that location
        in its own layer (overriding the type of any weaker definition) and calls
        AddReference on it.

        The target prim inside the referenced file is resolved in order:
          1. The explicit "Import Prim Path" field, if filled in.
          2. The layer's defaultPrim, if one is set.
          3. The single root prim, if the layer has exactly one.
          4. No prim path (USD picks the defaultPrim at runtime).

        When the target is a chunkgroup (a saved chunk network), the node hosts that
        network instead of placing it in the scene: dive into the node to see and tweak
        the network, as with a chunkgroup. Tweaks are overrides in the current file; the
        referenced network file is never written to.

        Import is also variant-context-aware via author_in_variant_contexts, so the
        reference is authored inside any active variant selection on the stage.
        """)
