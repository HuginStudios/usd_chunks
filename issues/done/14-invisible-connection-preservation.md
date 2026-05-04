# Preserve invisible connections when chunk node ports collapse

## What
When a chunk node's param group collapses and hides its ports, existing wire connections on
those ports must stay intact (not disconnected). Show a small indicator dot on the fold toggle
button when hidden ports have active connections.

## Where
- `scripts/node_graph/nodes.py` — `ChunkWrapperBase` (around line 3700), specifically the
  fold toggle logic (▶/▼ button, `_toggle_ports`)
- `scripts/node_graph/graph_widget.py` — `_neon_paint` or the fold toggle widget repaint

## Implementation

### Port invisibility (already partially exists)
NodeGraphQt's `port.set_visible(False)` hides a port from the canvas but preserves its
connections. Verify this is already being used for the chunk node fold toggle; if not, switch
from removing ports to calling `set_visible(False)`.

### Indicator dot
After toggling port visibility, check if any hidden port has connections:
```python
has_hidden_wires = any(
    not p.visible() and p.connected_ports()
    for p in list(node.input_ports()) + list(node.output_ports())
)
```
If `has_hidden_wires`, render a small filled circle (e.g. 6px, colour `PORT_COLORS["trigger"]`)
on the fold toggle button. Clear it when no hidden ports have connections.

## Notes
- The fold toggle is currently a text button embedded as a `NodeBaseWidget`. The dot can be
  drawn by overriding the button's `paintEvent` or by appending a Unicode bullet to the label.
- Check `_make_chunk_node` in `nodes.py` (around line 3695) for the fold toggle implementation.
