from usd_chunks import chunk, chunk_core


def addCube(owner, notice, sender):
    stage = owner.get_stage()
    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1


def main():

    button = chunk.Button("add_cube", "Add Cube")
    button.add_callback(addCube)

    chunk_api = chunk.Chunk("Add Cubes", category="Examples")
    chunk_api.add_param(button)

    return chunk_api
