# Type-aware AI node suggestions

## What
When the user opens the Tab menu while dragging from a typed port, filter the suggestion list
to only show nodes that have a compatible input (when dragging from an output) or output
(when dragging from an input) port.

Also filter AI ghost button suggestions by the same rule.

## Where
- `scripts/node_graph/graph_widget.py`
  - `_TabSearchDialog.__init__` (around line 2840) — already receives `port_type` arg
  - `_show_ghost_buttons` (search for `_show_ghost_buttons`) — AI suggestions after node select
  - `_show_tab_menu` or the Tab key handler (around line 870)

## Implementation

### Tab menu
1. When Tab is pressed while a port drag is in progress, pass the dragged port to `_TabSearchDialog`.
2. In `_TabSearchDialog`, filter `all_nodes` to those with a port whose `_data_type` is
   compatible with the dragged port's `_data_type` (or `any`).

### Port data type of a node class (not instance)
To filter without instantiating every node class, add a class-level `PORT_TYPES` dict:
```python
class PrimPathNode(UsdNodeBase):
    PORT_TYPES = {"outputs": ["prim", "list", "float"], "inputs": []}
```
Or inspect `ALL_NODES` entries by instantiating them once and caching their port types at
startup. The latter is simpler since the node list is already built at startup.

### Ghost buttons
In `_show_ghost_buttons`, after getting AI suggestions, additionally filter the list by
whether the suggested node has a port compatible with the selected node's output port type.

## Notes
- The `port_type` arg already passed to `_TabSearchDialog` is the NodeGraphQt IN/OUT enum,
  not the data type. Access the data type via `pending_port._data_type` on the viewer.
- `any` data type on either side always passes the filter.
