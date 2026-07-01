from usd_chunks import chunk


def addCube(owner: chunk.Button, notice, sender):
    stage = owner.get_stage()
    if not stage:
        return

    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1


def main():

    chunk_api = chunk.Chunk("Add Cubes")
    button = chunk.Button("add_cube", "Add Cube")
    chunk_api.add_param(button)

    button.add_callback(addCube)
    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.EXAMPLES, interactive=True)
