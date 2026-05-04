# Event node per-type rate limiting

## What
Add a `_min_interval_ms` class attribute to `_TriggerNodeBase` (default `0` = no limit).
Override in `OnFrameChangedNode` to `33` (≈30 fps cap).

In `_TriggerNodeBase._trigger_downstream()`, before executing, check:
```python
if self._min_interval_ms > 0:
    now = QtCore.QDateTime.currentMSecsSinceEpoch()
    last = getattr(self, '_last_trigger_ms', 0)
    if now - last < self._min_interval_ms:
        return
    self._last_trigger_ms = now
```

## Where
- `scripts/node_graph/nodes.py` — `_TriggerNodeBase` (around line 3285) and `OnFrameChangedNode` (around line 3392)

## Decisions
- `OnFrameChanged`: cap at 33ms (≈30 fps). Consider reading FPS from stage if needed later.
- `OnTimer`: already interval-based, no additional rate limit.
- `OnSelectionChanged`, `OnStageChanged`, others: discrete events, `_min_interval_ms = 0`.
