from usd_chunks import chunk


def toggle_vis(notice, sender: chunk.ChunkParamBase):
    """Toggle visibility of the selected prim in the USD stage."""
    api = sender.get_usdview_api()
    prim = api.prim
    if not prim.IsValid() or prim == api.stage.GetPseudoRoot():
        return

    # Set visibility
    visibility = prim.GetAttribute("visibility")
    if visibility.Get() == "inherited":
        visibility.Set("invisible")
    else:
        visibility.Set("inherited")


def clear_vis(notice, sender: chunk.ChunkParamBase):
    """Clear the visibility set in the stage"""
    stage = sender.get_stage()
    for prim in stage.Traverse():
        prim.GetAttribute("visibility").Clear()


def main(api):
    tglVis = chunk.Button("tgl_vis", "Toggle Visibility")
    tglVis.add_callback(toggle_vis)

    clearVis = chunk.Button("clr_vis", "Clear Visibility")
    clearVis.add_callback(clear_vis)

    myChunk = chunk.Chunk("Visibility", category="Utility")
    myChunk.add_params([tglVis, clearVis])
    return myChunk
