import random

from usd_chunks import chunk


def execute(node):
    min_val = float(node.get_param("min_value").value or 0.0)
    max_val = float(node.get_param("max_value").value or 0.0)
    seed = int(node.get_param("seed").value or 0)

    if min_val > max_val:
        min_val, max_val = max_val, min_val

    node.get_param("result").value = random.Random(seed).uniform(min_val, max_val)


def main():
    chunk_api = chunk.Chunk("Random")
    chunk_api.add_params([
        chunk.Slider("min_value", "Min", default_value=0.0, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("max_value", "Max", default_value=1.0, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("seed", "Seed", default_value=0, is_int=True, flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("result", "Result", flags=chunk.ChunkFlags.ParamFlags.OUTPUT),
    ])
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Generates a deterministic random float between Min and Max, seeded by Seed.\n"
            "Same seed always produces the same value; Min/Max are swapped if Min > Max."
        ),
    )
