# Remove chunk node's Sdf.Layer from stage when node is deleted

## What
When a chunk node is deleted from the graph, its `Sdf.Layer` (from `chunk.get_edit_context()`)
must be removed from the stage's layer stack so its USD contributions disappear from the scene.

## Where
- `scripts/node_graph/graph_widget.py` — connect to `self._graph.nodes_deleted` signal (or
  `node_deleted` if available in the NodeGraphQt version in use).

## Implementation
```python
self._graph.nodes_deleted.connect(self._on_nodes_deleted)

def _on_nodes_deleted(self, node_list):
    for node in node_list:
        try:
            layer = node.adapter.chunk.get_edit_context()  # or node._chunk_layer
            if layer and self._stage:
                root = self._stage.GetRootLayer()
                if layer.identifier in root.subLayerPaths:
                    paths = list(root.subLayerPaths)
                    paths.remove(layer.identifier)
                    root.subLayerPaths = paths
        except Exception:
            pass
```

## Notes
- Chunk nodes store the chunk via `node._adapter` or via `node._ensure_chunk()`.  Check `ChunkWrapperBase` in `nodes.py` (around line 3700) for the actual attribute name.
- Also check the `ChunkLayerManager` in `chunk_registry.py` — it may have a `remove_layer` helper.
- Signal name: verify it is `nodes_deleted` vs `node_deleted` for the installed NodeGraphQt version:
  `python -c "import NodeGraphQt; print([s for s in dir(NodeGraphQt.NodeGraph) if 'delet' in s.lower()])"`
