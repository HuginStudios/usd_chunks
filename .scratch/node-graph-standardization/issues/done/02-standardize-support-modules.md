Status: needs-triage

# Standardize support modules

## What to build

Apply the same standardization pass — `from __future__ import annotations`, type hints on all public functions, docstrings on any public function that lacks one, removal of commented-out debug code — to the five support modules:

- `_neon_theme.py` — `install_neon_theme(graph_widget) -> None`; inner paint helpers typed with `painter: QPainter`, `option`, `widget`
- `_graph_export.py` — type all public functions (e.g. `export_python_script`, `export_group_as_chunk`)
- `_graph_ui.py` — type all widget/dialog classes and their public methods
- `_graph_layout.py` — type `layout_nodes` and any helpers
- `graph_host.py` — type all `GraphHost` / `UsdviewGraphHost` methods

Do not alter logic.

## Acceptance criteria

- [ ] All five files have `from __future__ import annotations`
- [ ] Every public function and method has a return-type annotation
- [ ] No commented-out `print` or debug lines remain
- [ ] All existing tests still pass
- [ ] Progress is printed per file in this format:

```
── Standardizing support modules ─────────────────
  [1/5] _neon_theme.py      ✓
  [2/5] _graph_export.py    ✓
  [3/5] _graph_ui.py        ✓  · 2 inner classes left untyped (Qt subclasses)
  [4/5] _graph_layout.py    ✓
  [5/5] graph_host.py       ✓
── Tests ─────────────────────────────────────────
  37 passed
```

Flag any non-obvious decision with `·` and a brief reason. One line per file maximum.

## Blocked by

None — can start immediately.
