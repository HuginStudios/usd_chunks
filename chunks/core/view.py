from __future__ import annotations
from usd_chunks import chunk
from usd_chunks.chunk_core import ChunkConfig


def config():
    return ChunkConfig(
        terminal=True,
        category=chunk.StandardCategory.CORE,
        icon="view",
        description="""
        Cooks everything above it: the end point you look at while building a chain (V key).

        Terminates a chunk stack and triggers upstream execution. Place with V in the graph.
        """,
    )


def main():
    return chunk.Chunk("View")
