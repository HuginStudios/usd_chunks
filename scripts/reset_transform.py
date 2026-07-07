"""Reset Transform chunk — clears authored transform ops on the selected prim(s).

Requires: pxr (OpenUSD), usd_chunks.
"""

from pxr import UsdGeom
from usd_chunks import chunk


def _run(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        return
    prim = prims[0]

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Callbacks already run inside the chunk's edit context (NoticeConnection).
    UsdGeom.Xformable(prim).ClearXformOpOrder()


def main():
    chunk_api = chunk.Chunk("Reset Transform")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim whose transform ops to clear — pick it in the viewport or type a path.",
    )
    chunk_api.add_param(prim_path)

    run_btn = chunk.Button("run", "Reset Transform", tooltip="Clear the prim's authored transform ops.")
    chunk_api.add_param(run_btn)
    run_btn.add_callback(_run)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Clears all authored transform ops on the target prim, resetting it to its\n"
            "underlying composed transform. Authors in the chunk layer, so the prim's\n"
            "original definition is untouched — remove the chunk to restore it."
        ),
    )
