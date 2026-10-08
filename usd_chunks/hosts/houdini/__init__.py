"""usd_chunks in Houdini Solaris: every chunk is a LOP.

Houdini owns the graph here. Each chunk is one LOP node of the type ``usdchunks::chunk``,
wired to the others with ordinary LOP connections, and Houdini decides when it cooks --
usd_chunks never sees a wire between two chunks. A cook takes the node's input stage,
runs the chunk's module into its own layer on top of it, and hands a copy of that layer to
Houdini as a sublayer. So the invariant every host keeps holds here too: a chunk authors
into its own layer and nothing else.

- ``scene``  -- HoudiniScene, the SceneSender, and the one registry bring-up.
- ``parms``  -- chunk params as native Houdini parms, and values both ways.
- ``lop``    -- the node type: creating, cooking and deleting a chunk node.
- ``tools``  -- one TAB menu entry per chunk, under ``usdchunks/<category>``.
- ``build_hda.py`` -- regenerates ``otls/usdchunks_chunk.hda`` (run with hython).
- ``install.py``   -- one-time setup: dependencies + a Houdini package file.

Requires: hou (Houdini 21+). Not importable outside Houdini.
"""
