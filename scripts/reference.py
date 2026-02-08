import os
from pxr import Sdf
from usd_chunks import chunk


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

    prim = stage.GetPrimAtPath(prim_path)
    counter = 1
    while prim.IsValid():
        unique_path = prim_path + f"{counter}"
        prim = stage.GetPrimAtPath(prim_path)
        counter += 1
    param.get_chunk().get_param("primpath_location").value = unique_path


def import_callback(owner, notice, sender):
    param = notice.data
    stage = param.get_stage()
    # Clear any edits in this layer before importing a reference again.
    chunk_layer = stage.GetEditTarget().GetLayer()
    chunk_layer.Clear()

    reference_path = param.get_chunk().get_param("file_path").value
    prim_path = param.get_chunk().get_param("primpath_location").value
    prim = stage.GetPrimAtPath(prim_path)
    prim_type = "Xform"
    if not prim.IsValid():
        prim = stage.DefinePrim(prim_path, prim_type)
    import_path = param.get_chunk().get_param("import_primpath").value
    if import_path:
        ref_path = Sdf.Reference(reference_path, import_path)
    else:
        ref_path = Sdf.Reference(reference_path)
    prim.GetReferences().AddReference(ref_path)


def main():
    referenceChunk = chunk.Chunk("Reference")

    # import
    reference_fb = chunk.FileBrowser("file_path", extensions=[".usd", ".usda", ".usdc", ".usdz"])
    reference_fb.add_callback(file_changed)
    import_primpath = chunk.LineEdit("import_primpath", "Import Prim Path", flags=[chunk.ChunkFlags.ParamFlags.LABELED])
    import_primpath.add_callback(import_callback)

    referenceChunk.add_param(reference_fb)
    referenceChunk.add_param(import_primpath)

    # prim location
    primpath = chunk.LineEdit("primpath_location", "Insert to location", flags=[chunk.ChunkFlags.ParamFlags.LABELED])
    primpath.add_callback(import_callback)
    referenceChunk.add_param(primpath)

    button = chunk.Button("import", "import")
    button.add_callback(import_callback)

    referenceChunk.add_param(button)

    return referenceChunk


def config():
    return chunk.DefaulConfig(category="Utility")
