"""Surface Flow chunk -- points that crawl over a surface under a force, frame by frame.

Houdini's answer to "water running down glass" is a POP network with the particles stuck to
the surface; this is that, as one chunk. Every point of a Source (a Points cloud, a Scatter's
PointInstancer, a mesh's points) is dropped onto the nearest spot of the Surface and then
pushed by the Force -- gravity by default -- along the surface: the part of the force that
lies in the surface's tangent plane moves it, the part pressing into or pulling off the
surface does not. So a point runs straight down a wall, slides slowly round a shoulder, and
stops on a flat top or a flat underside, exactly where water would hang.

Three things make it read as liquid rather than as a ball bearing:

* **Wander** -- a smooth side-to-side drift, so a run is never a ruler line;
* **Stickiness** -- runs stop and go (a drop waits until enough water has gathered behind it);
* **Travel** -- each point runs only so far, varying per point.

The result is a ``Points`` prim with ``points`` time sampled over the frames (the default
value is the final state, for a still) and ``primvars:flow:distance`` -- how far each point
ran. It is plain point data: the Trails chunk turns the paths into curves, Scatter Points
can put instances where the points ended (its Painted Points input), and anything else that
reads points can use it.

Why not the RBD Solver: a rigid body on a wall falls off it. Nothing in a rigid-body solver
keeps a drop against the glass, and faking adhesion there would be the special case this
chunk avoids. A solver whose points live *on* a surface is the general tool -- rain on a
window, sap on bark, paint runs, a bead rolling in a bowl all come out of the same three
numbers.

Requires: pxr (OpenUSD), usd_chunks, numpy.
"""

from __future__ import annotations

import math

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_stage, sim_bake, chunk_utils

#: Points whose tangential push is below this fraction of the force sit still: a flat top,
#: or a flat underside the drop hangs from.
_MIN_SLOPE = 0.08


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


# ---------------------------------------------------------------------------
# the surface
# ---------------------------------------------------------------------------


def _fan(counts, indices):
    """Triangles of a polygon mesh, as (T, 3) vertex indices, plus each triangle's face."""
    counts = np.asarray(counts, dtype=np.int64)
    indices = np.asarray(indices, dtype=np.int64)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    tris, faces = [], []
    for k in range(1, int(counts.max()) - 1 if len(counts) else 0):
        has = counts > k + 1
        s = starts[has]
        tris.append(np.stack([indices[s], indices[s + k], indices[s + k + 1]], axis=1))
        faces.append(np.nonzero(has)[0])
    if not tris:
        return np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
    return np.concatenate(tris), np.concatenate(faces)


def collect_surface(stage, paths, time):
    """World-space triangles of every Mesh under *paths*: (v0, v1, v2, outward normal)."""
    parts = []
    cache = UsdGeom.XformCache(time)
    for path in paths:
        root = stage.GetPrimAtPath(path)
        if not root:
            continue
        # A GeomSubset stands for just its faces of the mesh above it -- the glass of a
        # bottle and not its label, say.
        subset = UsdGeom.Subset(root) if root.IsA(UsdGeom.Subset) else None
        for prim in [root.GetParent()] if subset else Usd.PrimRange(root):
            mesh = UsdGeom.Mesh(prim)
            if not mesh or UsdGeom.Imageable(prim).ComputeVisibility(time) == UsdGeom.Tokens.invisible:
                continue
            pts = mesh.GetPointsAttr().Get(time)
            counts = mesh.GetFaceVertexCountsAttr().Get(time)
            idx = mesh.GetFaceVertexIndicesAttr().Get(time)
            if not pts or not counts or not idx:
                continue
            m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
            p = np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3]
            tri, face = _fan(counts, idx)
            if subset:
                tri = tri[np.isin(face, np.asarray(subset.GetIndicesAttr().Get(time) or [], dtype=np.int64))]
            if not len(tri):
                continue
            if mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded:
                tri = tri[:, ::-1]
            if np.linalg.det(m[:3, :3]) < 0:
                tri = tri[:, ::-1]
            parts.append((p[tri[:, 0]], p[tri[:, 1]], p[tri[:, 2]]))
    if not parts:
        return None
    v0, v1, v2 = (np.concatenate([q[i] for q in parts]) for i in range(3))
    n = np.cross(v1 - v0, v2 - v0)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)
    return Surface(v0, v1, v2, n)


class Surface:
    """Triangles bucketed on a grid, for closest-point queries near a moving point."""

    def __init__(self, v0, v1, v2, n):
        self.v0, self.v1, self.v2, self.n = v0, v1, v2, n
        lo = np.minimum(np.minimum(v0, v1), v2)
        hi = np.maximum(np.maximum(v0, v1), v2)
        edge = float(np.median(np.linalg.norm(hi - lo, axis=1))) if len(v0) else 1.0
        self.cell = max(edge, 1e-9)
        self.origin = lo.min(axis=0)
        self.buckets: dict = {}
        a = np.floor((lo - self.origin) / self.cell).astype(np.int64)
        b = np.floor((hi - self.origin) / self.cell).astype(np.int64)
        for t in range(len(v0)):
            for i in range(a[t, 0], b[t, 0] + 1):
                for j in range(a[t, 1], b[t, 1] + 1):
                    for k in range(a[t, 2], b[t, 2] + 1):
                        self.buckets.setdefault((i, j, k), []).append(t)
        self.buckets = {key: np.asarray(v, dtype=np.int64) for key, v in self.buckets.items()}
        self._near: dict = {}

    def diagonal(self) -> float:
        pts = np.concatenate([self.v0, self.v1, self.v2])
        return float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))

    def _candidates(self, point, reach):
        c = tuple(np.floor((point - self.origin) / self.cell).astype(np.int64))
        key = (c, reach)
        hit = self._near.get(key)
        if hit is None:
            parts = [self.buckets[(c[0] + i, c[1] + j, c[2] + k)]
                     for i in range(-reach, reach + 1) for j in range(-reach, reach + 1)
                     for k in range(-reach, reach + 1) if (c[0] + i, c[1] + j, c[2] + k) in self.buckets]
            hit = np.unique(np.concatenate(parts)) if parts else np.zeros(0, dtype=np.int64)
            self._near[key] = hit
        return hit

    def closest(self, point, reach=1):
        """(closest point, outward normal, distance), or None when nothing is within reach."""
        cand = self._candidates(point, reach)
        if not len(cand):
            return None
        q, d2 = _closest_on_triangles(point, self.v0[cand], self.v1[cand], self.v2[cand])
        i = int(np.argmin(d2))
        return q[i], self.n[cand[i]], math.sqrt(float(d2[i]))

    def closest_anywhere(self, point):
        """Like closest, but searched over every triangle -- for dropping a far point on."""
        q, d2 = _closest_on_triangles(point, self.v0, self.v1, self.v2)
        i = int(np.argmin(d2))
        return q[i], self.n[i], math.sqrt(float(d2[i]))


def _closest_on_triangles(p, a, b, c):
    """Closest point on each triangle to *p* (Ericson, Real-Time Collision Detection 5.1.5)."""
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    denom = np.where(np.abs(va + vb + vc) > 1e-30, va + vb + vc, 1e-30)
    v = vb / denom
    w = vc / denom
    out = a + ab * v[:, None] + ac * w[:, None]
    # Edge and vertex regions, most specific last so they override the face case.
    def sel(mask, value):
        out[mask] = value[mask]
    t_bc = np.where(np.abs((d4 - d3) + (d5 - d6)) > 1e-30, (d4 - d3) / ((d4 - d3) + (d5 - d6) + 1e-300), 0.0)
    sel((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0), b + (c - b) * t_bc[:, None])
    t_ac = np.where(np.abs(d2 - d6) > 1e-30, d2 / (d2 - d6 + 1e-300), 0.0)
    sel((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + ac * t_ac[:, None])
    t_ab = np.where(np.abs(d1 - d3) > 1e-30, d1 / (d1 - d3 + 1e-300), 0.0)
    sel((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + ab * t_ab[:, None])
    sel((d6 >= 0) & (d5 <= d6), c)
    sel((d3 >= 0) & (d4 <= d3), b)
    sel((d1 <= 0) & (d2 <= 0), a)
    diff = out - p
    return out, np.einsum("ij,ij->i", diff, diff)


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------


def _smooth_noise(rng, n, length):
    """*n* smooth 1D noise tracks in -1..1, sampled at integer steps, one per point."""
    knots = rng.uniform(-1.0, 1.0, size=(n, length // 8 + 3))
    t = np.arange(length) / 8.0
    i = t.astype(np.int64)
    f = t - i
    f = f * f * (3.0 - 2.0 * f)
    return knots[:, i] * (1.0 - f) + knots[:, i + 1] * f


def simulate(surface, starts, *, force, frames, fps, speed, wander, stickiness, travel,
             travel_variation, release, seed, substeps=4):
    """Paths of *starts* crawling over *surface*.

    Returns (positions (frames+1, N, 3), normals (frames+1, N, 3), distance (N,)): frame 0
    is where each point landed on the surface, before anything moved.
    """
    rng = np.random.default_rng(seed)
    n = len(starts)
    force = np.asarray(force, dtype=np.float64)
    fmag = float(np.linalg.norm(force))
    fdir = force / fmag if fmag > 1e-12 else np.zeros(3)
    pos = np.zeros((n, 3))
    nrm = np.zeros((n, 3))
    on = np.ones(n, dtype=bool)
    for i, p in enumerate(np.asarray(starts, dtype=np.float64)):
        hit = surface.closest(p, 2) or surface.closest_anywhere(p)
        pos[i], nrm[i] = hit[0], hit[1]
    limit = travel * (1.0 - travel_variation * rng.random(n))
    begin = rng.random(n) * release * fps
    steps = frames * substeps
    side = _smooth_noise(rng, n, steps + 2)
    go = _smooth_noise(rng, n, steps // 4 + 4)
    dist = np.zeros(n)
    out_p = np.zeros((frames + 1, n, 3))
    out_n = np.zeros((frames + 1, n, 3))
    out_p[0], out_n[0] = pos, nrm
    dt = 1.0 / (fps * substeps)
    reach = max(1, int(math.ceil(speed * dt / surface.cell)) + 1)
    for s in range(steps):
        frame = s / substeps
        for i in range(n):
            if not on[i] or frame < begin[i] or dist[i] >= limit[i]:
                continue
            g_t = fdir - nrm[i] * float(fdir @ nrm[i])
            slope = float(np.linalg.norm(g_t))
            if slope < _MIN_SLOPE:
                continue
            # Stop-and-go: below the stickiness threshold the drop waits.
            gate = (go[i, s // 16] + 1.0) * 0.5
            if gate < stickiness:
                continue
            down = g_t / slope
            across = np.cross(nrm[i], down)
            step_dir = down + across * (wander * side[i, s])
            step_dir /= max(float(np.linalg.norm(step_dir)), 1e-12)
            step = speed * slope * dt
            hit = surface.closest(pos[i] + step_dir * step, reach)
            if hit is None or hit[2] > max(step, surface.cell * 0.05) * 2.0:
                on[i] = False  # ran off an edge: it stays where it was, hanging there
                continue
            moved = float(np.linalg.norm(hit[0] - pos[i]))
            if moved < step * 0.05:
                on[i] = False  # wedged in a crease
                continue
            pos[i], nrm[i] = hit[0], hit[1]
            dist[i] += moved
        if (s + 1) % substeps == 0:
            f = (s + 1) // substeps
            out_p[f], out_n[f] = pos, nrm
    return out_p, out_n, dist


def read_points(stage, path, time):
    """World-space points of a Points / PointInstancer / any PointBased prim."""
    prim = stage.GetPrimAtPath(path) if path else None
    if not prim:
        return None
    if prim.IsA(UsdGeom.PointInstancer):
        pts = UsdGeom.PointInstancer(prim).GetPositionsAttr().Get(time)
    elif prim.IsA(UsdGeom.PointBased):
        pts = UsdGeom.PointBased(prim).GetPointsAttr().Get(time)
    else:
        return None
    if not pts:
        return np.zeros((0, 3))
    m = np.array(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(time), dtype=np.float64)
    return np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3]


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    source = node.get_param("source", is_s).value.strip()
    surface_paths = [p.strip() for p in node.get_param("surface", is_s).value.split(",") if p.strip()]
    out_path = node.get_param("output_path", is_s).value.strip() or "/SurfaceFlow"
    if not source or not surface_paths:
        _report(node, "set a Source and a Surface")
        return
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    start_frame = node.get_param("start_frame", is_i).value
    starts = read_points(upstream, source, start_frame)
    if starts is None:
        _report(node, f"{source} has no points")
        return
    surface = collect_surface(upstream, surface_paths, start_frame)
    if surface is None:
        _report(node, "the Surface has no meshes")
        return
    fps = stage.GetTimeCodesPerSecond() or 24.0
    frames = max(1, node.get_param("frames", is_i).value)
    pos, nrm, dist = simulate(
        surface, starts,
        force=node.get_param("force").value or (0.0, 0.0, -1.0),
        frames=frames, fps=fps,
        speed=max(0.0, node.get_param("speed", is_f).value),
        wander=max(0.0, node.get_param("wander", is_f).value),
        stickiness=min(0.95, max(0.0, node.get_param("stickiness", is_f).value)),
        travel=max(0.0, node.get_param("travel", is_f).value),
        travel_variation=min(1.0, max(0.0, node.get_param("travel_variation", is_f).value)),
        release=max(0.0, node.get_param("release", is_f).value),
        seed=node.get_param("seed", is_i).value,
    )
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        pts = UsdGeom.Points.Define(stage, out_path)
        pa, na = pts.CreatePointsAttr(), pts.CreateNormalsAttr()
        pa.Set(Vt.Vec3fArray.FromNumpy(pos[-1].astype(np.float32)))
        na.Set(Vt.Vec3fArray.FromNumpy(nrm[-1].astype(np.float32)))
        sim_bake.write_samples(pa, ((start_frame + f, Vt.Vec3fArray.FromNumpy(pos[f].astype(np.float32)))
                                    for f in range(frames + 1)))
        sim_bake.write_samples(na, ((start_frame + f, Vt.Vec3fArray.FromNumpy(nrm[f].astype(np.float32)))
                                    for f in range(frames + 1)))
        width = max(1e-6, node.get_param("width", is_f).value)
        pts.CreateWidthsAttr().Set(Vt.FloatArray([width] * len(dist)))
        pts.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
        pv = UsdGeom.PrimvarsAPI(pts).CreatePrimvar("flow:distance", Sdf.ValueTypeNames.FloatArray,
                                                     UsdGeom.Tokens.vertex)
        pv.Set(Vt.FloatArray.FromNumpy(dist.astype(np.float32)))
    moved = int((dist > 1e-9).sum())
    _report(node, f"{len(dist)} points · {moved} ran, up to {dist.max() if len(dist) else 0:.3g} "
                  f"· frames {start_frame}–{start_frame + frames}")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Surface Flow")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    sel = chunk.ChunkFlags.ParamFlags.PRIM_SELECTION
    chunk_api.add_param(chunk.LineEdit(
        "source", "Source", flags=labeled | sel,
        tooltip="Where the points start: a Points prim, a PointInstancer (a Scatter's output) or any "
        "mesh's points. Each is dropped onto the nearest spot of the Surface first.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "surface", "Surface", flags=labeled,
        tooltip="The mesh (or a parent of meshes) the points crawl over; a GeomSubset limits it to "
        "those faces. Comma-separate several.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "output_path", "Output", default_value="/SurfaceFlow", flags=labeled,
        tooltip="The Points prim written: points over time, normals, widths and flow:distance.",
    ))
    chunk_api.add_param(chunk.Vector("force", "Force", param=chunk.Spinbox(), length=3, suffixes="XYZ",
                                     default_value=(0.0, 0.0, -1.0),
                                     tooltip="Direction the points are pushed, world space -- gravity is "
                                     "straight down. Only its direction matters; Speed sets how fast."))
    chunk_api.add_param(chunk.Spinbox("speed", "Speed", default_value=0.05,
                                      tooltip="How fast a point runs down a vertical wall, scene units per "
                                      "second. Slopes are slower in proportion."))
    chunk_api.add_param(chunk.Slider("wander", "Wander", min_value=0.0, max_value=2.0, default_value=0.35,
                                     tooltip="Side-to-side drift. 0 runs straight down the slope."))
    chunk_api.add_param(chunk.Slider("stickiness", "Stickiness", min_value=0.0, max_value=0.95,
                                     default_value=0.3,
                                     tooltip="How much of the time a point waits instead of moving -- "
                                     "runs stop and go."))
    chunk_api.add_param(chunk.Spinbox("travel", "Travel", default_value=0.08,
                                      tooltip="The furthest any point runs, scene units."))
    chunk_api.add_param(chunk.Slider("travel_variation", "Travel Variation", min_value=0.0, max_value=1.0,
                                     default_value=0.6,
                                     tooltip="Each point runs between (1 - this) and 1 times Travel."))
    chunk_api.add_param(chunk.Spinbox("release", "Release Over", default_value=1.0,
                                      tooltip="Seconds over which the points start moving, at random."))
    chunk_api.add_param(chunk.Spinbox("width", "Width", default_value=0.002,
                                      tooltip="The widths written on the points (Trails reads them)."))
    chunk_api.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True))
    chunk_api.add_param(chunk.Spinbox("frames", "Frames", default_value=96, is_int=True,
                                      tooltip="How many frames to run."))
    chunk_api.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    chunk_api.add_param(chunk.Label("readout", "nothing run yet", default_value="nothing run yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.DYNAMICS,
        icon="waves",
        description=(
            """
        Points crawling over a surface under gravity: water runs, rain on glass, over time.

        Points crawl over a surface under a force (gravity): runs of water, rain on glass.
        Writes a Points prim over time -- Trails turns the paths into curves.
        """
        ),
        requirements=["numpy>=2,<3"],
    )
