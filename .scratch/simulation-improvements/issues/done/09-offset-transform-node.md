Status: needs-triage

# Offset Transform node

## What to build

A new `OffsetTransformNode` in the Transform category that **adds** translate/rotate/scale values to a prim's existing transform, rather than replacing it like `SetTransformNode` does.

Use case: take a prim at its current position and nudge it — e.g. lift it 50 units up, rotate 30 degrees, without losing its original placement.

## Implementation

- Read the prim's current local xform ops
- Add the offset values on top (translate += offset, rotate += offset, scale *= offset)
- Write the combined result back
- Should work with prims that already have transforms from the scene file

## Acceptance criteria

- [ ] `OffsetTransformNode` exists in `_transform.py` with `CATEGORY = "Transform"`
- [ ] Has inputs: `prim`, `tx/ty/tz` (default 0), `rx/ry/rz` (default 0), `sx/sy/sz` (default 1)
- [ ] Adds to existing transform rather than replacing
- [ ] Scale multiplies (1 = no change), translate/rotate add
- [ ] Outputs the same prim
- [ ] Works correctly when chained (Offset → Offset → RBD Solver)
- [ ] Registered in `__init__.py` and `ALL_NODES`

## Blocked by

None — can start immediately.
