# Group node: widget hover bug + promote for non-chunk nodes

Status: ready-for-agent

## Bug A — Text widgets appear on hover inside an expanded group

When a group is expanded (double-click) and you hover over inner nodes, their text
input widgets randomly appear on the node body. Same bug that was fixed for the main
graph view. Root cause: `_fold_ports` fires once at node creation but the
expand/collapse cycle recreates QGraphicsItem views, resetting widget visibility.

### Fix
In `_exec_group` (graph_executor.py), after `widget._graph.expand_group_node(group)`,
re-hide text widgets on all inner chunk nodes:
```python
for n in sub.all_nodes():
    if hasattr(n, '_folded') and n._folded:
        for w in n.view._widgets.values():
            w.setVisible(False)
```
Also in `NodeGraphWidget._setup_sub_graph` (graph_widget.py), after the sub-graph tab
is shown, do the same pass over all nodes in the sub-graph.

## Bug B — "Promote to Group" missing for non-chunk node params

`_setup_chunk_ref_menu` (which wires up the "Promote to Group" right-click option) is
only called for chunk node params that have `value_reference`. Regular USD nodes (e.g.
SetTransform, CreatePrim) show a plain side panel with `QLineEdit` inputs but no
promote option.

### Fix
In `_setup_prop_ref_menu` (graph_widget.py, around line 1630), which handles the
right-click menu for regular node property inputs, add the same promote check:
```python
from NodeGraphQt.base.graph import SubGraph
g = self._active_graph()
if isinstance(g, SubGraph):
    menu.addSeparator()
    promote_act = menu.addAction("⬆ Promote to Group")
```
On trigger: call `self._promote_prop_to_group(node, prop_name, current_value)`.

Add `_promote_prop_to_group(self, inner_node, prop_name, value)` alongside the
existing `_promote_param_to_group`. It works the same way but stores
`chunk_param_class: "LineEdit"` (safe fallback since we don't have type info for
raw node properties) and sets `default_value` to the current property value.

## Where
- `scripts/node_graph/graph_executor.py` — `_exec_group`, after `expand_group_node`
- `scripts/node_graph/graph_widget.py` — `_setup_sub_graph`, `_setup_prop_ref_menu`,
  add `_promote_prop_to_group`
