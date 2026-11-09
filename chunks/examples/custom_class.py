from __future__ import annotations
from usd_chunks import chunk


class CustomChunk(chunk.Chunk):
    def __init__(self):
        super().__init__(name="CustomChunk")

        grp = self.add_param(chunk.Group("Container"))
        self.slider = grp.add_param(chunk.Slider("Slider"))


def main():
    return CustomChunk()


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.EXAMPLES,
        description="""
        Example: a chunk built as a Python class, a chunk.Chunk subclass with a grouped slider.

        main() returns an instance of a chunk.Chunk subclass whose __init__ adds a Container
        group holding a Slider.
        """,
    )
