# Soft-warn yellow wires for type mismatches

Status: ready-for-agent

Depends on: issue #18 (structural refactor must land first)

## What
In `_typed_validate_accept` (currently in `graph_widget.py`, may be in executor or a
`port_utils.py` helper after #18): allow the connection but return a "warn" flag when
types differ and neither is `'any'`.

In NodeGraphQt's pipe drawing, check for the warn flag and draw the pipe in amber/yellow
(`#d4a017`) instead of the default color. This requires patching the pipe's `paint()` or
setting a custom property on the pipe object that the painter reads.

If the mismatched input causes a Python exception at execution time, the node goes red
(covered by issue 19b) — the yellow wire is the pre-execution hint only.

## Where
- `scripts/node_graph/graph_widget.py` or `scripts/node_graph/port_utils.py`

## Decisions
- Yellow wire is a visual hint only — it does not prevent execution.

## Acceptance
- A type-mismatch wire (e.g. `float` → `prim`) draws in amber, not white/grey.
