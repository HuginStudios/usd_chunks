import os
from pxr import Sdf
from usd_chunks import chunk, chunk_utils
from usd_chunks.chunk_utils import get_unique_path


def file_changed(owner, notice, sender):
    """Callback for changing Payload paths."""
    param = notice.data
    _chunk = param.get_chunk()

    if param.name == "file_path" and param.value:
        Payload_path = param.value
    else:
        fp = _chunk.get_param("file_path") if _chunk else None
        Payload_path = fp.value if fp else None

    if not Payload_path:
        return

    primpath_param = _chunk.get_param("primpath_location") if _chunk else None
    if not primpath_param:
        return
    
    if chunk_utils.has_expression(primpath_param.raw_value or ""):
        return
    
    asset_name = os.path.splitext(os.path.basename(Payload_path))[0]
    stage = param.get_stage()
    unique_path = get_unique_path(stage, f"/{asset_name}")
    primpath_param.value = unique_path


def execute(node):
    payload_path = node.get_param("file_path").value
    if not payload_path:
        return

    stage = node.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    raw_path = node.get_param("primpath_location").value
    import_path = node.get_param("import_primpath").value

    if import_path:
        ref = Sdf.Payload(payload_path, import_path)
    else:
        layer = Sdf.Layer.FindOrOpen(payload_path)
        if layer and not layer.defaultPrim:
            roots = list(layer.rootPrims)
            ref = Sdf.Payload(payload_path, f"/{roots[0].name}") if len(roots) == 1 else Sdf.Payload(payload_path)
        else:
            ref = Sdf.Payload(payload_path)

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.DefinePrim(target_path, "Xform")
        prim.GetPayloads().AddPayload(ref)


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

    return PayloadChunk


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Payload chunk: Lets the user pick an external USD file and insert it as a
        Usd.Payload into the active stage.

        When a file is selected, the asset name is extracted and used to suggest a
        unique prim path (e.g. /MyAsset) under which the Payload will live.
        On import, the chunk creates an Xform prim at that location (if it doesn't
        already exist) and calls AddPayload on it.

        The target prim inside the Payloadd file is resolved in order:
          1. The explicit "Import Prim Path" field, if filled in.
          2. The layer's defaultPrim, if one is set.
          3. The single root prim, if the layer has exactly one.
          4. No prim path (USD picks the defaultPrim at runtime).

        Import is also variant-context-aware via author_in_variant_context, so the
        Payload is authored inside any active variant selection on the stage.
        """)
