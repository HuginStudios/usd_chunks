"""Add Reference chunk — adds a USD file reference onto the selected prim."""

from __future__ import annotations

from pxr import Sdf, Usd

from usd_chunks import chunk


def execute(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        return
    prim = prims[0]

    file_path = (_chunk.get_param("file_path").value or "").strip()
    if not file_path:
        return

    prim_path = (_chunk.get_param("ref_prim_path").value or "").strip()

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    ref = Sdf.Reference(file_path, Sdf.Path(prim_path)) if prim_path else Sdf.Reference(file_path)

    with Usd.EditContext(stage, layer):
        prim.GetReferences().AddReference(ref)


def main():
    chunk_api = chunk.Chunk("Add Reference")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)

    chunk_api.add_param(
        chunk.FileBrowser(
            "file_path",
            "USD File",
            extensions=["usd", "usda", "usdc", "usdz"],
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.FILE_EXISTING,
        )
    )

    chunk_api.add_param(
        chunk.LineEdit("ref_prim_path", "Prim Path in File (optional)")
    )

    for name in ("prim_path", "file_path", "ref_prim_path"):
        chunk_api.get_param(name).add_callback(execute)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Composition")
