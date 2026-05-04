# Isolate AI suggestions from execution

Status: ready-for-agent

Depends on: issue #18 (structural refactor must land first)

## What
Ensure `ai_suggest.py` callbacks never:
- Write to `_output_cache` or `_node_layer_registry`
- Call `executor.run_from()` or `executor.mark_dirty()`
- Access `node.execute()`

Add a guard at the top of each callback:
```python
if not self._executor:   # graph was closed while suggestion was in flight
    return
```

All suggestion results must only update UI widgets (ghost buttons, Tab menu list).

## Where
- `scripts/node_graph/ai_suggest.py` — execution isolation guards

## Acceptance
- No AI suggestion callback touches `_output_cache` (grep confirms).
