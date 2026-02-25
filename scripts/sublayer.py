from usd_chunks import chunk


def import_sublayer(owner: chunk.Chunk, notice, sender):
    """Callback for changing sublayer paths."""
    
    if notice.data.name not in ["size", "value"]:
        return

    list_group = owner.get_param("imports")
    paths = []

    assert isinstance(list_group, chunk.ListGroup)

    for i in range(list_group.size):
        p = list_group.get_indexed_param("sublayer_path", i)
        if p and p.value:
            paths.append(p.value)

    # Since we expect to only have one sublayer per chunk
    # We clear the old sublayer paths with the new one
    stage = owner.get_stage()
    if stage:
        # Each chunk has it's sublayer were all edigts go
        # By grabbing the current edittarget we get the chunks layer
        # in which we can add in our sublayer path.
        if edit_layer := owner.get_edit_context():
            edit_layer.subLayerPaths = paths
            print(f"Updated stage with sublayer. {paths}")


def main():
    sublayerChunk = chunk.Chunk("Sublayer")

    sublayer_fb = chunk.FileBrowser("sublayer_path", extensions=[".usd", ".usda", ".usdc", ".usdz"])
    button = chunk.Button("import", "import")

    listGroup = chunk.ListGroup("imports")
    listGroup.unset_flag(chunk.ChunkFlags.GroupFlags.LABELED)
    listGroup.add_param(sublayer_fb)

    sublayerChunk.add_param(listGroup)
    sublayerChunk.add_param(button)

    # Because the names of the ListGroup is modified when an item in the list is removed, listening to ATTRIBUTE_CHANGED
    # will result in double execution of the callback when removing an index from the ListGroup.
    # Although, we'd like to listen to ATTRIBUTE_CHANGED to catch 'size' changes, and 'value' changes. We dont have a nice way to
    # filter the senders, so doing it inside the callback by checking the name of the notice data

    sublayerChunk.add_callback(import_sublayer, notice_alias=chunk.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    return sublayerChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.UTILS)
