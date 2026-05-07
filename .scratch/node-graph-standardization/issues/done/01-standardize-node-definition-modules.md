Status: needs-triage

# Standardize node definition modules

## What to build

Add `from __future__ import annotations`, type hints on `execute()`, `to_python()`, and `_subscribe()`, and move any unsafe lazy imports (e.g. `fnmatch`, usdview internals) out of inner function bodies and into the module top-level — across all 15 hand-written node definition files:

`nodes/_input.py`, `_create.py`, `_event.py`, `_transform.py`, `_query.py`, `_utility.py`, `_math.py`, `_code.py`, `_lighting.py`, `_camera.py`, `_animation.py`, `_material.py`, `_simulation.py`, `_hierarchy.py`, `_composition.py`

The pattern is identical in every file:
- `execute(self, stage) -> dict`
- `to_python(self, var_map: dict) -> str`
- `_subscribe(self, stage) -> Callable | None` (event nodes only)
- Lazy usdview imports (`chunk_registry`, `ctrl._dataModel`) stay lazy — they are guarded for a reason and must not move.

Do not alter logic. This is a pure style / annotation pass.

## Acceptance criteria

- [ ] All 15 files have `from __future__ import annotations` at the top
- [ ] `execute()` is typed `(self, stage) -> dict` on every node class
- [ ] `to_python()` is typed `(self, var_map: dict) -> str` wherever it exists
- [ ] `_subscribe()` on event nodes is typed `(self, stage) -> Callable | None`
- [ ] No previously-lazy usdview import (e.g. `chunk_registry`, `ctrl`) is moved to module top
- [ ] All existing tests still pass (`pytest scripts/node_graph/tests/`)
- [ ] Progress is printed per file in this format:

```
── Standardizing nodes/ ──────────────────────────
  [ 1/15] _input.py         ✓
  [ 2/15] _create.py        ✓  · kept 2 lazy imports (usdview-guarded)
  [ 3/15] _event.py         ✓
  ...
── Tests ─────────────────────────────────────────
  37 passed
```

Flag any non-obvious decision (kept lazy import, skipped a method) with `·` and a brief reason. One line per file maximum.

## Blocked by

None — can start immediately.
