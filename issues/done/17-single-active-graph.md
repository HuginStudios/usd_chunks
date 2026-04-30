# Enforce single active graph

## What
Prevent opening a second `NodeGraphWidget` while one is already open. The active instance
should be accessible via a class-level reference so other code can check for it.

## Where
- `scripts/node_graph/graph_widget.py` — `NodeGraphWidget` class and wherever it is
  instantiated (likely `scripts/node_graph_chunk.py`)

## Implementation

### Class-level singleton
```python
class NodeGraphWidget(QtWidgets.QWidget):
    _active_instance = None  # class-level ref to the open graph

    def __init__(self, parent=None, stage=None):
        if NodeGraphWidget._active_instance is not None:
            raise RuntimeError("A node graph is already open. Close it before opening another.")
        super().__init__(parent, stage=stage)
        NodeGraphWidget._active_instance = self

    def closeEvent(self, event):
        NodeGraphWidget._active_instance = None
        super().closeEvent(event)
```

### At instantiation site (`node_graph_chunk.py`)
Wrap creation in a try/except and show a message if already open:
```python
try:
    widget = NodeGraphWidget(stage=stage)
except RuntimeError as e:
    QtWidgets.QMessageBox.information(None, "Node Graph", str(e))
    return
```

### Access from other code
```python
from scripts.node_graph.graph_widget import NodeGraphWidget
active = NodeGraphWidget._active_instance  # None if no graph open
```

## Notes
- This is a soft singleton — it only prevents opening via the normal chunk UI path.
  Direct instantiation in tests etc. still works by catching the RuntimeError.
