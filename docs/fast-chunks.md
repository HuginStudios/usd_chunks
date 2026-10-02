# Making a chunk fast

> In short: a slow cook is almost always one of two things. Either it authors through the Usd
> API one call at a time, or it loops over data in Python. Write specs with
> [`sdf_authoring`](#write-specs-not-usd-api-calls) inside one `Sdf.ChangeBlock`, and do the
> maths with [numpy](#do-the-maths-in-numpy). Threads don't help; see [What doesn't
> help](#what-doesnt-help).

This page is for writing or speeding up a chunk's `execute()`. The numbers come from
benchmarks on the usdbundle build (USD 26.05, Python 3.11) in October 2026.

## Find where the time goes

Measure before you change anything. Wrap the cook in `cProfile` or time its parts with
`time.perf_counter()`. Cooks usually spend their time in one of these:

| Symptom | Cause | Fix |
| --- | --- | --- |
| Time scales with prims or attributes written; profile full of `DefinePrim`, `CreateAttribute`, `Set` | One change notice per authoring call | [Write specs](#write-specs-not-usd-api-calls) |
| Many `attr.Set(value, time)` calls, one per frame | One notice per sample | [`sim_bake.write_samples`](#time-samples) |
| Time scales with points or faces; profile full of `Gf.Vec3f`, `Matrix4d.Transform`, list comprehensions | Python loops over data | [numpy](#do-the-maths-in-numpy) |
| Nested loops comparing every element with every other | O(n²) search | [A k-d tree](#nearest-neighbours) |

## Write specs, not Usd API calls

Every Usd API call that authors something sends a change notice: `stage.DefinePrim`,
`prim.CreateAttribute`, `attr.Set`, `UsdGeom.Mesh.Define`, `AddTransformOp`. For each notice
the stage recomposes, and everything listening runs: the framework, usdview's prim tree and
the viewport. A handful of calls is fine. Thousands are the whole cook.

On 8,000 prims with three attributes each, cooked through the framework:

| | Usd API | Sdf, one `ChangeBlock` |
| --- | --- | --- |
| Cook | 3.9 s | 0.2 s |
| Next frame, Storm | 0.22 s | 0.05 s |
| Next frame, V-Ray | 1.6 s | 0.07 s |

Converting CAD Import made it 9.6× faster on 3,000 placements.

`usd_chunks.sdf_authoring` writes specs directly into the layer the stage's edit target
points at, at the path the edit target maps to. They land where the Usd API would have put
them, including inside a variant:

```python
from pxr import Gf, Sdf, UsdGeom
from usd_chunks import sdf_authoring


def execute(node):
    stage = node.get_stage()
    writer = sdf_authoring.Writer(stage)
    with Sdf.ChangeBlock():
        for i, (x, y, z) in enumerate(positions):
            spec = writer.define(f"/World/Rocks/R{i}", "Cube")
            sdf_authoring.attribute(spec, "size", Sdf.ValueTypeNames.Double, 0.5)
            sdf_authoring.transform(spec, Gf.Matrix4d().SetTranslate(Gf.Vec3d(x, y, z)))
            sdf_authoring.primvar(spec, "displayColor", Sdf.ValueTypeNames.Color3fArray,
                                  [Gf.Vec3f(1, 0, 0)], UsdGeom.Tokens.constant)
```

| Usd API | `sdf_authoring` |
| --- | --- |
| `stage.DefinePrim(path, type)`, `UsdGeom.X.Define` | `writer.define(path, type)` |
| A property on a prim this layer doesn't define yet | `writer.over(path)` |
| `prim.GetReferences().AddInternalReference(path)` | `writer.reference(spec, path)` |
| `CreateXAttr(value)` on a schema | `attribute(spec, name, type, value)` |
| `prim.CreateAttribute(name, type)` | `attribute(spec, name, type, custom=True)` |
| `PrimvarsAPI.CreatePrimvar(name, type, interp).Set(v)` | `primvar(spec, name, type, v, interp)` |
| `AddTransformOp().Set(m)` on a fresh prim | `transform(spec, m)` |
| `prim.SetInstanceable(True)` | `spec.instanceable = True` |

### Rules

- **Don't read back what you wrote inside the block.** Composition catches up when the block
  closes. Inside it, a prim you defined isn't on the stage yet. Reading anything else is fine,
  including upstream prims and the source of a copy. If the loop needs composed state (a
  world transform, the prim it just made), gather it in a pass before the block.
- **Make the `Writer` inside the `author_in_variant_contexts` loop.** It reads the edit target
  once, when it's created.
- **Get variability right.** Attributes a schema declares `uniform` need `uniform=True`:
  `subdivisionScheme`, `xformOpOrder`, `orientation`, and a GeomSubset's `elementType` and
  `familyName`. The Usd API gets this from the schema. Specs don't.
- **Match `custom`.** Schema attributes and primvars aren't custom (the default).
  `Prim.CreateAttribute` makes custom ones, so pass `custom=True` when replacing it.
- **Compare outputs when you convert.** Run the old and new code on the same input and compare
  `layer.ExportToString()`. CAD Import, Duplicate Prim and Semantic Labels were converted this
  way with byte-identical results. `tests/test_sdf_authoring.py` shows the pattern.

Leave the Usd API alone where it's a fixed handful of calls per cook (a root prim, a scope, one
material). It's clearer, and there's nothing to gain.

### Time samples

To write many frames, use `sim_bake.write_samples(attr, ((time, value), ...), default=...)`.
It writes every sample into the edit target's layer in one change block. One `attr.Set(v, t)`
per frame cost a breakage preview 3 s for 7,000 notices.

## Do the maths in numpy

numpy runs a whole array in C, while a Python loop pays interpreter overhead per element. On
point and face data the difference is usually 10–100×.

**Move data between USD and numpy with `FromNumpy`, not lists:**

```python
points = np.asarray(mesh.GetPointsAttr().Get())              # Vt -> (N, 3) array
attr.Set(Vt.Vec3fArray.FromNumpy(points.astype(np.float32)))  # array -> Vt, no Python loop
```

`Vt.Vec3fArray([Gf.Vec3f(*p) for p in points])` builds one Python object per point. Avoid it.

**Transform points with one matrix product.** `Gf.Matrix4d` uses row vectors: `p' = p · M`,
with the translation in the last row. So:

```python
m = np.asarray(xform_cache.GetLocalToWorldTransform(prim))
world = points @ m[:3, :3] + m[3, :3]   # same as m.Transform(p) for every point
```

`usd_chunks.mesh_math.world_points(prim, points)` does exactly this. Its sibling
`fan_triangulate(counts, indices)` returns `(triangles, corners, face_of_triangle)` for any
polygon mesh. Use both instead of writing the loop again.

**Watch these details:**

- **dtypes.** USD point arrays are `float32`. Do the maths in `float64` if precision matters,
  and convert once when writing (`astype(np.float32)`), not per element.
- **Quaternions.** `Vt.QuathArray.FromNumpy` / `QuatfArray.FromNumpy` take `[x, y, z, w]`,
  imaginary first. MuJoCo and most maths code use `[w, x, y, z]`. Roll the last axis
  (`np.roll(q, -1, axis=-1)`) before writing.
- **Contiguous arrays.** `FromNumpy` needs a C-contiguous array, so call
  `np.ascontiguousarray` after slicing or transposing.

### Seeded randomness

Users rely on a seed giving the same result every time, so a speed-up must never change what a
seed produces.

- **Don't batch or reorder random draws in existing code.** If a loop calls `rng.random()` per
  attempt, vectorizing the maths around it is fine. Pre-drawing the numbers into an array
  changes the result unless the draws stay in exactly the same order, including draws that
  only happen on some attempts.
- **New code: use `np.random.default_rng(seed)` and draw whole arrays** (`rng.random((n, 3))`).
  It's fast and reproducible from the start. Never use the global `np.random` or `random`
  module: anything else drawing from it changes your result.

### Nearest neighbours

Checking every point against every other is O(n²), which takes seconds at a few thousand
points. Use `scipy.spatial.cKDTree`, and keep scipy optional the way Scatter Points does:

```python
try:
    from scipy.spatial import cKDTree
except ImportError:
    cKDTree = None  # fall back to the slower path
```

For a few hundred points, numpy broadcasting is enough:
`d2 = ((a[:, None] - b[None]) ** 2).sum(-1)`.

Some loops are sequential by nature, like dart throwing where each sample depends on the last
or a simulation step. Vectorize across the independent elements *inside* each step instead,
for example every particle in one step.

## What doesn't help

These were measured and rejected, so you don't have to try them again:

- **Threads.** Two independent cooks in two threads ran at 0.83× (Usd API) and 0.96× (Sdf),
  slower than one after the other. USD's Python bindings hold the GIL. numpy is already
  multithreaded inside, so two numpy threads ran at 0.33×. Only waiting (subprocesses, I/O,
  renders) overlaps in threads. For wide independent work like wedges and sweeps, use
  processes.
- **Authoring into a detached layer and swapping it in.** 1.1–1.2×. The cost is the per-call
  notice, not which stage hears it.
- **Usd API calls inside a `Sdf.ChangeBlock`.** Not safe: a prim defined inside the block
  isn't composed yet, so the next Usd call on it fails or misbehaves. Use specs.

## Checklist

1. Profile first: authoring or maths?
2. Authoring that grows with the data → `sdf_authoring` in one `ChangeBlock`, time samples
   through `write_samples`.
3. Python loops over points, faces or instances → numpy, plus `FromNumpy`, plus `mesh_math`.
4. Pairwise searches → `cKDTree` (optional import) or broadcasting.
5. Seeded output unchanged? Compare before and after with the same seed.
6. Output unchanged? Compare `layer.ExportToString()` before and after.
