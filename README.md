# usd_chunks

![image](docs/assets/chunks_view.png)

**A framework for building procedural USD tools — no Qt, no plugin boilerplate, no scene
format of your own.**

A *Chunk* is a short Python module that declares its parameters and what to do with them.
usd_chunks turns that one declaration into a node in a graph, a panel of widgets, and a
command-line invocation, for free:

- **The graph is USD.** A node is a prim, an edge is a USD connection — no adapter, no
  export step. Saving the stage saves the graph.
- **Runs everywhere.** The same chunk works as a panel, a node on the canvas, or
  headless from the command line — nothing to rewrite.
- **Self-describing.** The node graph, the param panels and the viewport gizmos are all
  generated from one declaration.
- **Just Python.** No Qt, no plugin registration, no serialization format, no undo
  stack, no CLI to hand-write.

No UI, no problem — the same network runs headless from the command line:

```
usdchunks run layout.usda Transform.translate_x=5 -o result.usda
```

## 🚀 Getting started

1. **[Install it](docs/install.md)** — `pip install`, every environment variable,
   troubleshooting what won't show up.
2. **[Use the node graph](docs/node-graph-basics.md)** — drop your first chunk in a
   minute.
3. **[Learn what a Chunk is](docs/chunk-basics.md)** — params, groups, flags, a worked
   example.
4. **[Meet View and ChunkGroup](docs/core-chunks.md)** — the two chunks every network
   needs.
5. **[Write your own chunk](docs/writing-a-chunk.md)** — `main()`, `config()`,
   `execute()`, `commit()`.

## 🧩 ChunkGroup — the subnet container

A ChunkGroup holds a whole network inside a single node on the outer graph — double-click
to dive in, build, dive back out. → [docs/core-chunks.md#chunkgroup](docs/core-chunks.md#chunkgroup)

## 🎬 View — what actually runs

Nothing in a network cooks until it's wired into a View. It decides what runs, and it's
how you switch between several results in the same graph. →
[docs/core-chunks.md#view](docs/core-chunks.md#view)

## 📦 What comes with it

[`scripts/`](scripts/) ships the chunks this has been built and tested against —
transforms, references, materials, MaterialX shading, scattering, a MuJoCo rigid-body
solver, and more. Most are a few hundred lines each, and doubly worth reading: they're
also the worked examples.

## 🕰️ Where it came from

Built to spare every [usdview](https://openusd.org/release/toolset.html#usdview) plugin
its own boilerplate, `plugInfo.json` and Qt code. That problem is solved, and it's no
longer the point — a chunk describes an operation on a stage, and usdview is just one
place to run it.

## 🔒 A note on trust

A chunk is arbitrary Python running with the host's full privileges — the same trust
model as `pip`. Opening a `.usda` can't introduce code you don't already have installed,
but it does decide what your chunks do. See [SECURITY.md](SECURITY.md) before opening a
network from someone else.

## 🤝 Contributing

This repo is the runtime core — not a place to publish chunks of your own. See
[CONTRIBUTING.md](CONTRIBUTING.md) for getting set up, running tests, and opening a PR.

## 📄 License

Apache License 2.0 — see [LICENSE](LICENSE). The same license OpenUSD itself ships under.
