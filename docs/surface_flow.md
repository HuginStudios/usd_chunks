# Liquid on surfaces: condensation, runs, drips

Water on glass is made from general tools rather than one "condensation" chunk. Each step writes
plain USD, and the next one reads it.

```
Noise Field ──► (Density Field) ──► Scatter Points "RunSeeds" ──► Surface Flow ──► Trails ──► Curves To Mesh
     │                                                                 │              │
     │                                                                 └► Scatter Points "RunHeads" (Painted Points)
     └────────► (Density Field) ──► Scatter Points "Drops"  ◄── Keep Away From ─────┘
```

| Chunk | Writes | Houdini equivalent |
|---|---|---|
| **Noise Field** | a 0..1 vertex primvar on meshes. *Only GeomSubsets* limits it to, say, a bottle's glass and keeps it off the label | Attribute Noise |
| **Scatter Points** | instances on a surface. The Density Field makes them cluster. *Spacing Follows Scale* packs small instances between big ones without overlap. *Scale Bias* gives many small and few big. *Keep Away From* keeps them clear of other geometry, such as the runs | Scatter + Copy to Points, with a relax on pscale |
| **Surface Flow** | a Points prim over time: each source point crawls over a surface under a force such as gravity, with wander, stop-and-go stickiness and a travel limit | a POP network with the particles stuck to the surface |
| **Trails** | one curve per point, through everywhere it has been. *Width Variation* swells and thins it along its length, and *Sink* presses it into the surface so a tube reads as a film of water. *Grow* animates the curves drawing themselves | Trail SOP |
| **Curves To Mesh** | a tube along each curve, so the runs render in any delegate | Sweep |

Why not the RBD Solver for the runs: a rigid body on a wall falls off it, and nothing in a
rigid-body solver keeps a drop against glass. Surface Flow's points live *on* the surface,
which is the general version of the idea. The same chunk also covers rain on a window, sap on
bark and paint runs.

## Variations

- **New mesh.** Point the model's Reference at it and set the GeomSubsets that should get wet.
  Every surface path is a parameter, so nothing else changes.
- **New look.** Change the seeds: Noise Field for where the clusters are, RunSeeds for where
  runs start, Surface Flow for their paths, Drops for the drops.

## Worked example

`C:\UsdScenes\chunk_demos\condensation\condensation_can.usda` is a close-up packshot of a soda
can, in centimetres. It is built by `build_condensation_can.py` in the same folder, which also converts the can
from OBJ and writes the droplet prototypes and a backdrop as assets. The scene itself is chunks
only. The water is a MaterialX standard_surface with transmission. Storm cannot refract, so the
material keeps a little base colour to stay visible in the viewport.
