from __future__ import annotations
from usd_chunks import chunk
from usd_chunks.chunk_core import ChunkConfig


def config():
    return ChunkConfig(
        terminal=True,
        category=chunk.StandardCategory.CORE,
        icon="output",
        description="""
        Marks what the network delivers: the end of its chain, what a run or reference cooks to.

        What this graph delivers: the end of its chain. Cooks with no active view, and is what a run, a reference or a
        stronger layer cooks to.
        """,
    )


def main():
    return chunk.Chunk("Output")
