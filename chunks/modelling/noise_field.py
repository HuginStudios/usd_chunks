"""Noise Field chunk -- a 0..1 noise primvar painted onto meshes, procedurally.

Houdini's Attribute Noise. It writes a per-vertex float primvar that anything reading a
field can use. It was built to feed Scatter Points' **Density Field**, which turns an even
scatter into clusters: dense where the noise is high, bare where it is low. The primvar name
is this chunk's own param, so the wire from here to the Scatter is visible and nothing is
implied.

* **Size** -- how big a blob of the noise is, in scene units (world space, so two meshes side
  by side get one continuous field);
* **Threshold** / **Softness** -- where the field cuts off and how gradually;
* **Only GeomSubsets** -- names of GeomSubsets that get the noise; every other vertex gets 0.
  The glass of a bottle, not its label.

Requires: pxr (OpenUSD), usd_chunks, numpy.
"""

from __future__ import annotations

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_stage, chunk_utils


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _hash(ix, iy, iz, seed):
    h = (ix * 73856093) ^ (iy * 19349663) ^ (iz * 83492791) ^ (seed * 2654435761)
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    return (h & 0xFFFFFF).astype(np.float64) / float(0xFFFFFF)


def value_noise(p, seed=0):
    """Smooth value noise in 0..1 at (N, 3) points, one unit per lattice cell."""
    p = np.asarray(p, dtype=np.float64)
    i = np.floor(p).astype(np.int64)
    f = p - i
    f = f * f * (3.0 - 2.0 * f)
    out = np.zeros(len(p))
    for dx in (0, 1):
        wx = f[:, 0] if dx else 1.0 - f[:, 0]
        for dy in (0, 1):
            wy = f[:, 1] if dy else 1.0 - f[:, 1]
            for dz in (0, 1):
                wz = f[:, 2] if dz else 1.0 - f[:, 2]
                out += wx * wy * wz * _hash(i[:, 0] + dx, i[:, 1] + dy, i[:, 2] + dz, seed)
    return out


def fbm(p, seed=0, octaves=3):
    """Fractal sum of value noise, normalised to 0..1."""
    total, amp, norm = np.zeros(len(p)), 1.0, 0.0
    for o in range(octaves):
        total += amp * value_noise(np.asarray(p) * (2.0 ** o), seed + o * 101)
        norm += amp
        amp *= 0.5
    return total / norm


def field(points, *, size, threshold, softness, seed, octaves=3):
    n = fbm(np.asarray(points) / max(size, 1e-9), seed, octaves)
    lo, hi = threshold - softness * 0.5, threshold + softness * 0.5
    t = np.clip((n - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    paths = [p.strip() for p in node.get_param("prim_path", is_s).value.split(",") if p.strip()]
    name = node.get_param("primvar", is_s).value.strip() or "density"
    subsets = {s.strip() for s in node.get_param("subsets", is_s).value.split(",") if s.strip()}
    if not paths:
        _report(node, "set the meshes to paint")
        return
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    settings = dict(size=max(1e-9, node.get_param("size", is_f).value),
                    threshold=node.get_param("threshold", is_f).value,
                    softness=max(0.0, node.get_param("softness", is_f).value),
                    seed=node.get_param("seed", is_i).value)
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    painted, total, lit = 0, 0, 0.0
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        for path in paths:
            root = upstream.GetPrimAtPath(path)
            if not root:
                continue
            for prim in Usd.PrimRange(root):
                mesh = UsdGeom.Mesh(prim)
                if not mesh:
                    continue
                pts = mesh.GetPointsAttr().Get()
                if not pts:
                    continue
                m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
                world = np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3]
                values = field(world, **settings)
                if subsets:
                    keep = np.zeros(len(pts), dtype=bool)
                    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
                    idx = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64)
                    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
                    for sub in UsdGeom.Subset.GetAllGeomSubsets(mesh):
                        if sub.GetPrim().GetName() not in subsets:
                            continue
                        for face in sub.GetIndicesAttr().Get() or []:
                            keep[idx[starts[face]:starts[face] + counts[face]]] = True
                    values = np.where(keep, values, 0.0)
                out = UsdGeom.PrimvarsAPI(stage.OverridePrim(prim.GetPath()))
                pv = out.CreatePrimvar(name, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
                pv.Set(Vt.FloatArray.FromNumpy(values.astype(np.float32)))
                painted += 1
                total += len(values)
                lit += float(values.sum())
    if not painted:
        _report(node, "no meshes found to paint")
        return
    _report(node, f"primvars:{name} on {painted} mesh{'es' if painted != 1 else ''} · "
                  f"{100.0 * lit / max(total, 1):.0f}% covered")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Noise Field")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Meshes", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Meshes (or parents of meshes) to paint. Comma-separate several.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primvar", "Primvar", default_value="density", flags=labeled,
        tooltip="Name of the float primvar written. Wire it into a Scatter Points' Density Field.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "subsets", "Only GeomSubsets", flags=labeled,
        tooltip="Names of GeomSubsets that get the noise, comma separated; everything else is 0. "
        "Empty paints the whole mesh.",
    ))
    chunk_api.add_param(chunk.Spinbox("size", "Size", default_value=0.03,
                                      tooltip="Size of a blob of noise, scene units."))
    chunk_api.add_param(chunk.Slider("threshold", "Threshold", min_value=0.0, max_value=1.0, default_value=0.5,
                                     tooltip="Noise below this goes to 0. Higher = fewer, smaller clusters."))
    chunk_api.add_param(chunk.Slider("softness", "Softness", min_value=0.0, max_value=1.0, default_value=0.2,
                                     tooltip="How gradually the field fades out around the threshold."))
    chunk_api.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    chunk_api.add_param(chunk.Label("readout", "nothing painted yet", default_value="nothing painted yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="waves",
        description="Paints a 0..1 noise primvar on meshes -- a procedural Density Field for Scatter Points.",
        requirements=["numpy>=2,<3"],
    )
