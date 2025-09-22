from usd_chunks import chunk

def addCube(chunkItem: chunk.ChunkParam, notice, sender):
    stage = chunkItem.get_stage()
    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1

def main(api, parent=None):
    button = chunk.ChunkParam()
    button.param_type = chunk.ChunkParamType.BUTTON
    button.name = "add_cube"
    button.label = "Add Cube"
    button.add_callback(addCube)

    chunk_api = chunk.Chunk()
    chunk_api.name = "Add Cubes"
    chunk_api.category = "Examples"

    chunk_api.add_params([button])
    return chunk_api
