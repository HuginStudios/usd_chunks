from usd_chunks import chunk


def file_changed(owner, notice, sender):
    """Callback for changing sublayer paths."""
    sublayer_path = ""
    param = notice.data
    if param.value:
        sublayer_path = notice.data.value
    else:
        # Import button needs to get the param.
        sublayer_path = param.get_chunk().get_param("sublayer_path").value

    stage = param.get_stage()
    # Each chunk has it's sublayer were all edigts go
    # By grabbing the current edittarget we get the chunks layer
    # in which we can add in our sublayer path.
    edit_layer = stage.GetEditTarget().GetLayer()
    # Since we expect to only have one sublayer per chunk
    # We clear the old sublayer paths with the new one
    if sublayer_path:
        edit_layer.subLayerPaths = [sublayer_path]
        print(f"Updated stage with sublayer. {sublayer_path}")


def main():
    sublayerChunk = chunk.Chunk("Sublayer")

    sublayer_fb = chunk.FileBrowser("sublayer_path", extensions=[".usd", ".usda", ".usdc", ".usdz"])
    sublayer_fb.add_callback(file_changed)

    sublayerChunk.add_param(sublayer_fb)

    button = chunk.Button("import", "import")
    button.add_callback(file_changed)

    sublayerChunk.add_param(button)

    return sublayerChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
