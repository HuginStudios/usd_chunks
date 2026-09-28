# USD Chunks

usd_chunks turns a short Python module into a node in a graph, a panel of widgets, and a
command-line invocation — all from one declaration. Chunks (`.py` files) load at runtime
from wherever you point the framework, into usdview or any other host.

New here? [Install](install.md#quick-start) has the copy-paste path from nothing to a
chunk on screen.

## Guides

1. [Install](install.md) — quick start, troubleshooting when something doesn't show up,
   every environment variable, building the docs.
2. [Using the node graph](node-graph-basics.md) — creating and wiring nodes, shortcuts.
3. [Chunks, params and groups](chunk-basics.md) — what a Chunk is, a worked example, the
   param and flag reference.
4. [View and ChunkGroup](core-chunks.md) — the two chunks that shape a graph rather than
   doing anything to the stage themselves.
5. [Writing a chunk](writing-a-chunk.md) — a chunk module's `main()`, `config()`,
   `execute()` and `commit()`.
6. [Building a chunk at runtime](runtime-chunks.md) — try an idea in the interpreter
   before it's worth a module.

> usd_chunks in a nutshell: chunks are written in Python. A chunk describes what it does
> in `execute(node)`, how it looks in `main()`, and its metadata in `config()` — things
> like `description` and `category`. That's it.

## The graph is USD

There is no scene format of our own, and no runtime graph living beside the stage:

- **A node is a prim.** Each chunk is a `Chunk`-typed prim; its params are `inputs:`
  attributes on it, and its position is `UsdUINodeGraphNodeAPI`.
- **A wire is a USD connection.** Execution order is `inputs:chunk` → `outputs:chunk`. A
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

## Where it came from

usd_chunks started as a way to spare every usdview plugin its own boilerplate — a
`plugInfo.json` and hand-written Qt code, tedious for one plugin and unmanageable for
fifty. That problem is solved, and it's no longer the point: a chunk describes an
operation on a stage, and usdview is just one place it can run.
