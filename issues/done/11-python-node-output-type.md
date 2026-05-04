# Python node: user-declared output type

## What
Add a dropdown to the Python node that lets the user declare the type of `output`.
The `result` output port color updates to match. `any` (default) connects to everything.

## Where
- `scripts/node_graph/nodes.py` — `PythonNode` class (search for `class PythonNode`)

## Implementation
1. In `PythonNode.__init__`, add a combo widget:
```python
self.add_combo_menu("output_type", "Output Type",
                    items=["any", "prim", "layer", "string", "float", "list", "bool"],
                    tab_item=False)
```

2. Override `execute` to update the output port color after the type changes:
```python
def execute(self, stage):
    # ... existing exec code ...
    # Update output port type
    dtype = self.get_property("output_type") or "any"
    port = self.get_output("result")
    if port:
        new_color = PORT_COLORS.get(dtype, PORT_COLORS["any"])
        port.view.color = new_color
        try:
            port.view._data_type = dtype
        except Exception:
            pass
    return {"result": local_vars.get("output")}
```

## Notes
- The port color change is visual only — NodeGraphQt doesn't re-validate existing connections
  when a port's color changes. Warn the user if they change the type while a connection exists.
- The `add_combo_menu` widget name (`output_type`) must be serialised in save/load via
  NodeGraphQt's `custom_properties` — verify it round-trips correctly.
