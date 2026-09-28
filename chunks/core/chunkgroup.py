from __future__ import annotations
from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags

def main():
    chunk_api = chunk.Chunk(
        "ChunkGroup",
        flags=(
            ChunkFlags.GroupFlags.CONTEXT
            | ChunkFlags.GroupFlags.COLLAPSABLE
            | ChunkFlags.GroupFlags.LABELED
            | ChunkFlags.GroupFlags.FRAMED
        ),
    )

    doc = chunk.TextEdit("doc", "Description", rows=3, flags=ChunkFlags.ParamFlags.LABELED, tooltip="Describe what this ChunkGroup does. Used by both users and LLMs to understand the network interface.")
    chunk_api.add_param(doc)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        description="""
        ChunkGroup: An empty container chunk that acts as a collection of Chunks.

        Double-click the node in the graph to dive into the group and work on its
        internal chunks. Sub-chunks are nested under the group's prim path in USD
        (e.g. /chunks/ChunkGroup/chunks/…), keeping the top-level graph clean.

        Groups can be saved as .usda files and loaded back via sublayer, reference or
        payload. Chunks sourced from an external layer are shown as locked nodes.
        """,
    )
