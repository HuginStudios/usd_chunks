from usd_chunks import chunk


class CustomChunk(chunk.Chunk):
    def __init__(self):
        super().__init__(name="CustomChunk")

        grp = self.add_param(chunk.Group("Container"))
        self.slider = grp.add_param(chunk.Slider("Slider"))


def main():
    return CustomChunk()


def config():
    return chunk.DefaultConfig(category=chunk.StandardCategory.EXAMPLES)
