# Building a chunk at runtime

A chunk does not have to come from a file. Build one in the interpreter — usdview's
Python panel, a script, a DCC's shell — and add it to the scene:

``` py
from usd_chunks import chunk

s = chunk.get_scene()
c = chunk.Chunk("hello")
s.add_chunk(c)
c.add_param(chunk.Slider("slider"))
```

The chunk appears in the graph immediately, wires up like any other, and its params
behave the same. It's the quickest way to try an idea before it's worth a module.

## What a save keeps

A chunk with no module of its own is recorded as the `runtime` type, and comes back with
its **name, params, values, wiring and position** intact. Params are rebuilt from the
attributes on its prim, so this survives a reopen with nothing else to do:

``` py
c.add_param(chunk.Slider("slider"))   # untouched, and still there next time
```

**Behaviour is not saved.** Callbacks are Python objects belonging to the session that
made them, and a file records no code — deliberately, so opening a network can never run
any. A runtime chunk therefore reloads as its shape and its data; a chunk that *does*
something needs its code re-run to do it again.

One more limit worth knowing: an attribute records a param's type and value, not which
widget it was built as. A `Slider` of floats comes back as whatever a float maps to,
without its range or label. Names, values and wiring are exact.

When a chunk is worth keeping, move it into a `.py` file on `USD_CHUNK_DIRS` with a
`main()` that builds it — then its behaviour comes back too, because the code is what's
loaded. See [Writing a chunk](writing-a-chunk.md).
