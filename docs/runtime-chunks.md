# Building a chunk at runtime

A chunk doesn't have to come from a file. You can build one in Python (usdview's Python
panel, a script, or a DCC's shell) and add it to the scene:

``` py
from usd_chunks import chunk

s = chunk.get_scene()
c = chunk.Chunk("hello")
s.add_chunk(c)
c.add_param(chunk.Slider("slider"))
```

The chunk shows up in the graph straight away, and you can connect it and use its params
like any other chunk. It's the quickest way to try an idea before writing a module.

## What a save keeps

A chunk without its own module is saved as the `runtime` type. When you reopen the file,
its **name, params, values, wiring and position** come back. The params are rebuilt from
the attributes on its prim, so this is still there after reopening:

``` py
c.add_param(chunk.Slider("slider"))   # still there next time
```

**Behaviour isn't saved.** Callbacks are Python objects that only exist in the session
that made them, and a saved file doesn't store them. A runtime chunk comes back with its
params and data only. To make it do something again, you
have to run its code again.

Also, an attribute only stores a param's type and value, not which widget it was. A float
`Slider` comes back as the default widget for a float, without its range or label. Names,
values and wiring come back exactly.

When a chunk is worth keeping, move it into a `.py` file in `USD_CHUNK_DIRS` with a
`main()` that builds it. Then its behaviour comes back too, because the code is loaded
from the file. See [Writing a chunk](writing-a-chunk.md).
