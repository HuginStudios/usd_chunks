# Stop execution at first error; mark downstream as skipped

## What
When `_execute_node` catches an exception, it currently marks the node red and continues.
Change `_execute_from_node` and `execute_graph` to stop at the first errored node and mark
all remaining downstream nodes with a new `"skipped"` exec state.

## Where
- `scripts/node_graph/graph_widget.py`
  - `_execute_node` (around line 1350) — sets state to `"error"`, returns `None`/empty dict
  - `_execute_from_node` (around line 1493) — iterates `order`; stop loop when a node returns error state
  - `execute_graph` (around line 2650) — same pattern

## Implementation
1. In `_execute_node`, on exception: set state `"error"`, return a sentinel like `None` or raise.
2. In both topo-sort loops, track an `errored` set. When a node in `order` had an upstream in `errored`, call `_set_exec_state(node, "skipped")` and skip `_execute_node`.

## Visual state
Add `"skipped"` to `_set_exec_state` in `graph_widget.py`. Render as muted grey-blue with a
small `⊘` badge (similar to how `"error"` shows a red `✕` badge). See `_neon_paint` in
`graph_widget.py` (around line 400–530) for badge rendering.
