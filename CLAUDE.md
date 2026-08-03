# usd_chunks — AI Agent System Overview

## What this project is
`usd_chunks` is a plugin system for usdview that lets you create interactive UI panels ("Chunks") without writing Qt boilerplate. This project extends it with AI capabilities and a visual node graph.

To understand this project better, read README.md

## Three main systems

### 1. Chunk System (`usd_chunks/`, `plugins/`, `scripts/`)
The core framework. A "Chunk" is a Python module that returns params (sliders, menus, buttons, etc.) and callbacks. The framework handles UI generation, layer management, and notice-based reactivity.

**Key concepts:**
- **Params**: `Slider`, `Spinbox`, `Menu`, `LineEdit`, `Checkbox`, `Button`, `FileBrowser`, etc.
- **Groups**: Collapsible containers for params. Can be shown/hidden dynamically.
- **Callbacks**: Functions triggered by param changes via the notice system (`PARAM_CHANGED`, `PARAM_ADDED`, `INPUT_CHANGED`).
- **ChunkLayerManager**: Each chunk gets its own `Sdf.Layer` for edits. Access via `chunk.get_edit_context()` which returns the layer directly.
- **DagNode system** (`chunk_notice.py`): Dependency graph between params. `add_dependency(A)` creates A→B connection. `iter_changes()` yields what changed. `INPUT_CHANGED` notice fires when dependencies update.
- **Prim selection**: `LineEdit` with `PRIM_SELECTION` flag. Use `set_prims()` / `get_prims()` to pass prims between params.
- **Widget factory**: `plugins/chunk_qt.py` → `ChunkQTWidgetFactory` creates Qt widgets from params. `ChunkWidget(chunk_obj)` creates the full UI panel.

**Key files:**
- `usd_chunks/chunk.py` — param classes (Slider, Menu, Button, etc.)
- `usd_chunks/chunk_core.py` — base classes, ChunkParamBase, ChunkGroupBase
- `usd_chunks/chunk_notice.py` — notice system, DagNode, callbacks
- `usd_chunks/chunk_flags.py` — ParamType, ParamFlags, GroupFlags, State
- `usd_chunks/chunk_registry.py` — ChunkRegistry singleton, ChunkLayerManager
- `plugins/chunk_qt.py` — Qt widget factory, ChunkWidget, ContainerWidget

### 2. Node Graph (`scripts/node_graph/`)
A visual node-based editor embedded in usdview as a chunk. Extends the chunk system with a flowchart-style interface.

**Architecture:**
- Built on **NodeGraphQt** library with heavy customization
- **Entry point**: `scripts/node_graph_chunk.py` → creates the graph widget
- **Dark neon theme** with custom `_neon_paint` for all nodes
- **~60+ node types** across categories: Input, USD, Transform, Material, Lighting, Camera, Animation, Math, Code, Utility, Event, Chunks

**Key features:**
- **Chunk integration**: Any chunk module auto-generates a node (`_make_chunk_node`). The side panel shows the real `ChunkWidget` — same UI as the usdview chunk panel. Chunk nodes have foldable ports (▶/▼ icon).
- **Hybrid output system**: Chunk nodes output both `prim` (the primary created prim) and `layer` (the chunk's full `Sdf.Layer` edit). Layer output enables Solaris-style composable workflows.
- **Layer inspection**: Right-click chunk node → "📄 Show Layer Edits" shows the USD text of everything the chunk authored.
- **Prim thumbnails**: PrimPathNode captures a viewport screenshot of the prim and displays it on the node. Toggle with 📷 Thumbs button.
- **Execution states**: Nodes show running (amber pulse), valid (green), none (grey-blue), error (red) with badges and stripe colors. Auto-clears after 1.5s.
- **Trigger ports**: Event nodes have a purple `trigger` output that causes downstream execution without overriding data. Connect trigger→any input to trigger flow while keeping the node's own values.
- **Event nodes**: OnSelectionChanged, OnFrameChanged, OnTimer, OnStageChanged, OnPrimChanged, OnAttributeChanged — all fire `_trigger_downstream()` to re-execute connected nodes.
- **Smart layout** (L key): Connection-aware — nodes placed by depth, Y-aligned to upstream connections, source nodes pulled close to their targets.
- **Drag & drop**: Drop prims from usdview hierarchy → creates PrimPathNode. Drop attributes → creates PrimPath + GetAttribute connected. Drop images → creates TextureFileNode.
- **AI suggestions**: Tab menu and ghost buttons suggest next nodes based on graph context (via ollama).
- **Python node**: Write arbitrary Python with `inputs` dict and `output` variable. Ctrl+Space for AI code completion.
- **Group nodes**: Select nodes → Ctrl+G to group into a sub-graph.

**Key files:**
- `scripts/node_graph/graph_widget.py` — main widget, toolbar, execution engine, side panel, all monkey-patches
- `scripts/node_graph/nodes.py` — all node class definitions, port colors, chunk node auto-generation
- `scripts/node_graph/chunk_adapter.py` — bridges chunks into the node graph
- `scripts/node_graph/ai_suggest.py` — AI-powered node suggestions

**Node graph ↔ Chunk relationship:**
- The node graph is an **extender** of chunks, not a replacement
- Chunk nodes wrap real chunks via `ChunkAdapter` — the chunk's callbacks and notice system still drive the actual USD edits
- The node graph adds: visual wiring, execution ordering, data flow between chunks, layer output
- Side panel shows the real `ChunkWidget` so users get the same editing experience as the chunk panel
- Chunk edits go to the chunk's own layer (via `ChunkLayerManager`), not the session layer

### 3. AI Systems

#### Embedded AI Chat (`scripts/ai_chat/`)
An AI chat widget embedded directly in usdview's chunk panel.
- **Backends**: ollama (local, qwen3:8b) or Anthropic Claude API
- **Tools**: ~30 USD tools (find_prims, set_variant, duplicate_prim, place_on_top, etc.)
- **Preprocess**: pattern matching for common commands (select, frame, switch variant) — bypasses LLM for speed
- **Key files**: `ai_chat/tools.py`, `ai_chat/agent.py`, `ai_chat/widget.py`, `ai_chat/prompts.py`

#### Kiro CLI Integration (MCP)
Kiro CLI connects to usdview via MCP (Model Context Protocol).
- **HTTP MCP server**: `scripts/usd_mcp_server.py` — port 8765, exposes USD tools
- **Kiro web chat chunk**: `scripts/kiro_chunk_web.py` — web UI in QWebEngineView
- **Agent config**: `.kiro/agents/usd-agent.json`
- **Skills**: `.kiro/skills/` — layout-tool.md, create-chunk.md, usd-preview-surface.md

#### Render Preview (`scripts/render_preview.py`)
Captures viewport → generates photorealistic render via Gemini or Stable Diffusion.

## Key architecture decisions

### Layer management
- AI edits → `stage.GetSessionLayer()`
- Chunk edits → per-chunk layer via `ChunkLayerManager` (access: `chunk.get_edit_context()`)
- Node graph chunk nodes → same per-chunk layer (via adapter)

### Thread safety
- MCP server: asyncio thread → Qt via `QTimer.singleShot(0, fn)` + `threading.Event`
- AgentWorker: `QSemaphore` + `run_on_main` signal
- Node graph: all execution on main thread

### Data flow in node graph
- **Prim flow**: nodes pass `Usd.Prim` objects through `prim` ports
- **Layer flow**: chunk nodes output their `Sdf.Layer` for composable workflows
- **Trigger flow**: event nodes output `TRIGGER` sentinel — downstream `get_property()` skips it, preserving the node's own values
- **Auto-ports**: `add_text_input("name")` auto-creates `_name` input port for wiring data into text fields

## File map
```
usd_chunks/                    ← core chunk framework
  chunk.py                     ← param classes
  chunk_core.py                ← base classes
  chunk_notice.py              ← notice system, DagNode
  chunk_flags.py               ← type/flag enums
  chunk_registry.py            ← registry, ChunkLayerManager
plugins/
  chunk_qt.py                  ← Qt widget factory, ChunkWidget
  chunk_usdview/               ← usdview plugin registration
scripts/
  node_graph_chunk.py          ← node graph chunk entry point
  node_graph/
    graph_widget.py            ← main widget, execution, UI
    nodes.py                   ← all node definitions
    chunk_adapter.py           ← chunk ↔ node bridge
    ai_suggest.py              ← AI node suggestions
  ai_chat.py                   ← AI chat entry point
  ai_chat/                     ← AI chat package
  usd_mcp_server.py            ← MCP server
  render_preview.py            ← render preview chunk
  modelling/                   ← mesh creation chunks
.kiro/
  agents/usd-agent.json        ← Kiro agent config
  skills/                      ← on-demand knowledge
```

## Example graphs
- `chunk_hybrid_demo.json` — Add Mesh → prim output + layer output + Python node extracting prims from layer
- `test_tiled_material.json` — Texture → PBR Material → Assign to prim with UV tiling
- `test_parent_constraint_ref.json` — Parent constraint between two prims

## Common issues
- **Chunk param names `width`/`height`**: clash with NodeGraphQt reserved properties on save/load. Avoid these names in chunk params.
- **Qt crash from HTTP thread**: use `QTimer.singleShot(0, fn)` + `threading.Event.wait()`
- **PySide6 QPixmap.scaled()**: returns QImage, not QPixmap. Use `drawImage()` or `QPixmap.fromImage()`.
- **Port visibility**: `port.set_visible()` requires node to be in graph (has undo stack). Defer with `QTimer.singleShot(0, ...)`.

## Future direction
- **Kraken**: Standalone software with OpenUSD viewport + chunks. Not tied to usdview. Available for Houdini, Maya, etc.
- **Layer-first flow**: Move toward Solaris-style layer composition as the primary data flow in the node graph.
- **Dynamic chunk ports**: Ports that appear/disappear based on chunk param visibility state.
- **3D library chunk**: Browse/drag-drop 3D assets with thumbnails.
- **AI animation**: Prompt-driven animation of objects and rigs.

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Using default label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
