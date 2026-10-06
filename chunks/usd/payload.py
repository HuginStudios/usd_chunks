from __future__ import annotations
from pxr import Sdf
from usd_chunks import chunk, chunk_stage, chunk_utils, sdf_authoring


def file_changed(owner, notice, sender):
    """Callback for changing payload paths: suggest a prim location from the file name."""
    chunk_stage.suggest_location_from_file(notice.data)


def execute(node):
    payload_path = node.get_param("file_path").value
    if not payload_path:
        return

    stage = node.get_stage()

    raw_path = node.get_param("primpath_location").value
    import_path = node.get_param("import_primpath").value

    # A file whose target is a chunkgroup is a network, not scene content: it goes onto
    # this chunk's own prim as a payload, and this node becomes the group -- dive in and
    # the network is there. Same as the Reference chunk, with a payload arc, so the
    # network unloads with the stage's other payloads.
    network = chunk_stage.network_target(payload_path, import_path)
    if network is not None:
        chunk_stage.host_network(node, payload_path, network, arc="payload", namespace=raw_path or "")
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

    primpath_param = node.get_param("primpath_location")
    targets = []
    for target_path in chunk_utils.author_in_variant_contexts(stage, primpath_param):
        targets.append(target_path)
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.DefinePrim(target_path, "Xform")
        elif chunk_utils.is_composed_in(prim):
            # Valid, but only because another Payload chunk's file defines it. With two
            # chunks on one prim each saw the other's payload, neither defined the prim,
            # and it ended up an `over`. DefinePrim will not help: it returns a prim that
            # is already defined without writing anything.
            spec = sdf_authoring.Writer(stage).define(target_path)
            spec.typeName = spec.typeName or prim.GetTypeName() or "Xform"
        prim.GetPayloads().AddPayload(ref)
    chunk_utils.set_payloads_loaded(node, node.get_scene_stage(), targets, node.get_param("load").value)


def main():
    PayloadChunk = chunk.Chunk("Payload")

    # import
    Payload_fb = chunk.FileBrowser("file_path", extensions=[".usd*", ".usd", ".usda", ".usdc", ".usdz"])
    Payload_fb.set_flag(chunk.ChunkFlags.ParamFlags.FILE_EXISTING)
    Payload_fb.add_callback(file_changed)
    import_primpath = chunk.LineEdit("import_primpath", "Import Prim Path", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    PayloadChunk.add_param(Payload_fb)
    PayloadChunk.add_param(import_primpath)

    # prim location
    primpath = chunk.LineEdit("primpath_location", "Insert to location", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    PayloadChunk.add_param(primpath)
    PayloadChunk.add_param(chunk.Checkbox(
        "load", "Load", default_value=True,
        tooltip="Unchecked: the payload is unloaded, for a lighter stage. Checked loads back "
        "what this unloaded; it never loads a payload unloaded by hand.",
    ))

    return PayloadChunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        description="""
        Payload chunk: Lets the user pick an external USD file and insert it as a
        Usd.Payload into the active stage.

        When a file is selected, the asset name is extracted and used to suggest a
        unique prim path (e.g. /MyAsset) under which the Payload will live.
        On import, the chunk creates an Xform prim at that location (if it doesn't
        already exist) and calls AddPayload on it.

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
