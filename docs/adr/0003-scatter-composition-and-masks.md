# ADR-0003: Scatter is placement-only; regions go through a mask hook

**Status:** Accepted  
**Date:** 2026-06-02

## Context

The Scatter Points chunk had three modes: Surface, Volume, and Stack. Stack ran a
PyBullet simulation to drop instances into a container (e.g. apples into a bowl)
and let them settle. It was fragile (scale calibration, mid-air collisions, CoM
offsets) and pulled in a `pybullet` dependency. Volume was slow (point-in-mesh
ray-cast tested every triangle per candidate). Only Surface worked well.

Separately, the RBD Solver (MuJoCo) now settles a PointInstancer in a concave
collider robustly, and a future paint gizmo will let users mark where on a
surface to scatter.

## Decision

1. **Scatter does placement only; settling is the RBD Solver's job.** Removed
   Stack mode and the `pybullet` dependency. "Apples in a bowl" = scatter loosely
   on/above the bowl, then run the RBD Solver. This is the project's compose-small-
   chunks philosophy, and it makes scatter both simpler and more capable.
   Non-overlap splits cleanly: Poisson-disk **spacing** at scatter time (no initial
   overlap, auto-derived from the prototype footprint), the **solver** for real
   collision/settling.

2. **Region filtering goes through one per-point mask hook**
   `mask(point, normal) -> 0..1`. The sampler keeps a candidate with that
   probability. Slope is the only built-in source today (applied as a per-triangle
   pre-filter for efficiency); the paint gizmo will later supply a per-point weight
   through the same hook, touching neither the distribution nor variation code.

3. Kept Surface + Volume; sped up Volume by bucketing triangles into a (y,z) grid
   so each ray test only checks faces it can hit.

## When to revisit

If a use case genuinely needs in-chunk settling (not worth a solver round-trip),
or if the per-point mask hook proves too slow once paint masks sample a texture
per candidate (then pre-bake the mask into a grid like the Volume optimization).
