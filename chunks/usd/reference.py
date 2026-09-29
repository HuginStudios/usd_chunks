from __future__ import annotations
from pxr import Sdf
from usd_chunks import chunk, chunk_stage, chunk_utils


def file_changed(owner, notice, sender):
    """Callback for changing reference paths: suggest a prim location from the file name."""
    chunk_stage.suggest_location_from_file(notice.data)


def execute(node):
    reference_path = node.get_param("file_path").value
    if not reference_path:
        return

    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    import_path = node.get_param("import_primpath").value

    # A file whose target is a chunkgroup is a network, not scene content: it goes onto
    # this chunk's own prim, and this node becomes the group -- dive in and the network
    # is there, its edits landing in the shot as overrides. Nothing is put at
    # primpath_location; the network's own chunks author the content when they cook.
    network = chunk_stage.network_target(reference_path, import_path)
    if network is not None:
        chunk_stage.host_network(node, reference_path, network, namespace=raw_path or "")
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
        prim = stage.GetPrimAtPath(target_path)
        if not prim or not prim.IsDefined():
            prim = stage.DefinePrim(target_path, prim_type)
        prim.GetReferences().AddReference(ref)


def main():
    referenceChunk = chunk.Chunk("Reference")

    reference_fb = chunk.FileBrowser("file_path", extensions=[".usd*", ".usd", ".usda", ".usdc", ".usdz"])
    reference_fb.set_flag(chunk.ChunkFlags.ParamFlags.FILE_EXISTING)
    reference_fb.add_callback(file_changed)
    import_primpath = chunk.LineEdit("import_primpath", "Import Prim Path")
    primpath = chunk.LineEdit("primpath_location", "Insert to location")
    prim_type = chunk.LineEdit("prim_type", "Prim Type", default_value="Xform")

    referenceChunk.add_param(reference_fb)
    referenceChunk.add_param(import_primpath)
    referenceChunk.add_param(primpath)
    referenceChunk.add_param(prim_type)

    return referenceChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Reference chunk: Lets the user pick an external USD file and insert it as a
        Usd.Reference into the active stage.

        When a file is selected, the asset name is extracted and used to suggest a
        unique prim path (e.g. /MyAsset) under which the reference will live.
        On import, the chunk creates an Xform prim at that location (if it doesn't
        already exist) and calls AddReference on it.

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
