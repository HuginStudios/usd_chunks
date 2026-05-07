Status: needs-triage

# Extract PropertyPanelFactory from NodeGraphWidget

## What to build

The 230-line `_on_node_selected` method in `graph_widget.py` detects node and property types and creates Qt widgets inline. This is Candidate 2 from the architecture review: a shallow, untestable god-method that mixes node-type dispatch, property-type dispatch, and widget construction in one place.

Extract the property-widget selection logic into a `PropertyPanelFactory` module at `scripts/node_graph/_property_panel.py` with a single public function:

```python
def build_node_panel(
    node,
    form: QFormLayout,
    on_changed: Callable,
    active_graph_fn: Callable | None = None,
    ref_menu_fn: Callable | None = None,
) -> None:
    ...
```

Internally, dispatch on node type (GroupNode → `build_group_promo_panel`, chunk node → `build_chunk_panel`, else → property rows) and on property type (`"code"` → code editor, `"file_path"` → file browser, bool → checkbox, etc.).

`_on_node_selected` in `graph_widget.py` becomes a ~20-line method that clears the form and calls `build_node_panel(node, self._props_form, ...)`.

Register `_property_panel` in `CONTEXT.md` as **PropertyPanelFactory** once the seam is clean.

## Acceptance criteria

- [ ] `_property_panel.py` exists with `build_node_panel` as its public interface
- [ ] `_on_node_selected` in `graph_widget.py` delegates entirely to `build_node_panel`
- [ ] No widget-creation logic (QCheckBox, _CodeEditor, file browser) remains in `graph_widget.py`'s selection handler
- [ ] A test in `test_panel_builder.py` calls `build_node_panel` with a plain `UsdNodeBase` subclass and asserts rows are created
- [ ] All existing tests still pass
- [ ] `CONTEXT.md` updated with **PropertyPanelFactory** entry

## Blocked by

- `.scratch/node-graph-standardization/issues/03-standardize-graph-widget.md` — file should be tidy before the surgical extraction
