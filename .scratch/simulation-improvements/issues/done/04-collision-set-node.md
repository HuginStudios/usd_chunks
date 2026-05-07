Status: needs-triage

# CollisionSetNode

## What to build

A new `CollisionSetNode` in the `"Simulation"` category that lets users specify multiple collision prims in one place, removing the need to wire a separate node per collider.

Input: a multi-line text area (one absolute prim path per line, e.g. `/World/Floor\n/World/WallA`). Also hook into the existing drag-and-drop mechanism used by `PrimPathNode` so prims can be dragged from the usdview hierarchy directly onto the node — each dropped prim appends its path to the text area.

Output: a single `prims` port (`PORT_COLORS["prim"]`) that emits a `list[Usd.Prim]` — compatible with the existing `multi_input` collision port on `RBDSolverNode`.

Invalid or non-existent paths are skipped with a per-path warning; valid paths continue to resolve.

## Acceptance criteria

- [ ] Node appears in `"Simulation"` category
- [ ] Multi-line text area accepts one prim path per line
- [ ] Drag-and-drop from the usdview hierarchy appends the prim path to the text area
- [ ] `prims` output emits a `list[Usd.Prim]` containing only valid, resolved prims
- [ ] Invalid paths print a warning and are excluded from the output list
- [ ] Connecting the output to the `collision` port of `RBDSolverNode` works end-to-end

## Blocked by

- `01-simulation-category.md`
