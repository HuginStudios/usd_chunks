# Unified undo: NodeGraphQt undo + graph re-execution

## What
When the user presses Ctrl+Z, NodeGraphQt undoes a graph structure change (move, connect,
delete). The USD scene must also be restored — by re-executing the full graph after each undo.

## Where
- `scripts/node_graph/graph_widget.py` — intercept NodeGraphQt's undo stack

## Implementation
NodeGraphQt uses a `QUndoStack` internally. Intercept it:
```python
undo_stack = self._graph._undo_stack  # or self._graph.undo_stack()
undo_stack.indexChanged.connect(self._on_undo_index_changed)

def _on_undo_index_changed(self, index):
    # Fired after every undo or redo
    QtCore.QTimer.singleShot(0, self.execute_graph)
```

## Notes
- Verify the attribute name: `python -c "from NodeGraphQt import NodeGraph; g = NodeGraph(); print([a for a in dir(g) if 'undo' in a.lower()])"`
- `indexChanged` fires on both undo and redo — re-executing covers both cases.
- Debounce is not needed here: undo is a discrete action, not a rapid-fire event.
- `execute_graph()` re-runs all nodes in topo order from scratch, restoring USD scene state.
