Status: needs-triage

# Standardize graph_widget.py

## What to build

Apply the standardization pass to `graph_widget.py` (~2000 lines):

1. **Style constants** — extract the three inline style strings that appear in multiple places (`MENU_STYLE`, `PANEL_STYLE`, and the `_BTN` / `_RUN` toolbar strings) to module-level constants if not already there.
2. **Type hints** — add `from __future__ import annotations`; annotate all public methods (`__init__`, `_on_node_selected`, `_on_ports_connected`, `_on_ports_disconnected`, `_save_graph`, `_load_graph`, `_set_exec_state`, etc.).
3. **Deferred imports** — any `from NodeGraphQt...` or `from pxr import ...` inside method bodies that have no circular-import reason to be lazy should move to the module top.
4. **Dead code** — remove any commented-out blocks.

Do **not** extract `_on_node_selected` into a factory (that is tracked separately in issue #06).
Do **not** refactor wiring logic (deferred per ADR-0001).

## Acceptance criteria

- [ ] `from __future__ import annotations` present at file top
- [ ] All public methods have return-type annotations
- [ ] `MENU_STYLE` and `PANEL_STYLE` are module-level constants (not re-defined inside methods)
- [ ] No commented-out code blocks remain
- [ ] All existing tests still pass

## Blocked by

None — can start immediately.
