# Export group node as chunk module

Status: ready-for-agent

Depends on: issue #20 (param promotion must land first)

## What
Right-click a GroupNode → "Export as Chunk". Generates a `.py` chunk module from the group's
inner nodes, writes it to `~/.usd_chunks/user_chunks/`, and auto-registers it with
ChunkRegistry so it appears in the chunk panel immediately — no restart.

## Where
- `scripts/node_graph/graph_widget.py` — right-click menu entry on GroupNode
- `scripts/node_graph/_graph_export.py` — `export_group_as_chunk(group_node, out_dir)`
- `usd_chunks/chunk_registry.py` — `load_chunk_module()` or equivalent for hot-register

## Implementation
Topo-sort inner nodes (same pattern as `_exec_group`). Generate `main()` with one chunk param
per `_promotions` entry using stored `chunk_param_class`/`chunk_param_kwargs`. Generate a single
`execute()` callback that runs each inner node's `to_python()` inline, substituting promoted
param values and baking non-promoted params as literals. Generate `config()` with
`StandardCategory.CUSTOM`.

If an inner node has no `to_python()`, emit a `# TODO` comment instead of failing.

Filename: `snake_case(group_node.name()) + ".py"`. Re-export overwrites silently.

## Decisions
- Self-contained: generated code uses raw USD API, no `create_chunks()` calls.
- Group node stays in graph unchanged after export — it's the editable source.
- Name collision: overwrite and re-register silently.

## Acceptance
- Export a group with 3 inner nodes and 2 promoted params → valid `.py` that loads as a chunk.
- Chunk appears in panel without restart.
- Re-export overwrites cleanly.
