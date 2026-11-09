---
name: usd-chunks-framework-change
description: The non-obvious invariants of the usd_chunks framework itself -- when and how chunks cook, main-thread rules, the graph stored as USD, the toolkit-free engine vs. frontends, Output and view slots, the shared clipboard, and the permanent callback and module contracts. Use before changing anything under usd_chunks/ (the framework, graph engine, frontends or hosts), not for writing or using chunks.
---

# Change the framework

Read this before you edit anything under `usd_chunks/`. Writing chunks, building networks
and running them don't need it; the rules for those are in `AGENTS.md`.

The graph engine is imported as `from usd_chunks.graph import engine as chunk_graph`;
`chunk_graph.*` below means that module.

## Invariants

- **Cooking**: when a chunk cooks (batching, stale, held cooks, `dispatch`) is
  `chunk_cook.py`, which imports nothing above `chunk_notice`. How (guards, then the
  module into its layer) is `chunk_cook.cook`, reached through `ChunkBase._cook`.
  Skip-cooking is always on.
- **Main thread only for Qt/USD.** A background thread hops back with
  `scene.call_later(fn)` (+ `threading.Event` to block for a result). It's host-neutral
  (usdview: QTimer, Blender: `bpy.app.timers`), and a debounce cancels via its return value.
- **Graph = USD.** Chunk prims under `/chunks` are the nodes; wires are USD connections
  (`inputs:chunk`/`outputs:chunk` exec + typed `inputs:*`/`outputs:*`); position is
  `UsdUINodeGraphNodeAPI`. A node's body is the real `ChunkWidget` via a proxy widget.
  Graph nodes wrap the real chunk, so they use the same layer.
- **Engine / frontends.** `usd_chunks/graph/engine.py` decides what happens to the graph and
  imports no toolkit (a test enforces it). Frontends decide what appears on screen.
  `GraphFrontend` is a Protocol, not an ABC; `test_graph_frontend_contract.py` checks names
  and signatures. Qt's paste/materialize is deliberately unshared.
- **Output and view slots.** Each context (tab/chunkgroup) has one Output (what it
  delivers) and view slots. The active view drives cooking (`chunk_stage.recook_for_view`)
  and persists as `chunk:activeView`; with none active the context cooks to its Output.
  `usdchunks run` cooks to the Output. **Only what the active view/Output reaches cooks**:
  a new, dropped or pasted node cooks when it is wired in. A frontend holds what it
  creates in a driven graph (`chunk_graph.hold_until_wired`; set values after with
  `set_param_visibly`) and recooks after every wire, delete, create or paste
  (`chunk_graph.recook_active`). A graph with no view or Output (a script) cooks everything.
- **Shared clipboard.** `chunk_graph.node_clipboard`/`edge_clipboard` are module-level;
  assign into them (`copy_to_clipboard`), never rebind.
- **Two callback forms are permanent**: `(notice, sender)` and `(owner, notice, sender)`;
  the framework passes whichever the callback declares. Changing that, or the
  `main()`/`config()`/`execute(node)` module contract, breaks every chunk that exists.

## Before you finish

- A bug fix comes with a test that failed before it and passes after.
- Run the tests for the files you touched, then the full suite and `ruff check .`
  (see "Set up, test, lint" in `AGENTS.md`).
- Update `docs/` when the change is something a user would notice.
- Add it to `CHANGELOG.md` under Unreleased (Added, Changed or Fixed). Start the line with
  `**Breaking:**` if an existing script, network or import now behaves differently or
  fails, and say what to change.
