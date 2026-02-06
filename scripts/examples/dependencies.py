from usd_chunks import chunk


def main():
    a = chunk.Chunk("A")
    a.add_param(chunk.Checkbox("A_param"))

    b = chunk.Chunk("B")
    b.add_param(chunk.Checkbox("B_param"))
    b.add_input(a)

    c = chunk.Chunk("C")
    c.add_param(chunk.Checkbox("C_param"))

    d = chunk.Chunk("D")

    d.add_input(a)
    d.add_input(b)
    d.add_input(c)

    return [a, b, c, d]


def config():
    return chunk.DefaulConfig(chunk.StandardCategory.EXAMPLES)