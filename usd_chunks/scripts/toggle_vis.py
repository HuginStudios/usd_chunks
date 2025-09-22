from usd_chunks.chunk import Chunk

def toggle_vis(api):
    """Toggle visibility of the selected prim in the USD stage."""
    stage = api.stage
    selected_prim = api.prim
    if not selected_prim.IsValid() or selected_prim == api.stage.GetPseudoRoot():
        return
    # Set visibility
    visibility = selected_prim.GetAttribute("visibility")
    if visibility.Get() == "inherited":
        visibility.Set("invisible")
    else:
        visibility.Set("inherited")
    
def clear_vis(api):
    """Clear the visibility set in the stage"""
    stage = api.stage
    for prim in stage.Traverse():
        visibility = prim.GetAttribute("visibility").Clear()

def main(api, parent=None):
    chunk_api = Chunk(api, parent=parent)
    chunk_api.name = "Vis/Invis"
    chunk_api.category = "Utility"
    layout = chunk_api.new_layout()
    chunk_api.create_button("Toggle visiblity!", toggle_vis, layout=layout)
    chunk_api.create_button("Clear visiblity!", clear_vis)
    return chunk_api
