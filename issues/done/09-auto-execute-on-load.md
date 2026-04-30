# Auto-execute full graph after load_session

## What
After `self._graph.load_session(path)` completes, immediately call `self.execute_graph()`.
This restores the USD scene to match the loaded graph state (chunk layers are empty until executed).

## Where
- `scripts/node_graph/graph_widget.py` — `_load_graph` method (around line 1940)

## Implementation
```python
def _load_graph(self):
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        self, "Load Graph", "", "Node Graph (*.json)")
    if path:
        self._graph.load_session(path)
        # Restore USD scene — chunk layers are empty until executed
        QtCore.QTimer.singleShot(0, self.execute_graph)
```

The `QTimer.singleShot(0, ...)` defers one event loop cycle to let NodeGraphQt finish
wiring up all nodes and ports before execution begins.
