"""usd_chunks in Houdini Solaris: every chunk is a LOP.

Houdini owns the graph here. Each chunk is one LOP node of the type ``usdchunks::chunk``,
wired to the others with ordinary LOP connections, and Houdini decides when it cooks --
usd_chunks never sees a wire between two chunks. A cook takes the node's input stage,
runs the chunk's module into its own layer on top of it, and merges that layer into
Houdini's active layer, as any Solaris node authors. A Layer chunk starts a new active
layer, so the chunks after it share it (see layers).

- ``scene``  -- HoudiniScene, the SceneSender, and the one registry bring-up.
- ``parms``  -- chunk params as native Houdini parms, and values both ways.
- ``lop``    -- the node type: creating, cooking and deleting a chunk node.
- ``layers`` -- the active layer: merging into it, Layer and Edit Target, named layers.
- ``tools``  -- one TAB menu entry per chunk, under ``usdchunks/<category>``.
- ``build_hda.py`` -- regenerates ``otls/usdchunks_chunk.hda`` (run with hython).
- ``install.py``   -- one-time setup: dependencies + a Houdini package file.

Requires: hou (Houdini 21+). Not importable outside Houdini.
"""
