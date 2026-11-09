"""Specialize chunk -- adds a specializes arc from prims to a base prim.

The base prim's opinions become the prims' fallbacks: the weakest opinion of all, which
anything else on the prim wins over, even what it references in. It keeps following the
base through references, so a material library's base shader can be specialized and
refined per asset. Inherit is the strong twin. A base that doesn't exist yet is created
as an abstract `class` prim. All edits go to the chunk's own layer.
"""

from __future__ import annotations

from usd_chunks import chunk, chunk_utils


def execute(node: chunk.Chunk) -> None:
    chunk_utils.author_class_arc(node, "specializes")


def main():
    chunk_api = chunk.Chunk("Specialize")
    chunk_api.add_params([
        chunk.LineEdit(
            "prim_path", "Prims",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The prims that specialize the base. Supports globs, comma-separated "
            "paths and %collection references.",
        ),
        chunk.LineEdit(
            "base_path", "Base Prim", default_value="/_base",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The prim they specialize. Created as an abstract class prim if it "
            "doesn't exist yet.",
        ),
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="link",
        description="""
        Makes prims specialize a base prim, so its opinions are fallbacks each prim can override.

        Specialize -- adds a specializes arc from prims to a base prim, so its opinions
        become their fallbacks.

        A specialized opinion is the weakest of all: anything else on the prim wins,
        even what it references in. Use it for defaults that each prim may refine, like
        a base material every asset's material specializes. To make the base win over
        the assets instead, use Inherit.

        Parameters:
          - Prims: the prims that specialize the base. Supports globs, comma-separated
            paths and %collection references. A prim not on the stage yet gets an over.
          - Base Prim: the prim they specialize. If it doesn't exist it is created as an
            abstract `class` prim (not rendered or traversed); author its opinions with
            downstream chunks, such as Property Editor or Bind Material.

        A prim that is the base or sits under it is skipped: an arc to an ancestor is
        a composition error. All edits go to the chunk's own layer.
        """,
    )
