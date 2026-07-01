from usd_chunks import chunk


def main():
    a = chunk.Chunk("A")
    a_param = chunk.Slider("A_param")
    a.add_param(a_param)

    b = chunk.Chunk("B")
    b_param = chunk.Slider("B_param")
    b_param.connect_value(a_param)
    b.add_param(b_param)

    return [a, b]


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES)
