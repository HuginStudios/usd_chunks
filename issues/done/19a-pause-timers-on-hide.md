# Pause background timers when graph window is hidden

Status: ready-for-agent

Depends on: issue #18 (structural refactor must land first)

## What
In `NodeGraphWidget.hideEvent` stop all background timers:
- Minimap 500ms update timer
- Any per-node `QTimer` for trigger port visibility (collected at node creation into
  `self._init_timers` list on the widget)
- Prim thumbnail capture timer

In `showEvent` restart them. This eliminates background CPU and prevents ghost-execution
when usdview is in the foreground but the graph window is minimised/hidden.

Timer pause applies to the floating graph window only, not to event nodes' internal
subscriptions (those should keep listening so the graph is live when re-shown).

## Where
- `scripts/node_graph/graph_widget.py` — `hideEvent`, `showEvent`, timer collection

## Acceptance
- Hiding the graph window and waiting 5 seconds: no Python activity on the main thread
  (verify with a simple `print` guard in the minimap timer).
