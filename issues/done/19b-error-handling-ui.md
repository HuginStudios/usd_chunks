# Tighten error handling and surface errors in UI

Status: ready-for-agent

Depends on: issue #18 (structural refactor must land first)

## What
Replace bare `except Exception` blocks in node `execute()` methods with:
```python
except Exception as exc:
    self._exec_state = "error"
    self._exec_error_msg = str(exc)
    return {}
```

In the side panel, when a node in error state is selected, show the error message in a
red-tinted label below the node name. Clear it when the node re-executes successfully.

Remove all `[DBG]` print statements (if not already removed in issue #18).

## Where
- `scripts/node_graph/graph_executor.py` — tighter except blocks
- `scripts/node_graph/nodes/_base.py` — `_exec_error_msg` storage on `UsdNodeBase`
- `scripts/node_graph/graph_widget.py` — side panel error label

## Decisions
- Error message in side panel clears on next successful execution of that node.

## Acceptance
- A node that throws in `execute()` shows red state + readable message in side panel.
