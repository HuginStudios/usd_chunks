from usd_chunks import chunk


def toggle_vis(owner: chunk.ChunkParamBase, notice, sender):
    """Toggle visibility of the selected prim in the USD stage."""
    stage = owner.get_stage()
    if not stage:
        return

    if scene := chunk.get_scene():
        prims = scene.get_selected_prims()
    else:
        return

    for prim in prims:
        if not prim.IsValid() or prim == stage.GetPseudoRoot():
            return

        # Set visibility
        visibility = prim.GetAttribute("visibility")
        if visibility.Get() == "inherited":
            visibility.Set("invisible")
        else:
            visibility.Set("inherited")


def clear_vis(owner: chunk.ChunkParamBase, notice, sender):
    """Clear the visibility set in the stage"""

    stage = owner.get_stage()
    if not stage:
        return

    for prim in stage.Traverse():
        prim.GetAttribute("visibility").Clear()


def main():
    tglVis = chunk.Button("tgl_vis", "Toggle Visibility")
    tglVis.add_callback(toggle_vis)

    clearVis = chunk.Button("clr_vis", "Clear Visibility")
    clearVis.add_callback(clear_vis)

    myChunk = chunk.Chunk("Visibility")
    myChunk.add_params([tglVis, clearVis])
    return myChunk


def config():
    return chunk.DefaultConfig(category=chunk.StandardCategory.EXAMPLES)
