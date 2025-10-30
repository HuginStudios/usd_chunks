from usd_chunks import chunk, chunk_interface


def toggle_vis(chunkItem: chunk.ChunkParamBase, notice, sender: chunk.ChunkParamBase):
    """Toggle visibility of the selected prim in the USD stage."""
    api = chunkItem.get_usdview_api()
    prim = api.prim
    if not prim.IsValid() or prim == api.stage.GetPseudoRoot():
        return

    # Set visibility
    visibility = prim.GetAttribute("visibility")
    if visibility.Get() == "inherited":
        visibility.Set("invisible")
    else:
        visibility.Set("inherited")


def clear_vis(chunkItem: chunk.ChunkParamBase, notice, sender: chunk.ChunkParamBase):
    """Clear the visibility set in the stage"""
    stage = chunkItem.get_stage()
    for prim in stage.Traverse():
        prim.GetAttribute("visibility").Clear()


def main(api):
    tglVis = chunk.ChunkParamBase(chunk_interface.ButtonTemplate(), "tgl_vis", "Toggle Visibility")
    tglVis.add_callback(toggle_vis)

    clearVis = chunk.ChunkParamBase(chunk_interface.ButtonTemplate(), "clr_vis", "Clear Visibility")
    clearVis.add_callback(clear_vis)

    myChunk = chunk.Chunk("Visibility", category="Utility", children=[tglVis, clearVis])
    return myChunk
