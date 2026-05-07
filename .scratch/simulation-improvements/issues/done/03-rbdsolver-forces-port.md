Status: needs-triage

# RBDSolver forces port and application loop

## What to build

Add a `forces` multi-input port to `RBDSolverNode` that accepts Python dicts emitted by force nodes (Wind, Drag, Turbulence). At each simulation step the solver iterates over all connected force dicts and applies the appropriate impulse to every body.

Force dict contract:
- `{"type": "wind", "x": float, "y": float, "z": float, "strength": float}`
- `{"type": "drag", "coefficient": float}` — reads current body velocity, applies `force = -coeff * velocity`
- `{"type": "turbulence", "strength": float, "frequency": float, "seed": int, "step": int}` — solver passes current step index; node pre-computes noise or solver samples it inline

The solver collects force dicts the same way it collects collision prims: a `_get_all_force_inputs()` helper that mirrors `_get_all_collision_inputs()`.

## Acceptance criteria

- [ ] `forces` multi-input port exists on `RBDSolverNode` with `PORT_COLORS["data"]` colour
- [ ] `_get_all_force_inputs()` returns a list of dicts from all connected force nodes
- [ ] Each simulation step calls `p.applyExternalForce` / `p.applyExternalTorque` for every body × every force dict
- [ ] Unknown `type` values in a force dict are silently skipped with a `print` warning
- [ ] With no forces connected the simulation behaves identically to before

## Blocked by

- `01-simulation-category.md`
