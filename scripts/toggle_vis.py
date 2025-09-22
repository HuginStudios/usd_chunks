from usd_chunks import chunk, chunk_notice


def toggle_vis(chunkItem: chunk.ChunkParam, notice, sender: chunk.ChunkParam):
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


def clear_vis(chunkItem: chunk.ChunkParam, notice, sender: chunk.ChunkParam):
    """Clear the visibility set in the stage"""
    stage = chunkItem.get_stage()
    # stage = api.stage
    for prim in stage.Traverse():
        prim.GetAttribute("visibility").Clear()


def main(api):
    tglVis = chunk.ChunkParam()
    tglVis.param_type = chunk.ChunkParamType.BUTTON
    tglVis.name = "tgl_vis"
    tglVis.label = "Toggle Visility"
    tglVis.add_callback(toggle_vis)

    clearVis = chunk.ChunkParam()
    clearVis.param_type = chunk.ChunkParamType.BUTTON
    clearVis.name = "clr_vis"
    clearVis.label = "Clear Visility"
    clearVis.add_callback(clear_vis)

    myChunk = chunk.Chunk()
    myChunk.name = "myChunk"
    myChunk.category = "Utility"
    myChunk.add_params([tglVis, clearVis])
    return myChunk
