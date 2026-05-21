import os
from pxr import Sdf
from usd_chunks import chunk, chunk_utils
from usd_chunks.chunk_utils import get_unique_path


def file_changed(owner, notice, sender):
    """Callback for changing reference paths."""
    reference_path = ""
    param = notice.data
    if param.name == "file_path" and param.value:
        reference_path = notice.data.value
    else:
        # Import button needs to get the param.
        reference_path = param.get_chunk().get_param("file_path").value

    asset_name = os.path.basename(reference_path)
    asset_name = os.path.splitext(asset_name)[0]
    prim_path = prim_path = f"/{asset_name}"
    unique_path = prim_path
    stage = param.get_stage()

    unique_path = get_unique_path(stage, prim_path)
    param.get_chunk().get_param("primpath_location").value = unique_path


def import_callback(owner, notice, sender):
    param = notice.data
    stage = param.get_stage()
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    reference_path = param.get_chunk().get_param("file_path").value
    raw_path = param.get_chunk().get_param("primpath_location").value
    import_path = param.get_chunk().get_param("import_primpath").value

    if import_path:
        ref = Sdf.Reference(reference_path, import_path)
    else:
        layer = Sdf.Layer.FindOrOpen(reference_path)
        if layer and not layer.defaultPrim:
            roots = list(layer.rootPrims)
            ref = Sdf.Reference(reference_path, f"/{roots[0].name}") if len(roots) == 1 else Sdf.Reference(reference_path)
        else:
            ref = Sdf.Reference(reference_path)

    with chunk_utils.author_in_variant_context(stage, raw_path) as target_path:
        prim = stage.GetPrimAtPath(target_path)
        if not prim.IsValid():
            prim = stage.DefinePrim(target_path, "Xform")
        prim.GetReferences().AddReference(ref)


def main():
    referenceChunk = chunk.Chunk("Reference")

    # import
    reference_fb = chunk.FileBrowser("file_path", extensions=[".usd*", ".usd", ".usda", ".usdc", ".usdz"])
    reference_fb.add_callback(file_changed)
    import_primpath = chunk.LineEdit("import_primpath", "Import Prim Path", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    import_primpath.add_callback(import_callback)

    referenceChunk.add_param(reference_fb)
    referenceChunk.add_param(import_primpath)

    # prim location
    primpath = chunk.LineEdit("primpath_location", "Insert to location", flags=chunk.ChunkFlags.ParamFlags.LABELED)
    primpath.add_callback(import_callback)
    referenceChunk.add_param(primpath)

    button = chunk.Button("import", "import")
    button.add_callback(import_callback)

    referenceChunk.add_param(button)

    return referenceChunk


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="""
        Reference chunk — lets the user pick an external USD file and insert it as a
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

        Import is also variant-context-aware via author_in_variant_context, so the
        reference is authored inside any active variant selection on the stage.
        """)
