# Rewrite to_python export as chunk bootstrap

## What
Replace the current `to_python` export (which generates raw USD API calls but falls back to
`# (no code generation)` for most nodes) with a chunk bootstrap exporter. The output is a
standalone Python script that:
1. Imports `usd_chunks`
2. Creates a stage
3. For each chunk node: imports the chunk module, instantiates the chunk, sets its params, calls execute
4. For each pure USD node (PrimPath, SetVariant, etc.): emits the existing `to_python` code
5. Saves the stage to a `.usd` file

## Where
- `scripts/node_graph/graph_widget.py` — `_export_python` method (around line 1950)

## Implementation
```python
def _export_python(self):
    path, _ = QtWidgets.QFileDialog.getSaveFileName(
        self, "Export Python", "", "Python (*.py)")
    if not path:
        return
    lines = [
        "from pxr import Usd, UsdGeom, Sdf, Gf",
        "from usd_chunks import chunk, chunk_registry",
        "",
        "stage = Usd.Stage.CreateInMemory()",
        "",
    ]
    g = self._active_graph()
    # Topo-sort nodes
    visited, order = set(), []
    def _topo(n):
        if n.id in visited: return
        visited.add(n.id)
        for p in n.input_ports():
            for cp in p.connected_ports(): _topo(cp.node())
        order.append(n)
    for n in g.all_nodes(): _topo(n)

    for node in order:
        lines.append(f"# {node.name()}")
        chunk_module = getattr(node, "_chunk_module_name", None)
        if chunk_module:
            # Chunk node bootstrap
            lines.append(f"_chunks = chunk.create_chunks('{chunk_module}')")
            lines.append(f"_c = _chunks[0] if _chunks else None")
            for k, v in node.model.custom_properties.items():
                if k not in ("id", "name", "color", "disabled", "selected",
                             "width", "height", "icon", "pos", "type_", "thumb"):
                    lines.append(f"if _c: _c.get_param('{k}') and setattr(_c.get_param('{k}'), 'value', {v!r})")
        else:
            # Regular node
            var_map = {}
            lines.append(node.to_python(var_map))
        lines.append("")

    lines += ["stage.GetRootLayer().Export(stage.GetRootLayer().realPath or 'output.usda')"]
    with open(path, "w") as f:
        f.write("\n".join(lines))
```

## Notes
- `_chunk_module_name` needs to be set on chunk nodes in `_make_chunk_node` (already exists
  as the `module_name` arg — just assign it: `self._chunk_module_name = module_name`).
- The bootstrap code does not reproduce the exact node wiring — it's a linearised script.
  Wire dependencies are implicit in the execution order.
