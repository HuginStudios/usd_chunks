from __future__ import annotations
from usd_chunks import chunk


def execute(node):
    """A Dot authors nothing; it only reroutes a wire."""


def main():
    return chunk.Chunk("Dot")


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        description="""
        Routes the execution chain without changing the stage: a passthrough to fan wires out.

        Pure passthrough — routes the execution chain without modifying the stage. Fan-out by connecting multiple
        downstream chunks to the single outputs:chunk.
        """,
    )
