"""Runtime Chunk -- the type a chunk gets when it was built in the interpreter.

A chunk made by hand has no module behind it::

    from usd_chunks import chunk
    c = chunk.Chunk("hello")
    chunk.get_scene().add_chunk(c)
    c.add_param(chunk.Slider("slider"))

Everything about it lands on the stage -- its prim, its params as `inputs:*`, their values,
its position and wiring. The one thing missing was `info:chunkType`, which is how a load
finds the module to rebuild a chunk from; without it the prim was skipped and the chunk
disappeared, with all its data sitting right there in the file.

So a module-less chunk is saved as this type. The module itself is deliberately empty: it
exists to give such a chunk a type to be instantiated from, and load_chunks recreates the
params from the attributes the file already holds.

**What does not come back is behaviour.** Callbacks are Python objects in whoever's
interpreter made them, and nothing here saves code -- deliberately: the format that did
(.chsc, removed in #170) executed code out of a file when you opened it. What returns is
the shape and the data: name, params, values, wiring, position. A chunk that only holds
values round-trips completely; one that did something needs its code re-run to do it again.

Requires: usd_chunks.
"""

from __future__ import annotations

from usd_chunks import chunk

#: The `info:chunkType` token written for a chunk with no module of its own. Saved into
#: every file that holds one, so it is not a name to change lightly.
TYPE_NAME = "runtime"


def main():
    # Deliberately bare. The params come from the file: load_chunks rebuilds them from the
    # prim's own attributes, which is where the prim being loaded is actually known -- a
    # CHUNK_LOADED hook here would only know the chunk, and would have to guess its way back
    # to a prim that may not sit where the chunk now does.
    return chunk.Chunk("Chunk")


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        hidden=True,
        description="""
        What a chunk made in the interpreter, with no module of its own, saves as.

        Runtime Chunk -- what a chunk built in the interpreter is saved as.

        Not something to pick from this menu (it is hidden from it): the type exists so a
        chunk with no module of its own can be written to a file and read back, with its
        params and values intact. Behaviour is not saved -- callbacks live in the session
        that made them.
        """,
    )
