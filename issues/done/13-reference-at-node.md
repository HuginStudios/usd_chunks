# ReferenceAt node — reference a layer at a different prim path

## What
Takes a `layer` input and a target prim path string, and adds the layer as a reference on a
new prim at that path. Output is the new `Usd.Prim`.

## Where
- `scripts/node_graph/nodes.py` — add near `MergeLayersNode` (Composition category)

## Implementation
```python
class ReferenceAtNode(UsdNodeBase):
    """References a layer onto a prim at a given path — reuse authored content at any scene location."""
    NODE_NAME = "Reference At"
    CATEGORY = "Composition"

    def __init__(self):
        super().__init__()
        self.add_input("layer", color=PORT_COLORS["layer"])
        self.add_text_input("prim_path", "Prim Path", text="/ReferenceAt")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        from pxr import Sdf, UsdGeom
        layer = self._get_input_value("layer")
        path = self.get_property("prim_path") or "/ReferenceAt"
        if not layer:
            return {"prim": None}
        prim = stage.OverridePrim(path)
        prim.GetReferences().AddReference(layer.identifier)
        return {"prim": prim if prim.IsValid() else None}
```

## Notes
- Use `stage.OverridePrim` (not `DefinePrim`) so repeated execution doesn't duplicate opinions.
- `layer.identifier` is the anonymous layer path — valid within the same process lifetime.
- Register in `ALL_NODES`.
