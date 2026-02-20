from usd_chunks import chunk, chunk_registry


def chunk_changed(owner, notice, data):
    print(f"{owner.name} solved")


def main():
    myChunk = chunk.Chunk("Req")
    myChunk.add_callback(chunk_changed)

    reg = chunk_registry.ChunkRegistry()
    # mod = reg.get_chunk_module("examples.dependencies")
    mod = reg.get_chunk_module("examples.dependencies")
    if not mod:
        raise ModuleNotFoundError("Could not find depenednecy")
    if not mod.loaded:
        mod.load()

    # The dependencies module exports 4 chunks: A, B, C, D.
    if c := mod.loaded_chunks.get("D"):
        myChunk.add_dependency(c)

    return myChunk


def config():
    return chunk.DefaulConfig(
        chunk.StandardCategory.EXAMPLES,
        "Example showing shunk requirements on other chunks",
        requirements=["examples.dependencies", "examples.dependencies"],
    )
