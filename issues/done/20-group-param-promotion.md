# Group node param promotion

Status: ready-for-agent

## What
Right-click a param label in the side panel (when an inner node inside a group is selected)
→ "Promote to Group". Creates a named input port on the enclosing GroupNode and a matching
editable field in the group's side panel. Default value comes from the inner param's current value.

Similarly, right-click an inner node's `prim` or `layer` output port → "Promote Output to Group"
to expose it as a GroupNode output port.

## Where
- `scripts/node_graph/graph_widget.py` — side panel right-click menu, promotion logic
- `scripts/node_graph/nodes/_base.py` — `_promotions` dict and `_promoted_outputs` on GroupNode

## Implementation
Store promotions as a dict on GroupNode: `_promotions[param_name] = {inner_node_id, inner_param,
port_color, chunk_param_class, chunk_param_kwargs}`. Infer `chunk_param_class` from the inner
param type (Slider→Slider, Menu→Menu, etc., fallback to LineEdit). Serialise as JSON string
in a custom property `promoted_params` so it survives save/load.

Promoted param value changes write back to `_promotions[name]["default_value"]`.

## Decisions
- Duplicate promote silently overwrites.
- No un-promote UI yet.
- Output promotion limited to `prim` and `layer` typed ports.

## Acceptance
- Promote a Slider param from an inner node → GroupNode gains an input port and side panel slider.
- Promote a prim output → GroupNode gains a prim output port.
- Save/load graph preserves all promotions.
