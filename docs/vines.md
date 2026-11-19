# Vines: paint, grow, sweep, shade

Ivy on a wall, ready for any renderer. Most renderers, Aurora among them, don't draw
curves, so the vine is turned into a mesh and gets a material like any other mesh.

```
Scatter Brush ──► Vine ──► Curves to Mesh ──► Preview Surface (bark)
                    │
                    └─ Leaf Points ──► Scatter Points (leaf mesh) ──► Preview Surface (leaf)
```

`examples/scripts/ivy_wall.py` builds this whole network on a stone wall, with lights and
a camera:

```
USD_CHUNK_DIRS=chunks python examples/scripts/ivy_wall.py
```

## The chain

| Chunk | What it does |
|---|---|
| **Scatter Brush** | Paint where the ivy may grow. Wire its Density Field into the Vine's. |
| **Vine** | Grows vines over the paint. Writes a `BasisCurves` (the stems) and a guide `Points` cloud (one point per leaf, with the surface normal). |
| **Curves to Mesh** | Sweeps the stems into a tube mesh with UVs, and hides the curves so nothing draws them twice. |
| **Preview Surface** | Binds a bark material to the tube mesh. |
| **Scatter Points** | Instances a leaf mesh on the Vine's Leaf Points. Wire Leaf Points into its Painted Points, and set Up Along Surface to the leaf's tip axis: each leaf then points away from its stem, sized by the Vine. |

## Settings to reach for

- **Vine › Growth**: Density for how much of the paint gets covered, Meander for how much
  the stems wind, Climb for how strongly they head up.
- **Vine › Shape**: Thickness for stem width, as a share of the painted area.
- **Vine › Leaves**: Spacing, Spread (how far leaves sit beside the stem), Bare Base %
  (no leaves on the oldest part of each stem) and Tip Leaf Size (how small the young
  leaves at a growing tip are).
- **Vine › Hanging**: Hang % sends tendrils off the stem tips. They fall and drape over
  the mesh.
- **Curves to Mesh › Sides**: 6 is plenty at a few metres. Raise it for a close-up.

The roots start at the lowest painted points, but never on a face pointing straight down,
such as the buried underside of a wall.

## Animate it

Turn on **Vine › Animation › Animate Growth** and set Start and End Frame. The vine grows
from nothing to Growth % over those frames. Curves to Mesh sweeps every frame, so the mesh
grows too, and Scatter Points shows each leaf from the frame the vine reaches it.

The curves, the mesh and the leaf points all carry a `growth` primvar: 0 at the roots, 1
at the last tip. A shader can use it to colour young growth, or to grow the vine in at
render time instead.

## Render in Aurora

Export the scene and open it in usdview with Aurora, or render it with `usdrecord`:

```
usdrecord --renderer Aurora --camera /World/cam ivy_wall.usda ivy_wall.png
```

Aurora renders until it has converged, which can take minutes. For a quick look, cap the
samples with a RenderSettings prim (`int aurora:max_samples = 64`) and pass it with
`--renderSettingsPrimPath`.
