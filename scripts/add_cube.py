from usd_chunks import chunk, chunk_interface


def addCube(chunkItem: chunk.ChunkParamBase, notice, sender):
    stage = chunkItem.get_stage()
    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1


def main(api, parent=None):
    button = chunk.ChunkParamBase(chunk_interface.ButtonTemplate(), "add_cube", "Add Cube")
    button.add_callback(addCube)
    chunk_api = chunk.Chunk("Add Cubes", category="Examples", children=[button])
    return chunk_api
