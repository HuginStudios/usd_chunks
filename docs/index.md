# USD Chunks

usd_chunks is a toolkit for quickly building UI tools for USD — initially for usdview,
so a user could add custom tooling without writing the UI logic and plugin registration
each time. It works by being a USD plugin itself, loading chunks (`.py` files) at runtime
from wherever you point it.

> usd_chunks in a nutshell: chunks are written in Python. A chunk describes what it does
> in `execute(node)`, how it looks in `main()`, and its metadata in `config()` — things
> like `description` and `category`. That's it.

New here? [Install](install.md#quick-start) has the copy-paste path from nothing to a
chunk on screen.

## The graph is USD

There is no scene format of our own, and no runtime graph living beside the stage:

- **A node is a prim.** Each chunk is a `Chunk`-typed prim; its params are `inputs:`
  attributes on it, and its position is `UsdUINodeGraphNodeAPI`.
- **An edge is a USD connection.** Execution order is `inputs:chunk` → `outputs:chunk`. A
  value flowing from one chunk's param into another's is a connection plus an expression.
- **Each chunk authors into its own layer.** Composition *is* the dataflow.

So saving the stage saves the graph, and what a chunk produces is ordinary USD that any
consumer reads — no adapter, no export step, no intermediate format only this project
understands. Hand the result to a renderer, a simulator or another DCC and the chunks
need not be installed at all.

## Runs interactively and headless

A chunk is authored once and runs anywhere the framework does: as a panel, as a node on
the canvas, or from the command line with no UI present at all.

```
usdchunks run layout.usda Transform.translate_x=5 -o result.usda
usdchunks run layout.usda --procedural -o network.usda
```

The first flattens and exports the result with the Chunk prims stripped out; the second
keeps them, so the network reloads and re-cooks.

Procedural chunks regenerate their output on load, so a network is a recipe rather than a
bake. Chunks that write to disk do so through a separate `commit` hook, once the values
they're writing have stopped moving — see [`commit()`](writing-a-chunk.md#commit).

## Guides

1. [Install](install.md) — quick start, `pip install`, every environment variable,
   building the docs.
2. [Using the node graph](node-graph-basics.md) — creating and wiring nodes, shortcuts.
3. [Chunks, params and groups](chunk-basics.md) — what a Chunk is, a worked example, the
   param and flag reference.
4. [View and ChunkGroup](core-chunks.md) — the two chunks that shape a graph rather than
   doing anything to the stage themselves.
5. [Writing a chunk](writing-a-chunk.md) — a chunk module's `main()`, `config()`,
   `execute()` and `commit()`.

## Building a chunk at runtime

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

### What a save keeps

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
loaded.
