"""Inherit chunk -- adds an inherits arc from prims to a base prim.

The prims pick up the base prim's opinions, and keep following the base through
references: an inherited opinion is stronger than what a prim references in, so a base
authored in a shot overrides what every instance of an asset brought with it. Specialize
is the weak twin, for fallbacks. A base that doesn't exist yet is created as an abstract
`class` prim. All edits go to the chunk's own layer.
"""

from __future__ import annotations

from usd_chunks import chunk, chunk_utils


def execute(node: chunk.Chunk) -> None:
    chunk_utils.author_class_arc(node, "inherits")


def main():
    chunk_api = chunk.Chunk("Inherit")
    chunk_api.add_params([
        chunk.LineEdit(
            "prim_path", "Prims",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The prims that inherit. Supports globs, comma-separated paths and "
            "%collection references.",
        ),
        chunk.LineEdit(
            "base_path", "Base Prim", default_value="/_base",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The prim they inherit from. Created as an abstract class prim if it "
            "doesn't exist yet.",
        ),
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD,
        icon="link",
        description="""
        Makes prims inherit a base prim, so its opinions override every instance at once.

        Inherit -- adds an inherits arc from prims to a base prim, so they pick up the
        base's opinions and keep following it.

        An inherited opinion is stronger than what the prims reference in, so the base
        overrides every instance of an asset at once: set a colour on the base and every
        prim that inherits it changes, through any reference. For fallbacks that any
        other opinion wins over, use Specialize instead.

        Parameters:
          - Prims: the prims that inherit. Supports globs, comma-separated paths and
            %collection references. A prim not on the stage yet gets an over.
          - Base Prim: the prim they inherit from. If it doesn't exist it is created as
            an abstract `class` prim (not rendered or traversed); author its opinions
            with downstream chunks, such as Property Editor or Bind Material.

        A prim that is the base or sits under it is skipped: an arc to an ancestor is
        a composition error. All edits go to the chunk's own layer.
        """,
    )
