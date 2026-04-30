# MergeLayers node — sublayer composition

## What
A new node that takes two `layer` inputs and outputs a single `layer` that sublayers both.
The result is an anonymous `Sdf.Layer` that lists both inputs as sublayers.

## Where
- `scripts/node_graph/nodes.py` — add near the other Composition nodes (search `CATEGORY = "Composition"`)

## Implementation
```python
class MergeLayersNode(UsdNodeBase):
    """Sublayers two Sdf.Layers into a new anonymous composite layer."""
    NODE_NAME = "Merge Layers"
    CATEGORY = "Composition"

    def __init__(self):
        super().__init__()
        self.add_input("layer_a", color=PORT_COLORS["layer"])
        self.add_input("layer_b", color=PORT_COLORS["layer"])
        self.add_output("layer", color=PORT_COLORS["layer"])

    def execute(self, stage):
        from pxr import Sdf
        layer_a = self._get_input_value("layer_a")
        layer_b = self._get_input_value("layer_b")
        if not layer_a and not layer_b:
            return {"layer": None}
        merged = Sdf.Layer.CreateAnonymous("merged")
        if layer_a:
            merged.subLayerPaths.append(layer_a.identifier)
        if layer_b:
            merged.subLayerPaths.append(layer_b.identifier)
        return {"layer": merged}
```

## Register
Add `MergeLayersNode` to `ALL_NODES` list at the bottom of `nodes.py`.
