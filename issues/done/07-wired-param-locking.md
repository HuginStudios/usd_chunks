# Visually lock ChunkWidget params driven by an incoming wire

## What
When a port connection drives a chunk param (i.e. an auto-port `_name` has a connected wire),
the corresponding QWidget in the side panel should be greyed out and read-only so the user
can't edit a value that will be immediately overwritten on the next execution.

Unlock when the wire is disconnected.

## Where
- `scripts/node_graph/graph_widget.py`
  - `_build_props_panel` / `_build_chunk_panel` (around line 1750–2120) — where param widgets are created
  - `_on_ports_connected` / `_on_ports_disconnected` (around line 1637–1665) — where to trigger lock/unlock

## Implementation
1. After building the side panel, call `_refresh_param_lock_state(node)` to set initial state.
2. In `_on_ports_connected` / `_on_ports_disconnected`, re-call `_refresh_param_lock_state` for the affected node.
3. `_refresh_param_lock_state(node)`:
   - For each input port named `_<param>`: if `port.connected_ports()`, find the widget for `<param>` and call `widget.setEnabled(False)` + set a faint overlay or tooltip "driven by wire".
   - Otherwise `widget.setEnabled(True)`.

## Notes
- Widgets are stored in `self._prop_editors` (QLineEdit map) or built fresh each panel rebuild.
- Rebuilding the panel is the simplest approach: call `_on_node_selected(node)` after connect/disconnect to rebuild.
- The chunk panel (`_build_chunk_panel`) uses the real `ChunkWidget` — for those, find the underlying Qt widget for each param and call `setEnabled(False)`.
