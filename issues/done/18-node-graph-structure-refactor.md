# Node graph — structural refactor (Pass 1)

Status: ready-for-agent

## What
Break the node graph into focused modules and introduce a central `GraphExecutor` that owns
all execution logic. Currently `graph_widget.py` is 3201 lines handling execution, toolbar, side
panel, theming, undo/redo, drag-drop, and AI — all in one file. `nodes.py` is 4226 lines with
80+ node types. This makes bugs hard to isolate and improvements risky.

### 1. Extract `GraphExecutor` (`scripts/node_graph/graph_executor.py`)

New class `GraphExecutor` that owns:
- **Dirty tracking**: each node stores `_output_hashes: dict[str, int]` (hash of last output value
  per port). A node is dirty if any upstream output hash differs from what it saw last run.
- **Topological sort**: same logic as current `execute_graph()` but extracted.
- **Execution loop**: iterate topo-sorted dirty nodes only, call `node.execute(stage)`,
  call `QApplication.processEvents()` after each node so the UI stays responsive.
- **Execution entry points**:
  - `run_full()` — execute all nodes (initial load, undo/redo)
  - `run_from(node)` — dirty-mark node + all downstream, then run only dirty subgraph
  - `mark_dirty(node)` — nodes and event triggers call this instead of self-executing
- **Output cache**: move `_output_cache` dict from widget to executor.
- **Layer registry**: move `_node_layer_registry` to executor.

Nodes and event triggers must NOT call `_trigger_downstream()` or `_execute_from_node()`
directly. They call `executor.mark_dirty(self)` and the executor schedules the run via a
150ms debounce QTimer (same as current debounce, but now in one place).

### 2. Split `nodes.py` into a package (`scripts/node_graph/nodes/`)

```
scripts/node_graph/nodes/
  __init__.py        ← registry dict + re-exports; imports from all category files
  _base.py           ← UsdNodeBase, ChunkWrapperBase, _TriggerNodeBase, port helpers
  _input.py          ← PrimPathNode, StringNode, FloatNode, BoolNode, SelectedPrimNode
  _query.py          ← GetVariants, GetPrimInfo, GetAttribute, GetChildren, etc. (29 nodes)
  _transform.py      ← SetTransform, GetWorldTransform, ConstrainTransform, etc.
  _hierarchy.py      ← CreatePrim, DeletePrim, ReparentPrim, DuplicatePrim
  _composition.py    ← AddReference, AddPayload, MergeLayers
  _create.py         ← Cube, Sphere, Cylinder, Cone, Capsule, Plane, Xform, PointInstancer
  _material.py       ← CreatePreviewSurface, TextureFile, AssignMaterial, etc.
  _lighting.py       ← CreateCamera, CreateRectLight, etc.
  _utility.py        ← ListIndex, ForEach, Merge, Compare, Switch, StringFormat, etc.
  _math.py           ← Add, Subtract, Multiply, Divide, Clamp, Remap
  _code.py           ← PythonNode
  _event.py          ← OnSelectionChanged, OnFrameChanged, OnTimer, OnStageChanged, etc.
  _debug.py          ← PrintNode
```

`nodes/__init__.py` exports `NODE_REGISTRY` (the dict currently built at module level in
`nodes.py`) and `_make_chunk_node`. All existing import sites (`graph_widget.py`,
`node_graph_chunk.py`) import from `scripts.node_graph.nodes` — no change needed there.

### 3. Slim `graph_widget.py`

Remove from `graph_widget.py`:
- `execute_graph()`, `_execute_node()`, `_execute_from_node()` → moved to `GraphExecutor`
- `_output_cache`, `_node_layer_registry` → owned by `GraphExecutor`

`NodeGraphWidget` holds a `self._executor = GraphExecutor(self)` and delegates all
execution calls to it. Qt signals/slots that currently call execution methods call
`self._executor.run_from(node)` or `self._executor.mark_dirty(node)` instead.

## Where
- `scripts/node_graph/graph_widget.py` — extract execution logic
- `scripts/node_graph/nodes.py` — split into `nodes/` package
- New file: `scripts/node_graph/graph_executor.py`
- `scripts/node_graph_chunk.py` — import paths may need updating after nodes split

## Decisions
- `GraphExecutor` is a plain Python class (not a QObject); it holds a reference to the
  widget only for `processEvents()` and state badge updates.
- Dirty comparison: `hash(str(value))` is sufficient. No deep equality needed.
- Keep `ForEachNode` iteration logic in the executor (it already has special-case handling
  there); do not move it into the node.
- Event/trigger nodes: replace `_trigger_downstream()` body with `self._executor.mark_dirty(self)`.
  The executor's debounce timer coalesces rapid fires (e.g. OnTimer at 100ms interval).
- Remove dead code during split: commented-out `UVCheckerNode` block, all `[DBG]` print
  statements in chunk node `execute()`.
- Do not change any node's public API (`execute()`, `get_property()`, port names) — this is
  a structural move, not a behaviour change.

## Acceptance
- All existing graph JSON files (`chunk_hybrid_demo.json`, `test_tiled_material.json`,
  `test_parent_constraint_ref.json`) load and execute without error after the refactor.
- No `[DBG]` strings remain in any file under `scripts/node_graph/`.
- `graph_widget.py` is under 2000 lines.
- `graph_executor.py` has no Qt widget imports (only `QApplication.processEvents` and
  `QTimer` are allowed for the debounce).
