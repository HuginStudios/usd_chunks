# usd_chunks — AI Agent System Overview

## What this project is
`usd_chunks` is a plugin system for usdview that lets you create interactive UI panels ("Chunks") without writing Qt boilerplate. This project extends it with a visual node graph (and, in ongoing local work not yet part of the shared repo, AI capabilities).

To understand this project better, read README.md

## Main systems

### 1. Chunk System (`usd_chunks/`, `usd_chunks/plugins/`, `scripts/`)
The core framework. A "Chunk" is a Python module that returns params (sliders, menus, buttons, etc.) and callbacks. The framework handles UI generation, layer management, and notice-based reactivity.

**Key concepts:**
- **Params**: `Slider`, `Spinbox`, `Menu`, `LineEdit`, `Checkbox`, `Button`, `FileBrowser`, etc.
- **Groups**: Collapsible containers for params. Can be shown/hidden dynamically.
- **Callbacks**: Functions triggered by param changes via the notice system (`PARAM_CHANGED`, `PARAM_ADDED`, `INPUT_CHANGED`).
- **ChunkLayerManager**: Each chunk gets its own `Sdf.Layer` for edits. Access via `chunk.get_edit_context()` which returns the layer directly.
- **DagNode system** (`chunk_notice.py`): Dependency graph between params. `add_dependency(A)` creates A→B connection. `iter_changes()` yields what changed. `INPUT_CHANGED` notice fires when dependencies update.
- **Prim selection**: `LineEdit` with `PRIM_SELECTION` flag. Use `set_prims()` / `get_prims()` to pass prims between params.
- **Widget factory**: `usd_chunks/plugins/chunk_qt.py` → `ChunkQTWidgetFactory` creates Qt widgets from params. `ChunkWidget(chunk_obj)` creates the full UI panel.

**Key files:**
- `usd_chunks/chunk.py` — param classes (Slider, Menu, Button, etc.)
- `usd_chunks/chunk_core.py` — base classes, ChunkParamBase, ChunkGroupBase
- `usd_chunks/chunk_notice.py` — notice system, DagNode, callbacks
- `usd_chunks/chunk_flags.py` — ParamType, ParamFlags, GroupFlags, State
- `usd_chunks/chunk_registry.py` — ChunkRegistry singleton, ChunkLayerManager
- `usd_chunks/plugins/chunk_qt.py` — Qt widget factory, ChunkWidget, ContainerWidget

### 2. Node Graph (`usd_chunks/plugins/chunk_qt_nodegraph.py`, `chunk_graph_layout.py`)
A visual node-based editor embedded in usdview as a chunk panel. A custom `QGraphicsScene`/`QGraphicsView` implementation — does not use the NodeGraphQt library.

**Key concepts:**
- **Chunk prims ARE nodes**: `ChunkGraphScene.build()` / `_rebuild_nodes()` walks the USD stage under `/chunks` and creates one `ChunkNodeItem` per Chunk-typed prim (including inactive/disabled ones). Ports are derived from USD attributes: `inputs:chunk`/`outputs:chunk` (exec) plus typed `inputs:*`/`outputs:*` attributes.
- **Real widgets embedded on canvas**: a node's body is the actual `ChunkWidget` (the same one used in the params panel) dropped onto the graph via a proxy widget — not a separate re-implementation of param UI.
- **Position lives in USD**: node position reads/writes through `UsdUINodeGraphNodeAPI`, not a separate save file.
- **View-slot system**: each graph "context" (tab or chunkgroup) has up to 3 view slots (`1`/`2`/`3`); the active view drives execution (`_recook_active_view`); inactive views are dimmed, stale results highlighted amber.

**Key features:**
- Keyboard: `F` frame selection, `Esc` cancel wire-drag/defocus, `Delete` delete selected, `D` toggle disable, `V` create view node, `L` auto-layout selected-or-all.
- Drag-to-wire port connections; dragging a node onto an existing wire splices it in; a fast back-and-forth "shake" drag disconnects all of a node's wires with exec-chain healing.
- Right-click chunk menu creates + auto-connects a new node from a dropped wire.
- Disabled chunks stay visible (greyed), not hidden. Locked nodes (from external/reference layers) get a distinct amber header and can't be moved by auto-layout, but still act as obstacles for it.
- "Dive into chunkgroup" nesting — new chunks created while inside a group nest into it.

**Key files:**
- `usd_chunks/plugins/chunk_qt_nodegraph.py` — scene/view/node/port/edge classes (`ChunkGraphScene`, `ChunkGraphView`, `ChunkNodeItem`, `ChunkPortItem`, `ChunkEdgeItem`), USD build/rebuild, keyboard shortcuts, view-slot system, `_ProxyKeyRouter` (routes keyboard events to the focused embedded widget)
- `usd_chunks/plugins/chunk_graph_layout.py` — pure-Python auto-layout math (topological depth → columns, target-Y averaging → rows), no Qt/USD dependency, unit-testable in isolation. Ported from a since-removed predecessor (`scripts/node_graph/_graph_layout.py`, history at git branch `feat/node-graph`) with its group/backdrop-stacking logic dropped — chunkgroups here are subnets, not spatial containers.

## Key architecture decisions

### Layer management
- Chunk edits → per-chunk layer via `ChunkLayerManager` (access: `chunk.get_edit_context()`)
- Node graph nodes → the same per-chunk layer, since a node wraps the real chunk object directly (no separate adapter layer)

### Thread safety
- A background-thread call into Qt/USD must hop to the main thread via `QTimer.singleShot(0, fn)` (+ `threading.Event` if the caller needs to block for a result) — used throughout `chunk_qt*.py`, `chunk_usd.py`, `layer_stack.py`
- Node graph: all execution on main thread

## File map
```
usd_chunks/                    ← core chunk framework
  chunk.py                     ← param classes
  chunk_core.py                ← base classes
  chunk_notice.py              ← notice system, DagNode
  chunk_flags.py               ← type/flag enums
  chunk_registry.py            ← registry, ChunkLayerManager
  plugins/
    chunk_qt.py                 ← Qt widget factory, ChunkWidget
    chunk_qt_nodegraph.py       ← node graph scene/view/nodes
    chunk_graph_layout.py       ← auto-layout math
    chunk_usdview/               ← usdview plugin registration
scripts/                       ← individual chunk modules (transform, rbd_solver, edit_target, ...)
  modelling/                   ← mesh creation chunks
```

## Common issues
- **Qt crash from a background thread**: use `QTimer.singleShot(0, fn)` + `threading.Event.wait()` to hop back to the main thread.
- **PySide6 `QPixmap.scaled()`**: returns a `QImage`, not a `QPixmap`. Use `drawImage()` or `QPixmap.fromImage()`.

## Future direction
- **Kraken**: Standalone software with OpenUSD viewport + chunks. Not tied to usdview. Available for Houdini, Maya, etc.
- **Layer-first flow**: Move toward Solaris-style layer composition as the primary data flow in the node graph.
- **Dynamic chunk ports**: Ports that appear/disappear based on chunk param visibility state.
- **3D library chunk**: Browse/drag-drop 3D assets with thumbnails.
- **AI animation**: Prompt-driven animation of objects and rigs.
- **AI systems** (embedded chat, MCP/Kiro integration, render preview): exist as local, in-progress work, not yet part of the shared repo — will get their own section here once committed.

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Using default label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
