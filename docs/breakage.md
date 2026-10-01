# Breakage: fracture, emit, simulate

Rigid bodies that break into their pieces under load, built the way Houdini builds an RBD
setup: each step is a chunk that writes plain USD, the next one reads it, and the solver
only solves. Nothing a step does is hidden in the solver.

```
Machine (Reference)  ─┐
MantleDrive (Physics Drive)
Fracture ──► Emit ──► RBD Solver ──► lights, camera, render
     output_path ─► target ─► prim_path          (param wires)
```

## The chain

| Chunk | Writes | Houdini equivalent |
|---|---|---|
| **Fracture** | a PointInstancer whose prototypes are every piece of every source, with the break tree as `fracture:` attributes; UVs, inside/outside `GeomSubset`s, optional bevel | Voronoi / Boolean Fracture, packed |
| **Emit** | on that instancer: which piece each instance is, `positions`, `orientations`, `velocities`, `angularVelocities`, `primvars:rbd:emitFrame`, and `invisibleIds` over time as a preview | RBD Configure's emit, Copy to Points, Attribute Randomize on `v` / `w` |
| **Physics Drive** | on any part: `CollisionAPI`; a spinning part as a kinematic body on a `RevoluteJoint` with an angular velocity drive; a moving surface as `velocities` on its points | a static / animated collider, point `v` on a static object |
| **RBD Solver** (Breakable on) | the bake: the instancer rewritten with one instance per body, per frame; spins baked onto driven parts | RBD Bullet Solver |

Every one of these is ordinary USD. Open the instancer after Emit and the stream is
there, frame by frame; open a belt and its velocities are there. A downstream chunk wins
over an upstream one on the same prim, so Emit's instances replace Fracture's laid-out
ones, and the bake replaces Emit's.

## What the solver reads

- **Bodies**: the instances of its Target Prim, whose prototypes carry a fracture tree.
  Each starts at its `positions` / `orientations`, moving at its `velocities` /
  `angularVelocities` (the Initial State params stand in where those are missing), at
  its `primvars:rbd:emitFrame` (the first frame where missing). An instance still blocked
  when its frame comes waits a frame.
- **The world**: every mesh under Collider Prims. What each one is taken as is listed in
  the solver's readout:
    - *spinning* -- a `RevoluteJoint` with an angular `DriveAPI` `targetVelocity` moves it,
      with unlimited torque; the spin is baked onto the prim;
    - *animated* -- its transform is time sampled; it moves with that animation's real
      velocity, so it drags and grips rather than teleporting;
    - *moving surface* -- its points carry `velocities` (or PhysX's surface velocity) while
      staying put; it carries what rests on it as far as friction allows;
    - *static* -- everything else.

    A part leaves the collision with UsdPhysics' `physics:collisionEnabled = false` --
    the emitter box, typically.

## Breaking

A body breaks when the largest contact force on it within a step exceeds its strength,
given in multiples of its own weight and scaling with cross-section (a piece half the
size needs a quarter of the force). Its two or more children appear where it was, moving
as it moved. Material is conserved: the pieces of a break tile their parent exactly.

## Not recooking

- **Fracture** uses the shared cook cache (see
  [Caching a slow cook](writing-a-chunk.md#caching-a-slow-cook)). The key is its params
  plus the source geometry. A fracture made before from the same input is read back
  instead of cut again, also after the scene is closed and opened.
- **RBD Solver** stores each breakable bake in the same cook cache, keyed on everything
  that goes into it. The same input keeps its bake or reads it back. Opening a scene checks
  the cache once, so it comes back simulated. A new chunk downstream changes nothing
  upstream. Untick **Cache Result** to always simulate.

## Worked examples

- `cone_crusher_v2.usda` -- a gyrating mantle in a conical bowl (the mantle is modelled a
  degree and a half off its spin axis, so spinning makes it gyrate), rock fed down a chute,
  gravel into a tray. The machine and the room are plain models brought in by Reference.
