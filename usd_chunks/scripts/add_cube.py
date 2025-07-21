from usd_chunks.chunk import Chunk

def apply(api):
    stage = api.stage
    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1

def main(api, parent=None):
    chunk_api = Chunk(api, parent=parent)
    chunk_api.name = "Add Cubes"
    chunk_api.category = "Examples"
    layout = chunk_api.create_layout()
    chunk_api.create_button("Add cube!", apply, layout=layout)
    return chunk_api
