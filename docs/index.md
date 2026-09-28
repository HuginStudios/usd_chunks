# USD Chunks

usd_chunks turns a short Python module into a node in a graph, a panel of widgets, and a
command you can run from the terminal. Chunks are `.py` files that load at runtime from
the folders you point it at, in usdview or another host.

New here? The [quick start](install.md#quick-start) takes you from nothing to a chunk on
screen.

## Guides

1. [Install](install.md): quick start, fixing things that don't show up, every
   environment variable, building the docs.
2. [Using the node graph](node-graph-basics.md): creating and connecting nodes, shortcuts.
3. [Chunks, params and groups](chunk-basics.md): what a Chunk is, an example, and a list of
   params and flags.
4. [View and ChunkGroup](core-chunks.md): the two chunks that organise a graph instead of
   changing the stage.
5. [Writing a chunk](writing-a-chunk.md): a chunk module's `main()`, `config()`,
   `execute()` and `commit()`.
6. [Building a chunk at runtime](runtime-chunks.md): try an idea in the Python
   interpreter before making it a module.

> In short: a chunk is a Python module. `main()` sets up its params, `execute(node)` does
> the work, and `config()` holds details like its `description` and `category`.

## The graph is USD

usd_chunks has no file format of its own, and doesn't keep a separate graph next to the
stage:

- **A node is a prim.** Each chunk is a `Chunk`-typed prim. Its params are `inputs:`
  attributes on the prim, and its position is stored with `UsdUINodeGraphNodeAPI`.
- **A wire is a USD connection.** Execution order follows `inputs:chunk` → `outputs:chunk`.
  Passing a value from one chunk's param to another is a connection plus an expression.
- **Each chunk writes into its own layer.** The layers compose into the final result.

So saving the stage saves the graph. What a chunk makes is ordinary USD, so a renderer,
simulator or another DCC can read the result without usd_chunks installed.

## Interactive or headless

You write a chunk once and it runs as a panel, as a node, or from the command line with no
UI at all:

```
usdchunks run layout.usda Transform.translate_x=5 -o result.usda
usdchunks run layout.usda --procedural -o network.usda
```

The first command flattens the result and removes the Chunk prims. The second keeps them,
so the network can be opened again and re-cooked.

Procedural chunks rebuild their output when the file loads, so a saved network stores the
steps, not a baked result. Chunks that write files to disk do it in a separate `commit`
function that only runs once the values have settled. See
[`commit()`](writing-a-chunk.md#commit).

## Background

usd_chunks started as a way to add tools to usdview without writing a `plugInfo.json` and
Qt code for every one. It has grown past that: a chunk works on a USD stage, and usdview is
one of several places you can run it.
