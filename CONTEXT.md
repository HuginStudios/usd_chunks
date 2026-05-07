# usd_chunks — Domain Context

## Core concepts

**Chunk** — a Python module that returns params and callbacks. The framework handles UI, layer management, and notice-based reactivity. Chunks are the first-class unit of user-authored USD tooling.

**ChunkParam** — a typed, editable value inside a Chunk (Slider, LineEdit, Menu, Checkbox, Button, etc.). Params live in a DAG so changes propagate to dependents.

**ChunkLayerManager** — assigns each Chunk its own `Sdf.Layer`. Edits from a Chunk go to that layer, not the session layer.

**Node** — a visual box in the node graph. Nodes are one of two kinds:  
- *Hand-written nodes* (PrimPathNode, SetTransformNode, …) — self-contained USD operations defined in `nodes/_*.py`.  
- *Chunk nodes* (auto-generated) — wrap a Chunk and expose its params as ports.

**ChunkNodeFactory** (`nodes/_chunk_factory.py`) — the single place that turns a loaded chunk module into a node class. `make_chunk_node(module_name, chunk_name)` is its public interface.

**ChunkAdapter** (`chunk_adapter.py`) — bridges a Chunk instance into the node execution context. Owns param get/set and button-press dispatch.

**GraphExecutor** (`graph_executor.py`) — owns topological sort, dirty tracking, and the 150 ms debounce timer. Public interface: `run_full()`, `run_from(node)`, `mark_dirty(node)`.

**_dirty_callback** — a callable stored on every `UsdNodeBase` instance, set by `GraphExecutor._exec_node`. Nodes call it to request re-execution without knowing the executor exists. Replaces the former `sys.modules['_node_graph_ref']` global lookup.

**Promoted param** — a param inside a GroupNode's sub-graph that has been surfaced as an input port on the GroupNode itself. Stored in `group_node._promotions` dict, serialized to `model.custom_properties['promoted_params']`.

**PanelBuilder** (`_panel_builder.py`) — builds Qt side-panel rows from a node. Takes `form_layout` and `on_changed` callback; no reference to the executor or widget. Functions: `build_group_promo_panel`, `build_chunk_panel`.

**PropertyPanelFactory** (`_property_panel.py`) — top-level dispatch for the side panel. `build_node_panel(node, form, on_changed, ...)` routes to `build_group_promo_panel` (GroupNode), `build_chunk_panel` (chunk node), or builds property-type rows (code editor, file browser, bool, combo, line edit) for plain nodes. `_on_node_selected` in `graph_widget.py` delegates entirely to this module.

**WiringEngine** — *not yet extracted* (deferred, see ADR below). The logic for connecting/disconnecting ports, resolving chunk params, and calling `wire_from()` currently lives in `graph_widget.py`.

## Key invariants

- AI edits → `stage.GetSessionLayer()`.  
- Chunk edits → per-chunk layer via `ChunkLayerManager`.  
- All node graph execution runs on the main thread.  
- `mark_dirty` is dropped silently while `_is_executing` is True — prevents event-node feedback loops.

## File map (architectural roles)

```
nodes/_chunk_factory.py   ← ChunkNodeFactory — auto-generation of chunk node classes
nodes/_panel_builder.py   ← PanelBuilder — side-panel Qt row builders
_property_panel.py        ← PropertyPanelFactory — top-level node→panel dispatch
nodes/_base.py            ← UsdNodeBase, ChunkWrapperBase, promotion helpers
nodes/_event.py           ← Event/trigger nodes (OnSelectionChanged, OnTimer, …)
graph_executor.py         ← GraphExecutor — topo-sort, debounce, dirty propagation
graph_widget.py           ← Main widget — canvas, toolbar, wiring, side panel dispatch
chunk_adapter.py          ← ChunkAdapter — chunk ↔ node execution bridge
```
