from usd_chunks import chunk


def main():
    myChunk = chunk.Chunk("turtabler")
    return myChunk


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.RIGS)
