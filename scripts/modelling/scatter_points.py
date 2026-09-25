"""Scatter Points chunk — distributes prototype instances across a mesh.

Three placement modes:

* **Surface** — area-weighted sampling on the faces, Poisson-disk spacing to avoid overlap.
* **Volume** — uniform fill of a closed interior.
* **Stack** — a regular hex/grid lattice packed against the target, every instance resting
  exactly on the surface it lands on and optionally tiered into levels. This is the mode for
  "a cup of crayons standing up, tight but not intersecting": the pitch comes from the
  prototype's own oriented bounding box, so tight *is* collision-free by construction.

Prototypes are a list, each row carrying its own share of the mix (a percentage, normalised
across the rows), a scale multiplier, and optionally a variant selection written straight
into the path — ``/asset/crayon{color=red}``. ``{color=*}`` expands to one prototype per
variant in that set, which is the cheap way to scatter a whole colour range from one row.

Physical settling (apples coming to rest in a bowl) is still the RBD Solver's job — scatter
loosely, then settle. Stack mode is the deterministic alternative for when you want a tidy,
packed result with no simulation at all.

Region filtering goes through a single per-point mask hook, so a future paint gizmo can
supply a weight without touching the sampler/distribution/variation code.

Requires: pxr (OpenUSD), usd_chunks, numpy (bulk mesh maths -- see `_TriangleSoup`).
"""

from __future__ import annotations

import bisect
import logging
import math
import random
import re
from dataclasses import dataclass, field

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils
from usd_chunks import chunk_stage

logger = logging.getLogger(__name__)

_ORIENT_MODES = [
    "Align to Normal",
    "Keep Original",
    "Up",
    "Random",
    "Long Axis Up",
    "Long Axis to Normal",
]
_RAND_AXES = ["Off", "X", "Y", "Z", "All"]
_SCATTER_MODES = ["Surface", "Volume", "Stack"]
_PACK_PATTERNS = ["Hex", "Grid"]
_STACK_ORIENTS = ["Stand Up", "Lay Along Rows", "Keep Original"]

# /asset/crayon{color=red} — the variant value may be "*" (every variant in the set), which
# chunk_utils.split_variant_path's \w+ deliberately doesn't accept, hence the local regex.
_VARIANT_RE = re.compile(r"\{(\w+)=([\w*]+)\}")

_MAX_PROTOTYPES = 64  # a wildcard row on a big variant set shouldn't author 500 prototypes


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


class _TriangleSoup:
    """World-space triangles, held as four (N, 3) arrays rather than N Python tuples.

    A tuple soup was the obvious representation and is unusable past a few thousand faces.
    On a 680k-triangle photogrammetry rock -- an ordinary asset, not a stress test -- just
    *building* the list cost 28 seconds, and ten of those went on constructing the Gf.Vec3f
    objects: the geometry arrives from USD as flat buffers, and turning those into Python
    objects to do float arithmetic on one at a time is the whole expense. Sampling two
    hundred points off it then cost another twenty, re-walking the same list to add up
    areas. Wired to a Scatter Brush, whose every stroke recooks, that was the reported
    five-minute freeze.

    So nothing is ever materialised. Positions, areas, bounds, the slope filter and the
    bucket grids are each one array expression, and the same asset now collects in 0.13s
    and samples in well under a second. Only the few places that genuinely want a single
    triangle -- and the tests -- go through `__getitem__`, which builds its four Gf.Vec3f
    on the spot.

    Immutable by convention: `select` returns a new soup rather than filtering in place, so
    the surface triangles a slope filter produces and the full set Stack mode needs can
    coexist without either copying the other's vertices.
    """

    __slots__ = ("v0", "v1", "v2", "n")

    def __init__(self, v0, v1, v2, n):
        self.v0, self.v1, self.v2, self.n = v0, v1, v2, n

    @classmethod
    def empty(cls):
        zero = np.zeros((0, 3), dtype=np.float64)
        return cls(zero, zero, zero, zero)

    @classmethod
    def from_arrays(cls, points, i0, i1, i2):
        """Triangles from a vertex array and three index arrays, normals included.

        A degenerate face keeps its near-zero raw cross product rather than being dropped:
        the slope filter already rejects anything shorter than 1e-8, and dropping them here
        would renumber every triangle after them for no gain.
        """
        v0, v1, v2 = points[i0], points[i1], points[i2]
        n = np.cross(v1 - v0, v2 - v0)
        length = np.linalg.norm(n, axis=1)
        real = length > 1e-8
        n[real] /= length[real, None]
        return cls(v0, v1, v2, n)

    def __len__(self):
        return len(self.v0)

    def __bool__(self):
        return len(self.v0) > 0

    def __getitem__(self, index):
        return (
            Gf.Vec3f(*self.v0[index]),
            Gf.Vec3f(*self.v1[index]),
            Gf.Vec3f(*self.v2[index]),
            Gf.Vec3f(*self.n[index]),
        )

    def __iter__(self):
        for i in range(len(self.v0)):
            yield self[i]

    def select(self, keep):
        """The subset a boolean mask picks out, as a new soup."""
        return _TriangleSoup(self.v0[keep], self.v1[keep], self.v2[keep], self.n[keep])

    def bounds(self):
        """Axis-aligned (min, max) Gf.Vec3f, or None when empty."""
        if not len(self.v0):
            return None
        corners = np.concatenate((self.v0, self.v1, self.v2))
        return Gf.Vec3f(*corners.min(axis=0)), Gf.Vec3f(*corners.max(axis=0))

    def areas(self):
        """Per-triangle area -- the weight an area-weighted sampler draws against."""
        return 0.5 * np.linalg.norm(np.cross(self.v1 - self.v0, self.v2 - self.v0), axis=1)

    def slope_mask(self, world_up, max_slope_deg):
        """Which triangles face within `max_slope_deg` of world up.

        The rule `_slope_ok` states, asked of every triangle at once. Filtering the soup by
        list comprehension was the last per-triangle Python loop on the Surface path, and on
        a dense mesh it cost more than the sampling it fed.
        """
        if max_slope_deg >= 180.0:
            return np.ones(len(self.v0), dtype=bool)
        up = np.asarray([world_up[0], world_up[1], world_up[2]], dtype=self.n.dtype)
        # Normals are unit length except where the triangle was degenerate -- and those are
        # exactly the ones the scalar rule rejects.
        length = np.linalg.norm(self.n, axis=1)
        angle = np.degrees(np.arccos(np.clip(self.n @ up, -1.0, 1.0)))
        return (length >= 1e-8) & (angle <= max_slope_deg + 1e-4)

    def cell_span(self, axis, origin, cell, cells):
        """Per-triangle (first, last) bucket index along one axis, clamped to the grid.

        Both ray grids bucket triangles by their extent in a couple of axes, and building
        those buckets meant a Python pass over every triangle -- paying up front the cost
        the grids exist to avoid.
        """
        lo = np.minimum(np.minimum(self.v0[:, axis], self.v1[:, axis]), self.v2[:, axis])
        hi = np.maximum(np.maximum(self.v0[:, axis], self.v1[:, axis]), self.v2[:, axis])
        first = np.clip(((lo - origin) / cell).astype(np.int64), 0, cells - 1)
        last = np.clip(((hi - origin) / cell).astype(np.int64), 0, cells - 1)
        return first, last


def _world_points(prim, points):
    """A mesh's points in world space, as an (N, 3) array."""
    matrix = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(prim), dtype=np.float64)
    # Row-vector convention, which is what Gf.Matrix4d.Transform uses: p * M, with the
    # translation in the last row.
    return np.asarray(points, dtype=np.float64) @ matrix[:3, :3] + matrix[3, :3]


def _fan_triangulate(counts, indices):
    """Fan-triangulate every face at once: three index arrays, or None if there are no faces.

    The same fan the per-face loop walked -- (v0, vi, vi+1) for i in 1..count-2 -- as index
    arithmetic, so USD's flat face buffers are never unpacked a face at a time. Faces with
    fewer than three vertices contribute nothing, as before.
    """
    if not len(counts):
        return None
    per_face = np.maximum(counts - 2, 0)
    total = int(per_face.sum())
    if total == 0:
        return None
    face_start = np.concatenate(([0], np.cumsum(counts)[:-1]))
    fan_start = np.concatenate(([0], np.cumsum(per_face)[:-1]))
    # Which face each triangle came from, and how far into that face's fan it is.
    face_of = np.repeat(np.arange(len(counts)), per_face)
    within = np.arange(total) - np.repeat(fan_start, per_face)
    base = face_start[face_of]
    return indices[base], indices[base + within + 1], indices[base + within + 2]


def _collect_triangles(prim):
    """World-space triangles from prim and its descendants, as a `_TriangleSoup`."""
    if not prim or not prim.IsValid():
        return _TriangleSoup.empty()

    parts = []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        counts = mesh.GetFaceVertexCountsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not counts or not indices:
            continue
        fan = _fan_triangulate(
            np.asarray(counts, dtype=np.int64), np.asarray(indices, dtype=np.int64)
        )
        if fan is None:
            continue
        parts.append(_TriangleSoup.from_arrays(_world_points(p, points), *fan))

    if len(parts) == 1:
        return parts[0]
    if parts:
        return _TriangleSoup(*(
            np.concatenate([getattr(part, name) for part in parts])
            for name in ("v0", "v1", "v2", "n")
        ))

    # Fallback: a flat quad on the top face of the bounding box.
    bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
    bbox_range = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
    if bbox_range.IsEmpty():
        return _TriangleSoup.empty()
    mn = bbox_range.GetMin()
    mx = bbox_range.GetMax()
    corners = np.asarray(
        [
            [mn[0], mn[1], mx[2]],
            [mx[0], mn[1], mx[2]],
            [mx[0], mx[1], mx[2]],
            [mn[0], mx[1], mx[2]],
        ],
        dtype=np.float64,
    )
    # (v0, v1, v2) and (v0, v2, v3), the same two triangles as before.
    return _TriangleSoup(
        corners[[0, 0]],
        corners[[1, 2]],
        corners[[2, 3]],
        np.asarray([[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]]),
    )


def _triangle_bounds(triangles):
    """Axis-aligned (min, max) Vec3f of a triangle soup, or None when empty."""
    return triangles.bounds() if triangles is not None else None


def _slope_ok(normal, world_up, max_slope_deg):
    """True if the face is within max_slope_deg of facing world up.

    90 = every upward-facing face (the old default); 0 = only perfectly flat;
    180 = every face (e.g. wrap a sphere).

    One triangle at a time. The cook filters a whole soup through
    `_TriangleSoup.slope_mask`, which is this rule vectorised; this stays for callers
    holding a single normal.
    """
    if max_slope_deg >= 180.0:
        return True
    n = Gf.Vec3f(normal[0], normal[1], normal[2])
    ln = Gf.GetLength(n)
    if ln < 1e-8:
        return False
    cos_a = Gf.Dot(n / ln, world_up)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
    return angle <= max_slope_deg + 1e-4


def _ray_triangles(origin, direction, v0, v1, v2):
    """Moller-Trumbore against many triangles at once: ray parameter t, NaN where missed.

    Vectorised because both ray grids cast thousands of rays and each one tests every
    triangle in its bucket -- which on a dense mesh is hundreds. The tolerances are the
    scalar version's, kept identical so a hit here is a hit there.
    """
    e1 = v1 - v0
    e2 = v2 - v0
    ray = np.broadcast_to(np.asarray(direction, dtype=np.float64), e2.shape)
    h = np.cross(ray, e2)
    a = np.einsum("ij,ij->i", e1, h)
    with np.errstate(divide="ignore", invalid="ignore"):
        f = 1.0 / a
        s = np.asarray(origin, dtype=np.float64) - v0
        u = f * np.einsum("ij,ij->i", s, h)
        q = np.cross(s, e1)
        v = f * np.einsum("ij,ij->i", ray, q)
        t = f * np.einsum("ij,ij->i", e2, q)
    hit = (
        (np.abs(a) >= 1e-12)
        & (u >= -1e-6)
        & (u <= 1.0 + 1e-6)
        & (v >= -1e-6)
        & (u + v <= 1.0 + 1e-6)
        & (t > 1e-9)
    )
    return np.where(hit, t, np.nan)


class _PoissonGrid:
    """Spatial hash for O(1) min-distance rejection during sampling."""

    def __init__(self, min_dist):
        self._grid: dict = {}
        self._cell = max(min_dist, 1e-6)
        self._min_sq = min_dist * min_dist

    def _key(self, pt):
        c = self._cell
        return (math.floor(pt[0] / c), math.floor(pt[1] / c), math.floor(pt[2] / c))

    def too_close(self, pt):
        if self._min_sq <= 0:
            return False
        kx, ky, kz = self._key(pt)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for dz in range(-1, 2):
                    for ex in self._grid.get((kx + dx, ky + dy, kz + dz), ()):
                        d = (pt[0] - ex[0]) ** 2 + (pt[1] - ex[1]) ** 2 + (pt[2] - ex[2]) ** 2
                        if d < self._min_sq:
                            return True
        return False

    def add(self, pt):
        self._grid.setdefault(self._key(pt), []).append(pt)


class _AvoidSet:
    """Points to keep away from -- a curve's samples, another scatter -- on a spatial hash."""

    def __init__(self, points, distance):
        self._dist = max(distance, 1e-9)
        self._cell = self._dist
        self._grid: dict = {}
        for p in np.asarray(points, dtype=np.float64).reshape(-1, 3):
            self._grid.setdefault(self._key(p), []).append(p)

    def _key(self, pt):
        c = self._cell
        return (math.floor(pt[0] / c), math.floor(pt[1] / c), math.floor(pt[2] / c))

    def near(self, pt, extra=0.0):
        reach = self._dist + max(extra, 0.0)
        r = int(math.ceil(reach / self._cell))
        kx, ky, kz = self._key(pt)
        lim = reach * reach
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    for ex in self._grid.get((kx + dx, ky + dy, kz + dz), ()):
                        if (pt[0] - ex[0]) ** 2 + (pt[1] - ex[1]) ** 2 + (pt[2] - ex[2]) ** 2 < lim:
                            return True
        return False


def _avoid_points(stage, paths, step):
    """World-space points of every prim under *paths*; curves are sampled every *step* along."""
    out = []
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    for path in paths:
        root = stage.GetPrimAtPath(path) if path else None
        if not root:
            continue
        for prim in Usd.PrimRange(root):
            if prim.IsA(UsdGeom.PointInstancer):
                attr = UsdGeom.PointInstancer(prim).GetPositionsAttr()
            elif prim.IsA(UsdGeom.PointBased):
                attr = UsdGeom.PointBased(prim).GetPointsAttr()
            else:
                continue
            value = attr.Get()
            if value is None and attr.GetNumTimeSamples():
                value = attr.Get(attr.GetTimeSamples()[-1])
            if not value:
                continue
            pts = np.asarray(value, dtype=np.float64).reshape(-1, 3)
            if prim.IsA(UsdGeom.Curves):
                counts = UsdGeom.Curves(prim).GetCurveVertexCountsAttr().Get() or [len(pts)]
                dense, k = [], 0
                for c in counts:
                    seg = pts[k:k + c]
                    k += c
                    dense.append(seg[:1])
                    for a, b in zip(seg[:-1], seg[1:]):
                        n = max(1, int(math.ceil(float(np.linalg.norm(b - a)) / max(step, 1e-9))))
                        t = (np.arange(1, n + 1) / n)[:, None]
                        dense.append(a + (b - a) * t)
                pts = np.concatenate(dense) if dense else pts
            m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
            out.append(pts @ m[:3, :3] + m[3, :3])
    return np.concatenate(out) if out else np.zeros((0, 3))


def _draw_scale(rng, lo, hi, bias):
    """A random scale in lo..hi; *bias* above 1 favours the small end (1 = uniform)."""
    u = rng.random()
    return lo + (hi - lo) * (u ** bias if bias != 1.0 else u)


def _scatter_sized(triangles, count, rng, spacing, scale_rng, scale_lo, scale_hi, bias,
                   mask=None, avoid=None, vary=True):
    """Like _scatter_on_triangles, but each candidate draws its scale first and keeps
    Spacing x scale / Scale Max from the others -- small instances pack in between big ones,
    and none overlap. Returns positions, normals and the scale each one drew."""
    if not len(triangles):
        return [], [], []
    areas = triangles.areas()
    total_area = float(areas.sum())
    if total_area < 1e-10:
        return [], [], []
    cdf = (np.cumsum(areas) / total_area).tolist()
    v0s, v1s, v2s, normals_all = triangles.v0, triangles.v1, triangles.v2, triangles.n
    # Radius per unit of scale -- or, with *vary* off, every instance keeps plain Spacing.
    unit = spacing / max(scale_hi, 1e-9) * 0.5 if vary else 0.0
    cell = max(spacing, 1e-6)
    grid: dict = {}
    positions, normals, scales = [], [], []
    attempts, max_attempts = 0, count * 100
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        tri_idx = min(bisect.bisect_left(cdf, rng.random()), len(triangles) - 1)
        u, v = rng.random(), rng.random()
        if u + v > 1.0:
            u, v = 1.0 - u, 1.0 - v
        w = 1.0 - u - v
        v0, v1, v2 = v0s[tri_idx], v1s[tri_idx], v2s[tri_idx]
        pt = (w * v0[0] + u * v1[0] + v * v2[0], w * v0[1] + u * v1[1] + v * v2[1],
              w * v0[2] + u * v1[2] + v * v2[2])
        s = _draw_scale(scale_rng, scale_lo, scale_hi, bias)
        r = unit * s if vary else spacing * 0.5
        if avoid is not None and avoid.near(pt, r):
            continue
        key = (math.floor(pt[0] / cell), math.floor(pt[1] / cell), math.floor(pt[2] / cell))
        clash = False
        if r > 0:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for ex, er in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                            if (pt[0] - ex[0]) ** 2 + (pt[1] - ex[1]) ** 2 + (pt[2] - ex[2]) ** 2 < (r + er) ** 2:
                                clash = True
                                break
                        if clash:
                            break
                    if clash:
                        break
        if clash:
            continue
        n = Gf.Vec3f(*normals_all[tri_idx])
        if mask is not None and rng.random() > mask(Gf.Vec3f(*pt), n):
            continue
        grid.setdefault(key, []).append((pt, r))
        positions.append(Gf.Vec3f(*pt))
        normals.append(n)
        scales.append(s)
    return positions, normals, scales


class _TriangleBuckets:
    """Triangles bucketed by their extent in two axes, for casting rays along the third.

    Both ray users want the same thing -- "which faces can a ray at this lateral position
    possibly hit" -- and each had grown its own copy of the grid: Stack's `_SurfaceRayGrid`
    bucketing in the two non-up axes, Volume's point-in-mesh test bucketing in (y, z). Two
    hand-rolled dictionaries, two clamped key functions, one bug surface each.

    The build is a single array expression. On a 680k-triangle mesh, filling the dictionary a
    triangle at a time cost more than every ray it went on to accelerate.
    """

    __slots__ = ("_axes", "_cells", "_origin", "_cell", "_groups")

    def __init__(self, triangles, axes, cells=64):
        self._axes = tuple(axes)
        self._cells = max(1, int(cells))
        self._groups: dict = {}

        bounds = triangles.bounds()
        if bounds is None:
            self._origin = (0.0, 0.0)
            self._cell = (1.0, 1.0)
            return

        low, high = bounds
        a, b = self._axes
        self._origin = (low[a], low[b])
        self._cell = (
            max((high[a] - low[a]) / self._cells, 1e-9),
            max((high[b] - low[b]) / self._cells, 1e-9),
        )

        first_a, last_a = triangles.cell_span(a, self._origin[0], self._cell[0], self._cells)
        first_b, last_b = triangles.cell_span(b, self._origin[1], self._cell[1], self._cells)
        self._groups = _group_by_cell(first_a, last_a, first_b, last_b, self._cells)

    def key(self, value_a, value_b):
        return (
            max(0, min(self._cells - 1, int((value_a - self._origin[0]) / self._cell[0]))),
            max(0, min(self._cells - 1, int((value_b - self._origin[1]) / self._cell[1]))),
        )

    def candidates(self, value_a, value_b):
        """Indices of every triangle whose extent covers this lateral position, or None."""
        return self._groups.get(self.key(value_a, value_b))


def _group_by_cell(first_a, last_a, first_b, last_b, cells):
    """`{(a, b): triangle indices}` for triangles spanning those inclusive cell ranges.

    A triangle usually falls in one cell, but a large one straddles several and has to
    appear in each -- so the spans are expanded before grouping. Both steps are array
    arithmetic: the expansion by `repeat`, the grouping by sorting on a flattened key and
    splitting where it changes.
    """
    span_a = last_a - first_a + 1
    span_b = last_b - first_b + 1
    per_triangle = span_a * span_b
    total = int(per_triangle.sum())
    if total == 0:
        return {}

    triangle = np.repeat(np.arange(len(per_triangle)), per_triangle)
    block_start = np.concatenate(([0], np.cumsum(per_triangle)[:-1]))
    within = np.arange(total) - np.repeat(block_start, per_triangle)
    wide_b = np.repeat(span_b, per_triangle)
    cell_a = np.repeat(first_a, per_triangle) + within // wide_b
    cell_b = np.repeat(first_b, per_triangle) + within % wide_b

    flat = cell_a * cells + cell_b
    order = np.argsort(flat, kind="stable")
    flat = flat[order]
    triangle = triangle[order]
    keys, starts = np.unique(flat, return_index=True)
    return {
        (int(k) // cells, int(k) % cells): group
        for k, group in zip(keys, np.split(triangle, starts[1:]))
    }


class _SurfaceRayGrid:
    """Downward ray casts against a triangle soup, for Stack mode.

    Stack needs "where does the surface sit under this lattice point" thousands of times.
    The bucketing keeps each cast to the faces the ray can actually reach; `_ray_triangles`
    then tests that whole bucket in one call rather than one triangle at a time.
    """

    def __init__(self, triangles, up_idx, cells=64):
        self._triangles = triangles
        self._up_idx = up_idx
        self._axes = [i for i in range(3) if i != up_idx]
        self._buckets = _TriangleBuckets(triangles, self._axes, cells)
        bounds = triangles.bounds()
        self._min, self._max = bounds if bounds else (Gf.Vec3f(0, 0, 0), Gf.Vec3f(0, 0, 0))

    def hit(self, lateral_a, lateral_b, up_dir):
        """Highest (point, normal) under the given lateral position, or None."""
        candidates = self._buckets.candidates(lateral_a, lateral_b)
        if candidates is None or not len(candidates):
            return None
        a, b = self._axes
        origin = [0.0, 0.0, 0.0]
        origin[a] = lateral_a
        origin[b] = lateral_b
        # Start clear of the mesh and cast down; the first surface met from above is the one
        # an instance should rest on (a bowl's inner floor, not its underside).
        span = self._max[self._up_idx] - self._min[self._up_idx]
        origin[self._up_idx] = self._max[self._up_idx] + max(1.0, span)

        direction = np.asarray([-up_dir[0], -up_dir[1], -up_dir[2]], dtype=np.float64)
        soup = self._triangles
        t = _ray_triangles(
            np.asarray(origin, dtype=np.float64),
            direction,
            soup.v0[candidates],
            soup.v1[candidates],
            soup.v2[candidates],
        )
        if np.all(np.isnan(t)):
            return None
        nearest = int(np.nanargmin(t))
        point = np.asarray(origin) + direction * t[nearest]
        return Gf.Vec3f(*point), Gf.Vec3f(*soup.n[candidates[nearest]])


#: Per-vertex float, 0..1, saying how much scatter a part of the surface wants. Painted by
#: the brush; ordinary USD, so a DCC's own paint tool or a bake can author it just as well.
DENSITY_PRIMVAR = "scatterDensity"

#: How finely the lookup grid divides the surface's bounds, at its coarsest. The grid only
#: has to be fine enough that a cell holds few vertices, since a query answers with the
#: nearest one in it -- past that it costs memory for nothing.
#: How many vertices a lookup cell should hold. Everything about the grid follows from this
#: one number, because it is what fixes the cell size *relative to the vertices* rather than
#: to the bounding box -- and a query only ever searches the 27 cells around it, so a cell
#: measured against anything else is a reach that means something different on every mesh.
#:
#: At this figure a cell comes out about 2.8 vertex spacings across whatever the density, so
#: the neighbourhood always reaches roughly four spacings and a candidate anywhere on a
#: triangle finds the vertices of that triangle.
_LOOKUP_TARGET_PER_CELL = 8

#: Past this the dictionary costs more than the search it saves.
_LOOKUP_MAX_DIVISIONS = 512


def _lookup_divisions(vertex_count):
    """How finely the lookup grid divides the painted surface's bounds.

    Derived from the vertex count, with no floor under it. A fixed 48 was the original, and
    it is wrong at both ends: far too coarse for a scan (340k vertices over 48 divisions is
    150 per cell, four thousand distances per query -- the brute force the grid exists to
    avoid) and, less obviously, far too *fine* for anything hand-made.

    The fine end was the one that produced wrong pictures rather than slow ones. On a floor
    of a few quads, cells of a forty-eighth of the surface are far smaller than the gap
    between vertices, so everything in the middle of a quad sits in an empty neighbourhood
    and reads as unpainted. A fully painted 20-unit quad scattered 139 of 200 instances, all
    of them within 1.1 units of a corner, in four clumps -- and the sampler's rejection loop
    hid it by simply retrying until it found somewhere near a vertex to put them.
    """
    wanted = math.sqrt(max(vertex_count, 1) / _LOOKUP_TARGET_PER_CELL)
    return int(max(1, min(_LOOKUP_MAX_DIVISIONS, wanted)))


#: Cell ids are packed into one integer -- `(x * S + y) * S + z` -- so a cell groups with
#: np.unique and looks up without a tuple being built per query. Wide enough that no two
#: cells of any grid this builds can land on the same id, and the packing is linear, so a
#: neighbour's id is this cell's plus a constant.
_KEY_STRIDE = 1 << 20

#: The 27-cell neighbourhood, as offsets in packed-id space.
_KEY_OFFSETS = np.asarray(
    [
        (dx * _KEY_STRIDE + dy) * _KEY_STRIDE + dz
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
    ],
    dtype=np.int64,
)

#: Shared empty result, so a query landing outside the painted region allocates nothing.
_NO_INDICES = np.zeros(0, dtype=np.int64)


#: What only means something when scatter is choosing the positions itself. Wire painted
#: points in and every one of them is answering a question that has already been settled.
#:
#: Mode is deliberately NOT in here. It looks like it belongs -- with points wired, Surface and
#: Volume do behave the same -- but Stack ignores the painted cloud on purpose, so Mode is the
#: one control that still decides something. Hiding it stranded you: connect a brush and the
#: only route to Stack disappeared, with no way back short of unwiring.
_SAMPLING_ONLY = ("auto_spacing", "spacing", "max_count", "seed", "max_slope")


def _update_sampling_params(owner) -> None:
    """Hide the distribution controls while the positions are being handed to us.

    Reported live: with Painted Points connected, Auto Spacing, Spacing, Max Count and Seed
    all still sat there doing nothing. A control that cannot affect the result is worse than
    an absent one -- it invites you to reach for it when something looks wrong.
    """
    placed = bool(owner.get_param("points_prim") and owner.get_param("points_prim").value)
    for name in _SAMPLING_ONLY:
        param = owner.get_param(name)
        if param is None:
            continue
        if placed:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)


def _on_points_input_changed(owner, notice, sender) -> None:
    _update_sampling_params(owner.get_chunk())


def _painted_points(stage, points_path):
    """Points a brush placed by hand, with the surface normal recorded at each.

    Empty whenever nothing is wired, which is the common case and costs one path lookup.

    The normals are why this returns a pair. A scatter has the triangle it sampled and can
    take the facing from that; a placed point has no triangle, so the brush records the
    surface's facing when the dab lands -- the same job Houdini's N does on a point.
    """
    if not points_path or stage is None:
        return [], []
    prim = stage.GetPrimAtPath(points_path)
    if not prim or not prim.IsValid():
        return [], []

    cloud = UsdGeom.Points(prim)
    points = cloud.GetPointsAttr().Get()
    if not points:
        return [], []
    normals = cloud.GetNormalsAttr().Get()

    up = Gf.Vec3f(0, 0, 1)
    positions = [Gf.Vec3f(*p) for p in points]
    facing = [Gf.Vec3f(*n) for n in normals] if normals else []
    # Points placed before normals were recorded still work; they simply lie flat.
    facing += [up] * (len(positions) - len(facing))
    return positions, facing[:len(positions)]


def _density_mask(prim, primvar_name=None):
    """A weight function over the painted surface, or None when nothing is painted.

    `primvar_name` is which painted field to read, and it comes off this chunk's Density Field
    input -- normally wired from a Scatter Brush's output rather than typed. Empty means *no
    field*, and an unconnected scatter therefore behaves exactly as it did before painting
    existed. That is the point of the wire: the dependency is visible, and a scatter nobody
    connected a brush to is not quietly governed by one.

    None rather than a function returning 1.0, deliberately: the sampler skips the hook
    entirely when there is no mask, so an unpainted scatter costs exactly what it did before
    this existed. That matters -- the hook runs per candidate point, and candidates outnumber
    instances by whatever the rejection rate happens to be.

    Sampling is nearest-vertex. The weight is authored per vertex, and the sampler hands over
    a point with no idea which triangle produced it, so there is nothing to interpolate
    barycentrically against. Density resolution is therefore mesh resolution: paint a
    twelve-triangle floor and the edge of the painted region is blocky. Worth knowing before
    it is discovered.
    """
    primvar_name = primvar_name or ""
    if not prim or not prim.IsValid() or not primvar_name:
        return None

    point_parts, weight_parts = [], []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        mesh_points = mesh.GetPointsAttr().Get()
        if not mesh_points:
            continue
        primvar = UsdGeom.PrimvarsAPI(p).GetPrimvar(primvar_name)
        if not primvar or not primvar.HasValue():
            continue

        values = primvar.ComputeFlattened()
        if values is None:
            continue
        values = np.asarray(values, dtype=np.float64)
        # Constant interpolation is one number for the whole mesh -- a legitimate way to dial
        # a surface down without painting it, and it has no per-vertex values to index.
        if len(values) == 1:
            values = np.full(len(mesh_points), values[0])
        if len(values) < len(mesh_points):
            logger.warning(
                "%s has %d %s values for %d points; ignoring it",
                p.GetPath(), len(values), primvar_name, len(mesh_points),
            )
            continue

        point_parts.append(_world_points(p, mesh_points))
        weight_parts.append(values[:len(mesh_points)])

    if not point_parts:
        return None
    return _NearestWeight(
        np.concatenate(point_parts), np.concatenate(weight_parts)
    ).sample


class _NearestWeight:
    """Nearest-vertex weight lookup over a uniform grid.

    Brute force over every vertex would be O(V) per candidate point, and the sampler makes up
    to a hundred candidates per instance -- on a real mesh that is the whole cook. The grid
    makes it near-constant at the cost of one pass at build time.

    Both halves are array work. Building the grid a vertex at a time, each one first
    transformed to world space through Gf, took tens of seconds on a scanned mesh -- and it
    happens on every cook, so a Scatter Brush upstream paid it on every stroke.
    """

    __slots__ = ("_points", "_weights", "_origin", "_cell", "_cells", "_neighbourhood")

    def __init__(self, points, weights):
        self._points = np.asarray(points, dtype=np.float64)
        self._weights = np.clip(np.asarray(weights, dtype=np.float64), 0.0, 1.0)

        low = self._points.min(axis=0)
        span = max(float((self._points.max(axis=0) - low).max()), 1e-6)
        divisions = _lookup_divisions(len(self._points))
        self._origin = low
        self._cell = span / divisions

        keys = self._keys(self._points)
        order = np.argsort(keys, kind="stable")
        unique, starts = np.unique(keys[order], return_index=True)
        self._cells = dict(zip(unique.tolist(), np.split(np.arange(len(keys))[order], starts[1:])))
        # A cell's 27-neighbourhood, merged on first use. Candidates cluster -- a scatter
        # makes many of them in the same region -- so the merge is paid once per cell rather
        # than once per query, which is where the concatenation cost would otherwise land.
        self._neighbourhood: dict = {}

    def _keys(self, points):
        """Flat cell ids for an (N, 3) array of points.

        Flat rather than a (x, y, z) tuple: an integer key groups with `np.unique` and looks
        up without building a tuple per query. `_KEY_STRIDE` is far larger than any grid this
        builds, so distinct cells cannot collide onto one id.
        """
        cell = np.floor_divide(np.asarray(points) - self._origin, self._cell).astype(np.int64)
        return (cell[..., 0] * _KEY_STRIDE + cell[..., 1]) * _KEY_STRIDE + cell[..., 2]

    def _nearby(self, key):
        merged = self._neighbourhood.get(key)
        if merged is None:
            parts = [self._cells[k] for k in key + _KEY_OFFSETS if k in self._cells]
            merged = np.concatenate(parts) if parts else _NO_INDICES
            self._neighbourhood[key] = merged
        return merged

    def sample(self, point, normal=None):
        """The painted weight nearest `point`, in 0..1. `normal` is unused but is part of the
        hook's signature -- a future falloff-by-facing would want it."""
        # The point can land nearer a vertex in an adjacent cell than one in its own, and an
        # empty cell is ordinary on a coarse mesh, so the neighbours are always searched.
        nearby = self._nearby(int(self._keys(np.asarray([point[0], point[1], point[2]]))))
        if not len(nearby):
            # Nothing painted anywhere near: outside the painted region entirely.
            return 0.0
        offset = self._points[nearby] - np.asarray([point[0], point[1], point[2]])
        return float(self._weights[nearby[np.einsum("ij,ij->i", offset, offset).argmin()]])


def _scatter_on_triangles(triangles, count, rng, min_dist=0, mask=None):
    """Area-weighted point sampling with min-distance rejection.

    *mask*: optional per-point hook ``mask(point, normal) -> 0..1`` — the paint
    gizmo will pass a weight function here; a candidate is kept with that
    probability. None means accept everything that passes spacing.

    The area table and its cumulative distribution are built in one array pass -- adding up
    679,936 triangle areas a Gf.Vec3f cross product at a time took twenty seconds, which was
    most of a cook on a photogrammetry mesh. The draw loop itself is deliberately unchanged:
    it consumes the generator in exactly the same order (triangle, u, v, then the mask's
    roll), so a given seed still produces the scatter it always did.
    """
    if not len(triangles):
        return [], []

    areas = triangles.areas()
    total_area = float(areas.sum())
    if total_area < 1e-10:
        return [], []
    # Back to a plain list for the draw: bisect over one is faster per call than
    # np.searchsorted, which pays array-protocol overhead on a single scalar.
    cdf = (np.cumsum(areas) / total_area).tolist()

    v0s, v1s, v2s, normals_all = triangles.v0, triangles.v1, triangles.v2, triangles.n
    positions, normals = [], []
    grid = _PoissonGrid(min_dist)
    max_attempts = count * 100
    attempts = 0
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        tri_idx = min(bisect.bisect_left(cdf, rng.random()), len(triangles) - 1)

        u, v = rng.random(), rng.random()
        if u + v > 1.0:
            u, v = 1.0 - u, 1.0 - v
        w = 1.0 - u - v
        v0, v1, v2 = v0s[tri_idx], v1s[tri_idx], v2s[tri_idx]
        pt = Gf.Vec3f(
            w * v0[0] + u * v1[0] + v * v2[0],
            w * v0[1] + u * v1[1] + v * v2[1],
            w * v0[2] + u * v1[2] + v * v2[2],
        )
        n = Gf.Vec3f(*normals_all[tri_idx])

        if grid.too_close(pt):
            continue
        if mask is not None and rng.random() > mask(pt, n):
            continue

        positions.append(pt)
        normals.append(n)
        grid.add(pt)

    return positions, normals


def _fill_in_volume(triangles, count, rng, min_dist=0, mask=None):
    """Uniformly fill the interior of a closed mesh with random points.

    Point-in-mesh is an odd/even ray cast along +X, against the triangles `_TriangleBuckets`
    says the ray can reach at that (y, z). The cast tests the whole bucket in one array
    call: on a dense mesh a bucket holds hundreds of faces, and walking them in Python was
    the cost the bucketing exists to remove.
    """
    if not len(triangles):
        return [], []

    low, high = triangles.bounds()
    buckets = _TriangleBuckets(triangles, (1, 2), cells=_volume_cells(len(triangles)))
    direction = np.asarray([1.0, 0.0, 0.0])

    def _inside(pt):
        candidates = buckets.candidates(pt[1], pt[2])
        if candidates is None or not len(candidates):
            return False
        t = _ray_triangles(
            np.asarray([pt[0], pt[1], pt[2]], dtype=np.float64),
            direction,
            triangles.v0[candidates],
            triangles.v1[candidates],
            triangles.v2[candidates],
        )
        return int(np.count_nonzero(~np.isnan(t))) % 2 == 1

    positions, normals = [], []
    up = Gf.Vec3f(0, 1, 0)  # caller replaces with world up
    grid = _PoissonGrid(min_dist)
    max_attempts = count * 200
    attempts = 0
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        pt = Gf.Vec3f(
            rng.uniform(low[0], high[0]),
            rng.uniform(low[1], high[1]),
            rng.uniform(low[2], high[2]),
        )
        if not _inside(pt):
            continue
        if grid.too_close(pt):
            continue
        if mask is not None and rng.random() > mask(pt, up):
            continue
        positions.append(pt)
        normals.append(up)
        grid.add(pt)

    return positions, normals


def _volume_cells(triangle_count):
    """Grid resolution for the point-in-mesh test, from how dense the mesh is.

    A fixed 32 was fine for the hand-built meshes this was written against and hopeless on a
    scanned one: 680k triangles over 1024 cells is 660 faces to test per candidate, on every
    candidate. Scaled so a cell holds a handful, and capped so a coarse mesh does not get a
    grid mostly made of empty cells.
    """
    return int(max(32, min(256, math.sqrt(max(triangle_count, 1) / 4.0))))


# ---------------------------------------------------------------------------
# rotation helpers — internal math in Quatf, converted to Quath only on write
# ---------------------------------------------------------------------------


def _align_quat(from_dir, to_dir):
    """Shortest-arc rotation taking from_dir to to_dir."""
    a = Gf.Vec3f(from_dir[0], from_dir[1], from_dir[2])
    b = Gf.Vec3f(to_dir[0], to_dir[1], to_dir[2])
    la, lb = Gf.GetLength(a), Gf.GetLength(b)
    if la < 1e-8 or lb < 1e-8:
        return Gf.Quatf(1, 0, 0, 0)
    a, b = a / la, b / lb
    dot = Gf.Dot(a, b)
    if dot > 0.9999:
        return Gf.Quatf(1, 0, 0, 0)
    if dot < -0.9999:
        # Antiparallel: any perpendicular axis is a valid 180° turn.
        axis = Gf.Cross(a, Gf.Vec3f(1, 0, 0))
        if Gf.GetLength(axis) < 1e-6:
            axis = Gf.Cross(a, Gf.Vec3f(0, 1, 0))
        axis = axis / Gf.GetLength(axis)
        return Gf.Quatf(0, axis[0], axis[1], axis[2])
    axis = Gf.Cross(a, b)
    axis = axis / Gf.GetLength(axis)
    half = math.acos(max(-1.0, min(1.0, dot))) * 0.5
    s = math.sin(half)
    return Gf.Quatf(math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def _normal_to_quat(normal):
    """Rotation taking +Z to the given normal direction."""
    return _align_quat(Gf.Vec3f(0, 0, 1), normal)


def _axis_quat(axis, angle_rad):
    a = Gf.Vec3f(axis[0], axis[1], axis[2])
    ln = Gf.GetLength(a)
    if ln < 1e-8:
        return Gf.Quatf(1, 0, 0, 0)
    a = a / ln
    half = angle_rad * 0.5
    s = math.sin(half)
    return Gf.Quatf(math.cos(half), a[0] * s, a[1] * s, a[2] * s)


def _quat_mul(a, b):
    aw, ax, ay, az = a.GetReal(), *a.GetImaginary()
    bw, bx, by, bz = b.GetReal(), *b.GetImaginary()
    return Gf.Quatf(
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _rotate_vec(quat, vec):
    """Rotate vec by quat: v + 2w(u×v) + 2u×(u×v), u = imaginary part."""
    w = quat.GetReal()
    im = quat.GetImaginary()
    u = Gf.Vec3f(im[0], im[1], im[2])
    v = Gf.Vec3f(vec[0], vec[1], vec[2])
    uv = Gf.Cross(u, v)
    uuv = Gf.Cross(u, uv)
    return v + uv * (2.0 * w) + uuv * 2.0


def _to_quath(quat):
    im = quat.GetImaginary()
    return Gf.Quath(quat.GetReal(), im[0], im[1], im[2])


def _random_quat(rng):
    """Uniform random orientation (Shoemake)."""
    u1, u2, u3 = rng.random(), rng.random(), rng.random()
    s1, s2 = math.sqrt(1 - u1), math.sqrt(u1)
    return Gf.Quatf(
        s2 * math.cos(2 * math.pi * u3),
        s1 * math.sin(2 * math.pi * u2),
        s1 * math.cos(2 * math.pi * u2),
        s2 * math.sin(2 * math.pi * u3),
    )


def _compute_orientation(mode, normal, rng, proto=None, world_up=None):
    """Base orientation for one instance, before random spin/tilt."""
    if mode == "Align to Normal":
        return _normal_to_quat(normal)
    if mode == "Random":
        return _random_quat(rng)
    if mode == "Long Axis Up" and proto is not None:
        return _align_quat(proto.long_axis_dir(), world_up or Gf.Vec3f(0, 0, 1))
    if mode == "Long Axis to Normal" and proto is not None:
        return _align_quat(proto.long_axis_dir(), normal)
    # "Keep Original" / "Up" — prototype's own orientation.
    return Gf.Quatf(1, 0, 0, 0)


def _apply_random_rotation(base_quat, axis, amount_deg, rng):
    if axis == "Off":
        return base_quat
    amount_rad = math.radians(amount_deg)
    if axis == "All":
        ax = Gf.Vec3f(rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))
        ln = Gf.GetLength(ax)
        if ln < 1e-8:
            return base_quat
        ax = ax / ln
    else:
        ax = {"X": Gf.Vec3f(1, 0, 0), "Y": Gf.Vec3f(0, 1, 0), "Z": Gf.Vec3f(0, 0, 1)}[axis]
    angle = rng.uniform(-amount_rad, amount_rad)
    return _quat_mul(_axis_quat(ax, angle), base_quat)


# ---------------------------------------------------------------------------
# prototypes
# ---------------------------------------------------------------------------


@dataclass
class _Proto:
    """One resolved prototype: where it came from, how much of the mix it takes, how big it is.

    ``bmin``/``bmax`` are the *untransformed* local bounds — the prototype reference has its
    xformOpOrder cleared, so that is what the instancer actually instances — and ``scale`` is
    the source prim's world scale, which the instancer re-applies per instance. Effective
    world size is therefore ``(bmax - bmin) * scale * instance_scale``.
    """

    src_path: str = ""
    variants: tuple = ()
    weight: float = 100.0
    scale_mult: float = 1.0
    label: str = ""
    path: str = ""
    bmin: Gf.Vec3f = field(default_factory=lambda: Gf.Vec3f(-0.5, -0.5, -0.5))
    bmax: Gf.Vec3f = field(default_factory=lambda: Gf.Vec3f(0.5, 0.5, 0.5))
    scale: Gf.Vec3f = field(default_factory=lambda: Gf.Vec3f(1, 1, 1))

    def dims(self, uniform_scale=1.0):
        s = uniform_scale
        return Gf.Vec3f(
            abs(self.bmax[0] - self.bmin[0]) * self.scale[0] * s,
            abs(self.bmax[1] - self.bmin[1]) * self.scale[1] * s,
            abs(self.bmax[2] - self.bmin[2]) * self.scale[2] * s,
        )

    def long_axis(self):
        d = self.dims()
        return max(range(3), key=lambda i: d[i])

    def long_axis_dir(self):
        v = [0.0, 0.0, 0.0]
        v[self.long_axis()] = 1.0
        return Gf.Vec3f(*v)

    def corners(self, uniform_scale=1.0):
        lo = [self.bmin[i] * self.scale[i] * uniform_scale for i in range(3)]
        hi = [self.bmax[i] * self.scale[i] * uniform_scale for i in range(3)]
        return [
            Gf.Vec3f(x, y, z)
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]


def _extent_along(corners, quat, axis):
    """(lo, hi) of the rotated corner cloud projected on axis."""
    lo = hi = None
    for c in corners:
        d = Gf.Dot(_rotate_vec(quat, c), axis)
        lo = d if lo is None or d < lo else lo
        hi = d if hi is None or d > hi else hi
    return (lo or 0.0), (hi or 0.0)


def _lateral_size(corners, quat, axis_a, axis_b):
    lo_a, hi_a = _extent_along(corners, quat, axis_a)
    lo_b, hi_b = _extent_along(corners, quat, axis_b)
    return hi_a - lo_a, hi_b - lo_b


def _parse_proto_spec(raw):
    """Split "/path/crayon{color=red}" into ("/path/crayon", (("color", "red"),))."""
    selections = tuple(_VARIANT_RE.findall(raw))
    return _VARIANT_RE.sub("", raw).strip(), selections


def _resolve_prototypes(node, scratch_stage):
    """Turn the Prototypes list rows into _Proto entries.

    Handles, per row: glob expansion (one prototype per matched prim), an explicit variant
    selection, and ``{set=*}`` wildcards (one prototype per variant in the set, the row's
    weight split evenly between them).
    """
    rows = node.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return []
    path_t = rows.get_param("proto_path")
    weight_t = rows.get_param("proto_weight")
    scale_t = rows.get_param("proto_scale")

    protos: list[_Proto] = []
    for i in range(min(rows.size, len(rows.get_my_params()))):
        try:
            raw = rows.get_indexed_param(path_t, i).value or ""
            weight = float(rows.get_indexed_param(weight_t, i).value or 0.0)
            scale_mult = float(rows.get_indexed_param(scale_t, i).value or 1.0)
        except (IndexError, ValueError, AttributeError):
            continue
        if not raw.strip() or weight <= 0.0:
            continue

        for token in re.split(r"[\s,]+", raw.strip()):
            if not token:
                continue
            base, selections = _parse_proto_spec(token)
            if not base:
                continue
            matched = chunk_utils.resolve_prim_paths(scratch_stage, base) if scratch_stage else [base]
            for path in matched:
                for expanded in _expand_variants(scratch_stage, path, selections):
                    protos.append(
                        _Proto(
                            src_path=path,
                            variants=expanded,
                            weight=weight,
                            scale_mult=scale_mult,
                            label=_proto_label(path, expanded),
                        )
                    )
    if len(protos) > _MAX_PROTOTYPES:
        del protos[_MAX_PROTOTYPES:]
    # A wildcard row splits its weight across the variants it expanded to, so one row is one
    # share of the mix no matter how many variants it turns out to cover.
    per_source: dict[str, int] = {}
    for p in protos:
        per_source[p.src_path] = per_source.get(p.src_path, 0) + 1
    for p in protos:
        p.weight /= per_source[p.src_path]
    return protos


def _expand_variants(stage, prim_path, selections):
    """Yield one selection tuple per concrete combination, expanding any "*" values."""
    combos = [()]
    prim = stage.GetPrimAtPath(prim_path) if stage else None
    for vset_name, value in selections:
        if value != "*":
            combos = [c + ((vset_name, value),) for c in combos]
            continue
        names = []
        if prim and prim.IsValid():
            vset = prim.GetVariantSets().GetVariantSet(vset_name)
            if vset:
                names = list(vset.GetVariantNames())
        if not names:
            # Unknown set (or a prim that isn't there yet) — drop the wildcard rather than
            # authoring a bogus selection.
            continue
        combos = [c + ((vset_name, n),) for c in combos for n in names]
    return combos


def _proto_label(path, selections):
    name = path.rsplit("/", 1)[-1] or path
    if selections:
        name += "".join(f"{{{s}={v}}}" for s, v in selections)
    return name


def _author_prototypes(stage, inst_path, protos, scratch_stage):
    """Author one prototype prim per _Proto under <inst_path>/Prototypes.

    Each is an internal reference to the source prim with its xformOpOrder cleared (the
    instancer supplies the transform) plus any variant selection for that entry — the same
    asset can therefore appear several times, each row selecting a different variant.

    Returns the prototypes that actually resolved; an empty result means the caller should
    fall back to the default cube.
    """
    proto_scope = f"{inst_path}/Prototypes"
    stage.DefinePrim(proto_scope, "Scope")

    resolved: list[_Proto] = []
    for i, proto in enumerate(protos):
        src = scratch_stage.GetPrimAtPath(proto.src_path) if scratch_stage else None
        if not src or not src.IsValid():
            continue
        path = f"{proto_scope}/proto_{len(resolved)}"
        ref_prim = stage.DefinePrim(path)
        ref_prim.GetReferences().AddInternalReference(Sdf.Path(proto.src_path))
        ref_prim.SetInstanceable(False)
        # src can be valid on the upstream composition yet its real source (a sublayer, a
        # reference chunk) may have vanished from the live stage between the lookup and now
        # -- the internal reference then resolves to nothing and ref_prim has no composed
        # type, so drop the entry instead of authoring onto a typeless prim.
        if not ref_prim.GetTypeName():
            ref_prim.GetReferences().ClearReferences()
            continue
        for vset_name, variant in proto.variants:
            vsets = ref_prim.GetVariantSets()
            if not vsets.HasVariantSet(vset_name):
                continue
            # A local selection on the referencing prim outranks whatever the asset itself
            # selects, which is exactly how one asset yields several coloured prototypes.
            vsets.GetVariantSet(vset_name).SetVariantSelection(variant)
        UsdGeom.Xformable(ref_prim).GetXformOpOrderAttr().Set([])

        proto.path = path
        _measure_proto(proto, ref_prim, src)
        resolved.append(proto)

    if not resolved:
        path = f"{proto_scope}/proto_0"
        cube = UsdGeom.Cube.Define(stage, path)
        cube.GetSizeAttr().Set(1.0)
        fallback = _Proto(label="cube (default)", path=path)
        resolved.append(fallback)
    return resolved


def _measure_proto(proto, proto_prim, src_prim):
    """Fill in proto.bmin/bmax (local, variant-aware) and proto.scale (source world scale)."""
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    rng = bbox_cache.ComputeUntransformedBound(proto_prim).ComputeAlignedRange()
    if not rng.IsEmpty():
        mn, mx = rng.GetMin(), rng.GetMax()
        proto.bmin = Gf.Vec3f(mn[0], mn[1], mn[2])
        proto.bmax = Gf.Vec3f(mx[0], mx[1], mx[2])

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    wm = xform_cache.GetLocalToWorldTransform(src_prim)
    r0, r1, r2 = wm.GetRow3(0), wm.GetRow3(1), wm.GetRow3(2)
    proto.scale = Gf.Vec3f(
        (r0[0] ** 2 + r1[0] ** 2 + r2[0] ** 2) ** 0.5,
        (r0[1] ** 2 + r1[1] ** 2 + r2[1] ** 2) ** 0.5,
        (r0[2] ** 2 + r1[2] ** 2 + r2[2] ** 2) ** 0.5,
    )


def _quota(weights, total):
    """Largest-remainder apportionment: exact per-prototype counts, not a statistical draw.

    30% of 10 instances is 3 of them, every cook, which is what a "percent of the mix"
    control has to mean to be usable.
    """
    if total <= 0 or not weights:
        return [0] * len(weights)
    sum_w = sum(weights)
    if sum_w <= 0:
        base = [total // len(weights)] * len(weights)
        for i in range(total - sum(base)):
            base[i] += 1
        return base
    exact = [w / sum_w * total for w in weights]
    counts = [int(math.floor(e)) for e in exact]
    remainder = total - sum(counts)
    order = sorted(range(len(weights)), key=lambda i: exact[i] - counts[i], reverse=True)
    for i in range(remainder):
        counts[order[i % len(order)]] += 1
    return counts


# ---------------------------------------------------------------------------
# stack (lattice) placement
# ---------------------------------------------------------------------------


def _stack_sites(triangles, up_idx, world_up, cfg, rng):
    """Lattice sites packed over the target, level by level.

    Returns [(point, normal, level)] in fill order — a whole level before the next one
    starts, so a Max Count that doesn't cover the lattice gives a complete single layer
    rather than a lopsided tower.
    """
    bounds = _triangle_bounds(triangles)
    axis_a, axis_b = [i for i in range(3) if i != up_idx]
    pitch_a = max(cfg["pitch_a"], 1e-4)
    pitch_b = max(cfg["pitch_b"], 1e-4)
    # Hex rows sit in each other's grooves: √3/2 row spacing with a half-pitch offset is the
    # densest arrangement of equal circles, ~15% more instances than a square grid.
    hexagonal = cfg["pattern"] == "Hex"
    row_step = pitch_b * (0.8660254 if hexagonal else 1.0)

    inset = cfg["inset"]
    if bounds:
        bmin, bmax = bounds
        lo_a, hi_a = bmin[axis_a] + inset, bmax[axis_a] - inset
        lo_b, hi_b = bmin[axis_b] + inset, bmax[axis_b] - inset
        top = bmax[up_idx]
    else:
        # Free-standing: no surface, so the block stands on its own at the origin.
        #
        # Wanted for "pencils into a cup", which nothing here does well: Volume fills a closed
        # mesh's *solid* (a cup's is its ceramic, so pencils land in the wall) and Stack needs
        # something to pack against. The answer is not a cleverer sampler -- it is to stack the
        # block in mid-air, tilt it, move it over the cup with a Transform and let the RBD
        # solver drop it in. This makes that possible without modelling a throwaway plane to
        # stack on. An empty target used to yield nothing at all, so no scene loses behaviour.
        per_level = max(1, -(-cfg["free_count"] // max(1, cfg["levels"])))
        side = max(1, math.ceil(math.sqrt(per_level)))
        lo_a, hi_a = -(side - 1) * pitch_a * 0.5, (side - 1) * pitch_a * 0.5
        lo_b, hi_b = -(side - 1) * row_step * 0.5, (side - 1) * row_step * 0.5
        top = 0.0
    if hi_a < lo_a or hi_b < lo_b:
        return []

    span_a, span_b = hi_a - lo_a, hi_b - lo_b
    cols = max(1, int(span_a / pitch_a) + 1)
    rows = max(1, int(span_b / row_step) + 1)
    centre_a = (lo_a + hi_a) * 0.5
    centre_b = (lo_b + hi_b) * 0.5
    start_a = centre_a - (cols - 1) * pitch_a * 0.5
    start_b = centre_b - (rows - 1) * row_step * 0.5

    # Nothing to conform to when free-standing, whatever the toggle says.
    grid = _SurfaceRayGrid(triangles, up_idx) if (cfg["conform"] and triangles) else None

    # Jitter is clamped to the slack between pitch and footprint, so "natural" never means
    # "intersecting".
    jit_a = max(0.0, pitch_a - cfg["footprint_a"]) * 0.5 * cfg["jitter"]
    jit_b = max(0.0, pitch_b - cfg["footprint_b"]) * 0.5 * cfg["jitter"]

    base_sites = []
    for row in range(rows):
        for col in range(cols):
            a = start_a + col * pitch_a
            b = start_b + row * row_step
            if hexagonal and row % 2:
                a += pitch_a * 0.5
                if a > hi_a:
                    continue
            if jit_a:
                a += rng.uniform(-jit_a, jit_a)
            if jit_b:
                b += rng.uniform(-jit_b, jit_b)
            if not (lo_a - 1e-6 <= a <= hi_a + 1e-6 and lo_b - 1e-6 <= b <= hi_b + 1e-6):
                continue
            if grid is not None:
                hit = grid.hit(a, b, world_up)
                if hit is None:
                    continue  # no surface under this lattice point: respect the silhouette
                base_sites.append((hit[0], hit[1]))
            else:
                pt = [0.0, 0.0, 0.0]
                pt[axis_a] = a
                pt[axis_b] = b
                pt[up_idx] = top
                base_sites.append((Gf.Vec3f(*pt), world_up))

    sites = []
    stagger = cfg["stagger"]
    for level in range(max(1, cfg["levels"])):
        for point, normal in base_sites:
            p = point
            if level and stagger:
                # Offset alternate tiers by half a pitch so they nest instead of balancing
                # one directly on top of the other.
                off = [0.0, 0.0, 0.0]
                off[axis_a] = pitch_a * 0.5 * (level % 2)
                off[axis_b] = row_step * 0.5 * (level % 2)
                p = p + Gf.Vec3f(*off)
            sites.append((p, normal, level))
    return sites


# ---------------------------------------------------------------------------
# ui reactions
# ---------------------------------------------------------------------------


def _set_hidden(obj, hidden):
    if obj is None:
        return
    if hidden:
        obj.set_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        obj.unset_state(chunk.ChunkFlags.State.HIDDEN)


def _on_mode_changed(owner, notice, sender):
    """Only show the controls that mean something for the current mode."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    mode = owner.value
    _set_hidden(chunk_obj.get_param("Region"), mode != "Surface")
    _set_hidden(chunk_obj.get_param("Stacking"), mode != "Stack")

    dist = chunk_obj.get_param("Distribution")
    if dist:
        # Stack derives its own pitch from the prototype bounds; Poisson spacing plays no
        # part in it.
        _set_hidden(dist.get_param("auto_spacing"), mode == "Stack")
        _set_hidden(dist.get_param("spacing"), mode == "Stack")
    var = chunk_obj.get_param("Variation")
    if var:
        _set_hidden(var.get_param("orient_mode"), mode == "Stack")
        _set_hidden(var.get_param("sit_on_surface"), mode == "Stack")

    # Last word to the painted-points rule. The two systems overlap on auto_spacing/spacing,
    # and this one had been unhiding them: outside Stack they mean something *to the sampler*,
    # which is true right up until the positions are handed to us and there is no sampler.
    # "Wired to a brush" is the stronger statement, so it is re-applied after the mode's.
    _update_sampling_params(chunk_obj)


def _on_stack_orient_changed(owner, notice, sender):
    """Lay Along Rows is the only stack orientation with a row direction to choose."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    grp = chunk_obj.get_param("Stacking")
    _set_hidden(grp.get_param("stack_row_axis") if grp else None, owner.value != "Lay Along Rows")


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _migrate_legacy_proto(node):
    """Move a pre-list ``proto_prim`` value into row 0 of the Prototypes list.

    Scenes saved before the list existed carry inputs:proto_prim. Migrating on the first
    cook means an old file opens with its prototype visible in the new UI instead of
    silently scattering default cubes.
    """
    legacy = node.get_param("proto_prim")
    if legacy is None or not (legacy.value or "").strip():
        return
    rows = node.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return
    if rows.size < 1:
        rows.size = 1
    first = rows.get_indexed_param(rows.get_param("proto_path"), 0)
    if first is not None and not (first.value or "").strip():
        first.value = legacy.value
    legacy.value = ""


def execute(node):
    """Scatter prototype instances onto/into/over the surface.

    Reactive: runs automatically whenever a param or an upstream dependency (e.g. a
    Transform feeding surface_prim) changes. Being reactive keeps things correct across an
    ordered DAG solve pass triggered by an *upstream* change (a chunk wired after this one is
    never part of that pass yet) -- but a plain edit made directly on this chunk's own params
    never goes through that ordered pass at all, so the surface and prototype prims are
    resolved through get_upstream_stage(): a downstream chunk's edit to the same prim (e.g. a
    Transform wired after Scatter, re-offsetting the same mesh) can't leak into this read
    regardless of what triggered this execute().
    """
    stage = node.get_stage()
    if not stage:
        return

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    surface_paths = node.get_param("surface_prim").resolve_paths()

    # `.value` is already typed and defaults when unset — no cast/`or` needed. inst_path
    # keeps its fallback because an empty path (user cleared the field) is invalid here.
    inst_path = node.get_param("inst_path", is_s).value or "/ScatterInstancer"
    scatter_mode = node.get_param("scatter_mode", is_s).value
    max_count = max(1, int(node.get_param("max_count", is_i).value))
    seed = int(node.get_param("seed", is_i).value)
    auto_spacing = node.get_param("auto_spacing", is_b).value
    spacing = node.get_param("spacing", is_f).value
    max_slope = node.get_param("max_slope", is_f).value
    scale_min = node.get_param("scale_min", is_f).value
    scale_max = node.get_param("scale_max", is_f).value
    if scale_max < scale_min:
        scale_min, scale_max = scale_max, scale_min
    scale_bias = max(0.05, node.get_param("scale_bias", is_f).value)
    size_spacing = node.get_param("size_spacing", is_b).value
    avoid_paths = [p.strip() for p in node.get_param("avoid_prims", is_s).value.split(",") if p.strip()]
    avoid_distance = max(0.0, node.get_param("avoid_distance", is_f).value)
    normal_offset = node.get_param("normal_offset", is_f).value
    orient_mode = node.get_param("orient_mode", is_s).value
    sit_on_surface = node.get_param("sit_on_surface", is_b).value
    rand_axis = node.get_param("rand_axis", is_s).value
    rand_amount = node.get_param("rand_amount", is_f).value

    stack_orient = node.get_param("stack_orient", is_s).value
    stack_row_axis = node.get_param("stack_row_axis", is_s).value
    pack_pattern = node.get_param("pack_pattern", is_s).value
    pack_padding = node.get_param("pack_padding", is_f).value
    pack_row_padding = node.get_param("pack_row_padding", is_f).value
    pack_inset = node.get_param("pack_inset", is_f).value
    pack_levels = max(1, int(node.get_param("pack_levels", is_i).value))
    pack_level_gap = node.get_param("pack_level_gap", is_f).value
    pack_stagger = node.get_param("pack_stagger", is_b).value
    pack_conform = node.get_param("pack_conform", is_b).value
    pack_jitter = node.get_param("pack_jitter", is_f).value
    pack_tilt = node.get_param("pack_tilt", is_f).value

    layer = node.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Erase stale positions/orientations from other layers (e.g. a solver bake),
    # or their old N-item arrays win over scatter's new M-item arrays and break
    # the PointInstancer (mismatched protoIndices vs positions).
    _inst_sdf = Sdf.Path(inst_path)
    for _lyr in stage.GetLayerStack():
        if _lyr.identifier == layer.identifier:
            continue
        for _prop in ("positions", "orientations", "scales"):
            _pp = _inst_sdf.AppendProperty(_prop)
            for _t in list(_lyr.ListTimeSamplesForPath(_pp)):
                _lyr.EraseTimeSample(_pp, _t)
            if _spec := _lyr.GetAttributeAtPath(_pp):
                _spec.ClearDefaultValue()

    scratch_stage = chunk_stage.get_upstream_stage(stage, node.get_scene(), node)
    surface_prim = scratch_stage.GetPrimAtPath(surface_paths[0]) if surface_paths else None

    _migrate_legacy_proto(node)
    specs = _resolve_prototypes(node, scratch_stage)

    triangles = _collect_triangles(surface_prim)
    # None whenever the surface carries no painted weight, so an unpainted scatter never pays
    # for the lookup -- see _density_mask.
    density = _density_mask(surface_prim, node.get_param("density_primvar").value)
    rng = random.Random(seed)
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    world_up = Gf.Vec3f(0, 1, 0) if up_idx == 1 else Gf.Vec3f(0, 0, 1)
    lat_a, lat_b = [i for i in range(3) if i != up_idx]
    axis_vec = [Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0), Gf.Vec3f(0, 0, 1)]

    # Read here, used at the dispatch below. Nothing about a painted cloud depends on the
    # prototypes, so it does not need to wait for them.
    points_input = node.get_param("points_prim").value
    placed, placed_normals = _painted_points(scratch_stage, points_input)
    sizes = None  # scales the sampler already drew, when spacing follows scale

    with Usd.EditContext(stage, layer):
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)
        protos = _author_prototypes(stage, inst_path, specs, scratch_stage)
        instancer.GetPrototypesRel().SetTargets([Sdf.Path(p.path) for p in protos])

        spins = rand_axis != "Off" and abs(rand_amount) > 1e-6

        # Base orientation per prototype, when the mode gives one that doesn't depend on the
        # point (everything except Align to Normal / Random / Long Axis to Normal). The pitch
        # and the resting offset are both derived from it.
        row_idx = "XYZ".find(stack_row_axis)
        row_dir = axis_vec[row_idx if row_idx in (lat_a, lat_b) else lat_a]
        base_quats = []
        for proto in protos:
            if scatter_mode == "Stack":
                if stack_orient == "Stand Up":
                    q = _align_quat(proto.long_axis_dir(), world_up)
                elif stack_orient == "Lay Along Rows":
                    q = _align_quat(proto.long_axis_dir(), row_dir)
                else:
                    q = Gf.Quatf(1, 0, 0, 0)
            else:
                q = _compute_orientation(orient_mode, world_up, random.Random(seed), proto, world_up)
            base_quats.append(q)

        # Footprint: the widest any prototype gets, laterally, at its largest scale. Under a
        # random spin the safe figure is the diagonal of that footprint, since the instance
        # can end up turned any way within the lattice cell.
        foot_a = foot_b = 0.0
        height = 0.0
        for proto, q in zip(protos, base_quats):
            corners = proto.corners(scale_max * proto.scale_mult)
            w, d = _lateral_size(corners, q, axis_vec[lat_a], axis_vec[lat_b])
            if spins or orient_mode == "Random":
                diag = math.hypot(w, d)
                if orient_mode == "Random":
                    lo_u, hi_u = _extent_along(corners, q, world_up)
                    diag = math.sqrt(w * w + d * d + (hi_u - lo_u) ** 2)
                w = d = diag
            foot_a = max(foot_a, w)
            foot_b = max(foot_b, d)
            lo_u, hi_u = _extent_along(corners, q, world_up)
            height = max(height, hi_u - lo_u)

        if auto_spacing and scatter_mode != "Stack":
            # Centre-to-centre >= the largest instance's footprint -> no overlap.
            spacing = max(foot_a, foot_b, 1e-6)

        if scatter_mode != "Stack" and points_input:
            # Hand-placed: the user said where. No sampling, no spacing, no slope filter --
            # those answer "where should these go", which is the question already settled
            # by placing them. The density field is ignored for the same reason.
            #
            # Empty is an answer too: a brush wired up but not yet painted means "nowhere",
            # not "anywhere". The *wire* decides, not whether the brush has points yet.
            #
            # Stack is checked first and so ignores a painted cloud entirely: packing a
            # lattice against the surface and placing points by hand are contradictory
            # answers to the same question, and the lattice is what the mode exists for.
            positions, normals = placed, placed_normals
            levels = [0] * len(positions)
        elif scatter_mode == "Stack":
            cfg = {
                "pitch_a": foot_a + pack_padding,
                "pitch_b": foot_b + pack_row_padding,
                "footprint_a": foot_a,
                "footprint_b": foot_b,
                "pattern": pack_pattern,
                "inset": pack_inset,
                "conform": pack_conform,
                "levels": pack_levels,
                "stagger": pack_stagger,
                "jitter": max(0.0, min(1.0, pack_jitter)),
                "free_count": max_count,
            }
            sites = _stack_sites(triangles, up_idx, world_up, cfg, rng)[:max_count]
            positions = [s[0] for s in sites]
            normals = [s[1] for s in sites]
            levels = [s[2] for s in sites]
        elif scatter_mode == "Volume":
            positions, normals = _fill_in_volume(triangles, max_count, rng, spacing, mask=density)
            normals = [world_up] * len(positions)
            levels = [0] * len(positions)
        else:
            # Surface — slope pre-filters the triangle set (correct area weighting,
            # no wasted samples). The paint mask, being per-point, will instead be
            # passed as the `mask=` hook above.
            surf_tris = triangles.select(triangles.slope_mask(world_up, max_slope))
            avoid = None
            if avoid_paths and avoid_distance > 0:
                avoid = _AvoidSet(_avoid_points(scratch_stage, avoid_paths, avoid_distance * 0.5), avoid_distance)
            if len(surf_tris) and (size_spacing or avoid is not None):
                # Spacing Follows Scale / Keep Away From: the scale is drawn with the point,
                # so the spacing and the clearance can both take the instance's size.
                positions, normals, drawn = _scatter_sized(
                    surf_tris, max_count, rng, spacing, random.Random(seed + 13),
                    scale_min, scale_max, scale_bias, mask=density, avoid=avoid, vary=size_spacing)
                sizes = drawn
            elif len(surf_tris):
                positions, normals = _scatter_on_triangles(surf_tris, max_count, rng, spacing, mask=density)
            else:
                # No mesh / no qualifying faces — fall back to a jittered grid.
                sp = max(spacing, 2.0)
                n = int(math.sqrt(max_count))
                off = (n - 1) * sp / 2
                positions = [
                    Gf.Vec3f(
                        x * sp - off + rng.uniform(-sp * 0.3, sp * 0.3),
                        y * sp - off + rng.uniform(-sp * 0.3, sp * 0.3),
                        0,
                    )
                    for x in range(n)
                    for y in range(n)
                ]
                normals = [Gf.Vec3f(0, 0, 1)] * len(positions)
            levels = [0] * len(positions)

        count = len(positions)

        # Exact mix: quota per prototype, then shuffled so the shares are spread over the
        # scatter instead of arriving in blocks.
        quota = _quota([p.weight for p in protos], count)
        assignment = [i for i, n in enumerate(quota) for _ in range(n)]
        assignment += [0] * (count - len(assignment))
        random.Random(seed + 31).shuffle(assignment)

        scale_rng = random.Random(seed + 13)
        orient_rng = random.Random(seed + 7)
        tilt_rng = random.Random(seed + 19)

        out_positions = Vt.Vec3fArray(count)
        out_scales = Vt.Vec3fArray(count)
        out_orients = Vt.QuathArray(count)
        out_indices = Vt.IntArray(count)

        level_step = height + pack_level_gap

        for i in range(count):
            pidx = assignment[i]
            proto = protos[pidx]
            if placed:
                # Seeded per point when the points were placed by hand, so a point's look is
                # tied to the point rather than to its position in the list.
                #
                # Appending alone would survive the sequential rng too -- consuming one more
                # value leaves the earlier ones untouched, which is worth stating because it
                # is easy to assume otherwise. What does not survive it is *editing*: delete
                # a point from the middle, or reorder them, and every point after it silently
                # changes scale and rotation. Painting is an editing workflow, so the look is
                # pinned to the index.
                scale_rng = random.Random(seed + 13 + i)
                orient_rng = random.Random(seed + 7 + i)
            s = sizes[i] if sizes else _draw_scale(scale_rng, scale_min, scale_max, scale_bias)
            nrm = normals[i]

            if scatter_mode == "Stack":
                quat = base_quats[pidx]
                if pack_tilt > 1e-6:
                    # Lean about a random lateral axis — a pile that isn't machine-perfect.
                    tilt_axis = Gf.Cross(world_up, _random_lateral(tilt_rng, lat_a, lat_b, axis_vec))
                    quat = _quat_mul(_axis_quat(tilt_axis, math.radians(tilt_rng.uniform(0.0, pack_tilt))), quat)
            else:
                quat = _compute_orientation(orient_mode, nrm, orient_rng, proto, world_up)
            quat = _apply_random_rotation(quat, rand_axis, rand_amount, orient_rng)

            pos = positions[i]
            if scatter_mode == "Stack":
                # Rest the instance's own bounding box on the point it landed on, whatever
                # its pivot happens to be, then lift it by its level. Recomputed from the
                # final orientation so a tilted instance still touches rather than sinks.
                lo_u, _ = _extent_along(proto.corners(s * proto.scale_mult), quat, world_up)
                lift = -lo_u + levels[i] * level_step
                pos = pos + world_up * lift
            elif sit_on_surface:
                lo_n, _ = _extent_along(proto.corners(s * proto.scale_mult), quat, nrm)
                pos = pos + Gf.Vec3f(nrm[0], nrm[1], nrm[2]) * -lo_n

            if abs(normal_offset) > 1e-9:
                pos = pos + Gf.Vec3f(nrm[0] * normal_offset, nrm[1] * normal_offset, nrm[2] * normal_offset)

            out_positions[i] = pos
            out_indices[i] = pidx
            out_orients[i] = _to_quath(quat)
            sm = s * proto.scale_mult
            out_scales[i] = Gf.Vec3f(sm * proto.scale[0], sm * proto.scale[1], sm * proto.scale[2])

        # Authored even when nothing was placed. Skipping the write left an instancer that
        # declares prototypes while carrying no protoIndices and no positions -- USD warns
        # ("no prototype indices") and Hydra then draws prototypes it has no way to index
        # into, which crashes usdview outright on a USD 26.05 build. 25.08 tolerates it,
        # so it only showed up on the newer bundle.
        #
        # Nothing placed is a real and reachable state: wiring painted points in before a
        # surface mesh is set puts the graph there on the way to a working setup, which is
        # exactly how it was hit. An empty instancer is coherent -- zero instances of the
        # prototypes it declares -- where a half-declared one is not.
        instancer.GetPositionsAttr().Set(out_positions)
        instancer.GetProtoIndicesAttr().Set(out_indices)
        instancer.GetScalesAttr().Set(out_scales)
        instancer.GetOrientationsAttr().Set(out_orients)

    _report(node, protos, quota, count, scatter_mode, pack_levels)


def _random_lateral(rng, lat_a, lat_b, axis_vec):
    """A random unit direction in the ground plane (for tilt axes)."""
    ang = rng.uniform(0.0, 2.0 * math.pi)
    return axis_vec[lat_a] * math.cos(ang) + axis_vec[lat_b] * math.sin(ang)


def _report(node, protos, quota, count, mode, levels):
    """Fill the readout label + the output params other chunks can read."""
    if count and protos:
        mix = "  ".join(
            f"{p.label} {q * 100.0 / count:.0f}% ({q})" for p, q in zip(protos, quota) if q
        )
    else:
        mix = "nothing placed — check the surface mesh and prototype paths"
    suffix = f", {levels} levels" if mode == "Stack" and levels > 1 else ""
    text = f"{count} instances · {mode}{suffix}\n{mix}"

    if readout := node.get_param("readout"):
        readout.value = text
    if out_count := node.get_param("out_count"):
        out_count.value = count
    if out_protos := node.get_param("out_prototypes"):
        out_protos.value = len(protos)

    logger.debug("%s", text.replace(chr(10), " | "))


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------

_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def _collapsed(group):
    """Collapse a group by default — the chunk is tall, so only the target is open."""
    group.set_state(chunk.ChunkFlags.State.COLLAPSED)
    return group


def main():
    chunk_api = chunk.Chunk("Scatter Points")

    # --- Target ---
    chunk_api.add_param(
        chunk.LineEdit(
            "surface_prim",
            "Surface Mesh",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="Mesh to scatter onto (Surface), fill (Volume) or pack against (Stack).",
        )
    )
    points_input = chunk.LineEdit(
        "points_prim",
        "Painted Points",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Place one instance at each point a Scatter Brush painted, instead of "
        "scattering. Wire this from the brush's Painted Points output. While it is "
        "connected, the distribution settings below do nothing and are hidden.",
    )
    points_input.add_callback(_on_points_input_changed)
    chunk_api.add_param(points_input)
    chunk_api.add_param(
        chunk.LineEdit(
            "density_primvar",
            "Density Field",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Which painted field decides where instances may land. Wire this from a "
            "Scatter Brush's Density Field output. Empty means no field — scatter everywhere.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "inst_path",
            "Instancer Path",
            default_value="/ScatterInstancer",
            tooltip="USD path where the PointInstancer is created. Feed this to the RBD Solver "
            "to settle the instances.",
        )
    )

    # --- Prototypes (the list) ---
    protos = chunk.ListGroup(
        "prototypes",
        "Prototypes",
        tooltip="One row per prototype. Mix % is relative — the rows are normalised, so "
        "75/25 and 3/1 mean the same thing.",
    )
    protos.add_param(
        chunk.LineEdit(
            "proto_path",
            "Prim",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="Prim to instance. Globs work (/props/crayon_*), and a variant selection "
            "can be written into the path:\n"
            "  /props/crayon{color=red}   one variant\n"
            "  /props/crayon{color=*}     one prototype per variant in the set,\n"
            "                             sharing this row's Mix %.",
        )
    )
    protos.add_param(
        chunk.Slider(
            "proto_weight",
            "Mix %",
            default_value=100.0,
            min_value=0.0,
            max_value=100.0,
            flags=_PF.LABELED,
            tooltip="This prototype's share of the instances, relative to the other rows. "
            "0 skips it. Counts are exact, not statistical: 25% of 100 is 25.",
        )
    )
    protos.add_param(
        chunk.Slider(
            "proto_scale",
            "Scale",
            default_value=1.0,
            min_value=0.01,
            max_value=10.0,
            flags=_PF.LABELED,
            tooltip="Extra scale for this prototype only, on top of the random Scale Min/Max.",
        )
    )
    protos.size = 1
    chunk_api.add_param(protos)

    # Legacy single-prototype field: hidden, kept so scenes saved before the list still
    # resolve their prototype. execute() migrates the value into row 0 on the first cook.
    legacy = chunk.LineEdit(
        "proto_prim",
        "Prototype Prim (legacy)",
        flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Superseded by the Prototypes list; migrated automatically.",
    )
    legacy.set_state(chunk.ChunkFlags.State.HIDDEN)
    chunk_api.add_param(legacy)

    chunk_api.add_param(
        chunk.Label(
            "readout",
            default_value="no scatter yet",
            tooltip="What the last cook produced, and how the mix actually landed.",
        )
    )

    # --- Distribution ---
    dist_grp = chunk.Group("Distribution", flags=_GF)
    mode_param = chunk.Menu(
        "scatter_mode",
        "Mode",
        menu_items=_SCATTER_MODES,
        default_value="Surface",
        tooltip="Surface: place on faces.\n"
        "Volume: fill the SOLID interior of a closed mesh — the material it is made of. An "
        "open container's cavity is not its interior, so filling a cup this way puts "
        "instances inside the ceramic. To fill a cup or a bowl, stack above it and drop them "
        "in with the RBD Solver.\n"
        "Stack: pack a regular lattice against the target, resting on it — tight but never "
        "intersecting, and stackable in levels. With no Target Prim set it packs a "
        "free-standing block at the origin, to move into place with a Transform and settle "
        "with the RBD Solver.",
    )
    mode_param.add_callback(_on_mode_changed)
    dist_grp.add_param(mode_param)
    dist_grp.add_param(
        chunk.Checkbox(
            "auto_spacing",
            "Auto Spacing",
            default_value=True,
            tooltip="Derive spacing from the widest prototype's footprint so instances don't "
            "overlap. Turn off to set Spacing manually.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "spacing",
            "Spacing",
            default_value=10.0,
            tooltip="Minimum distance between instances, in scene units (used when Auto Spacing "
            "is off). 0 = allow overlap.",
        )
    )
    dist_grp.add_param(
        chunk.Checkbox(
            "size_spacing",
            "Spacing Follows Scale",
            default_value=False,
            tooltip="Surface mode: each instance keeps Spacing x its scale / Scale Max from the "
            "others, so small ones fill in between big ones without touching them.",
        )
    )
    dist_grp.add_param(
        chunk.LineEdit(
            "avoid_prims",
            "Keep Away From",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Surface mode: prims whose points (curves along their length) instances "
            "stay clear of -- e.g. a Trails output. Comma-separate several.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "avoid_distance",
            "Keep Away Distance",
            default_value=0.0,
            tooltip="How far from Keep Away From an instance's edge must stay. 0 = off.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "max_count",
            "Max Count",
            default_value=200,
            is_int=True,
            tooltip="Upper limit on instances. Spacing (or the lattice) may place fewer if the "
            "surface fills up first.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "seed",
            "Seed",
            default_value=0,
            is_int=True,
            tooltip="Random seed — change for a different pattern with the same settings.",
        )
    )
    chunk_api.add_param(_collapsed(dist_grp))

    # --- Stacking (Stack mode only) ---
    stack_grp = chunk.Group("Stacking", flags=_GF)
    orient_param = chunk.Menu(
        "stack_orient",
        "Orientation",
        menu_items=_STACK_ORIENTS,
        default_value="Stand Up",
        tooltip="Stand Up: turn the prototype's longest axis to world up — crayons in a cup.\n"
        "Lay Along Rows: lay it down along the row direction — a woodpile.\n"
        "Keep Original: leave the prototype as authored.",
    )
    orient_param.add_callback(_on_stack_orient_changed)
    stack_grp.add_param(orient_param)
    row_axis = chunk.Menu(
        "stack_row_axis",
        "Row Axis",
        menu_items=["X", "Y", "Z"],
        default_value="X",
        tooltip="Which axis the rows run along, for Lay Along Rows.",
    )
    row_axis.set_state(chunk.ChunkFlags.State.HIDDEN)
    stack_grp.add_param(row_axis)
    stack_grp.add_param(
        chunk.Menu(
            "pack_pattern",
            "Pattern",
            menu_items=_PACK_PATTERNS,
            default_value="Hex",
            tooltip="Hex nests every other row into the gaps — the tightest arrangement for "
            "round-ish prototypes. Grid keeps square rows and columns.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_padding",
            "Padding",
            default_value=0.0,
            tooltip="Extra gap between neighbours ALONG a row, in scene units. 0 packs them "
            "touching; the pitch already comes from the prototype's own oriented bounds, so 0 "
            "does not mean intersecting.\n"
            "Negative pulls them closer than their bounds suggest — the escape hatch for an "
            "asset whose bounding box is loose (a pencil modelled inside a roomy box, say), "
            "where 0 already leaves a visible gap.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_row_padding",
            "Row Padding",
            default_value=0.0,
            tooltip="The same, but BETWEEN rows rather than along them.\n"
            "Separate because a loose bounding box is usually only loose in one direction: a "
            "pack can sit tight along a row and leave a gap between rows, and one number for "
            "both axes cannot fix that without ruining the axis that was already right. "
            "Negative closes the rows up.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_inset",
            "Edge Inset",
            default_value=0.0,
            tooltip="Shrink the packed area inwards by this much, to keep instances from "
            "hanging over the rim.",
        )
    )
    stack_grp.add_param(
        chunk.Slider(
            "pack_levels",
            "Levels",
            default_value=1,
            min_value=1,
            max_value=32,
            is_int=True,
            tooltip="Stack this many tiers, each lifted by the tallest prototype's height.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_level_gap",
            "Level Gap",
            default_value=0.0,
            tooltip="Extra vertical gap between tiers, in scene units.",
        )
    )
    stack_grp.add_param(
        chunk.Checkbox(
            "pack_stagger",
            "Stagger Levels",
            default_value=True,
            tooltip="Offset alternate tiers by half a pitch so they nest into the layer below "
            "instead of balancing on it.",
        )
    )
    stack_grp.add_param(
        chunk.Checkbox(
            "pack_conform",
            "Conform to Surface",
            default_value=True,
            tooltip="Drop each lattice point onto the mesh below it, so instances follow the "
            "real surface and its outline. Off places them on the target's bounding-box top.",
        )
    )
    stack_grp.add_param(
        chunk.Slider(
            "pack_jitter",
            "Jitter",
            default_value=0.0,
            min_value=0.0,
            max_value=1.0,
            tooltip="Randomise position within the free space of each cell. 1 uses all of the "
            "slack between pitch and footprint — never enough to intersect.",
        )
    )
    stack_grp.add_param(
        chunk.Slider(
            "pack_tilt",
            "Tilt",
            default_value=0.0,
            min_value=0.0,
            max_value=45.0,
            tooltip="Random lean, in degrees. Instances still rest on the surface — the resting "
            "offset is recomputed from the tilted bounds.",
        )
    )
    chunk_api.add_param(_collapsed(stack_grp))
    stack_grp.set_state(chunk.ChunkFlags.State.HIDDEN)  # Surface is the default mode

    # --- Region (Surface only) ---
    region_grp = chunk.Group("Region", flags=_GF)
    region_grp.add_param(
        chunk.Spinbox(
            "max_slope",
            "Max Slope",
            default_value=90.0,
            tooltip="Only scatter on faces within this many degrees of facing up.\n"
            "90 = all upward faces, lower = flatter ground only, 180 = every face.",
        )
    )
    chunk_api.add_param(_collapsed(region_grp))

    # --- Variation ---
    var_grp = chunk.Group("Variation", flags=_GF)
    var_grp.add_param(
        chunk.Spinbox(
            "scale_min",
            "Scale Min",
            default_value=1.0,
            tooltip="Smallest random scale applied to an instance.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "scale_max",
            "Scale Max",
            default_value=1.0,
            tooltip="Largest random scale. Set Min and Max apart for natural size variation.",
        )
    )
    var_grp.add_param(
        chunk.Slider(
            "scale_bias",
            "Scale Bias",
            min_value=0.2,
            max_value=5.0,
            default_value=1.0,
            tooltip="1 spreads scales evenly between Min and Max. Higher: most instances small, "
            "a few large -- condensation, gravel. Lower: mostly large.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "normal_offset",
            "Normal Offset",
            default_value=0.0,
            tooltip="Push instances along the surface normal. Positive lifts them off the surface "
            "(handy before an RBD settle); negative sinks them in.",
        )
    )
    var_grp.add_param(
        chunk.Menu(
            "orient_mode",
            "Orientation Mode",
            menu_items=_ORIENT_MODES,
            default_value="Align to Normal",
            tooltip="Align to Normal: follow the surface.\n"
            "Up / Keep Original: use the prototype's orientation.\n"
            "Random: fully random.\n"
            "Long Axis Up: turn the prototype's longest axis to world up.\n"
            "Long Axis to Normal: same, but along the surface normal.",
        )
    )
    var_grp.add_param(
        chunk.Checkbox(
            "sit_on_surface",
            "Rest on Surface",
            default_value=False,
            tooltip="Offset each instance so the bottom of its bounding box touches the surface "
            "point, instead of its pivot. Fixes half-buried prototypes whose origin isn't at "
            "their base. Stack mode always does this.",
        )
    )
    var_grp.add_param(
        chunk.Menu(
            "rand_axis",
            "Random Rotation Axis",
            menu_items=_RAND_AXES,
            default_value="Off",
            tooltip="Add a random spin around this axis on top of the base orientation. Off "
            "disables it. With a spin on, spacing and pack pitch grow to the footprint's "
            "diagonal so a turned instance still can't overlap its neighbour.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "rand_amount",
            "Random Rotation (deg)",
            default_value=360.0,
            tooltip="Maximum random rotation in degrees (each instance gets a random angle between "
            "-this and +this).",
        )
    )
    chunk_api.add_param(_collapsed(var_grp))

    # --- Outputs: readouts of the last cook, not settings ---
    #
    # They read as two unexplained numbers at the bottom of the node, and being sliders they
    # look draggable, which invites you to set the instance count with one. They are results:
    # dragging does nothing, the next cook overwrites them. Disabled so the UI says so, and
    # named for the fact that they report rather than request.
    out_count = chunk.Slider(
        "out_count",
        "Instances Made",
        is_int=True,
        flags=_PF.OUTPUT,
        tooltip="Read-only. How many instances the last cook actually placed.\n"
        "Usually lower than Max Count -- spacing, the slope filter, a density mask or the "
        "size of the surface can all mean fewer fit than you asked for.\n"
        "Other chunks can read it as @Scatter Points.out_count@.",
    )
    out_protos = chunk.Slider(
        "out_prototypes",
        "Prototypes Used",
        is_int=True,
        flags=_PF.OUTPUT,
        tooltip="Read-only. How many prototypes the last cook resolved from the list.\n"
        "Higher than the number of rows when a glob (/props/crayon_*) or a variant "
        "wildcard ({color=*}) expanded one row into several; lower when a row's prim could "
        "not be found.\n"
        "Other chunks can read it as @Scatter Points.out_prototypes@.",
    )
    for _out in (out_count, out_protos):
        _out.set_state(chunk.ChunkFlags.State.DISABLED)
        chunk_api.add_param(_out)

    _update_sampling_params(chunk_api)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MODELLING,
        # The mesh maths runs on arrays -- see `_TriangleSoup` for what it replaced and why.
        requirements=["numpy>=2,<3"],
        description=(
            "Scatters instances of one or more prototypes across a mesh as a PointInstancer.\n"
            "\n"
            "Prototypes are a list: each row takes a prim (globs allowed), a share of the mix\n"
            "as a percentage, and its own scale multiplier. A variant selection can be written\n"
            "into the path -- /props/crayon{color=red} -- and {color=*} expands to one\n"
            "prototype per variant in that set, splitting the row's share between them. Mix\n"
            "percentages are exact quotas, not probabilities.\n"
            "\n"
            "Modes:\n"
            "  Surface  area-weighted sampling on the faces, Poisson-disk spacing.\n"
            "  Volume   uniform fill of a closed interior.\n"
            "  Stack    a hex or grid lattice packed against the target, each instance resting\n"
            "           on the surface under it and optionally stacked in levels. Pitch comes\n"
            "           from the prototype's oriented bounding box, so packing tight is\n"
            "           collision-free: a cup of crayons standing up, a woodpile, a tray of\n"
            "           bottles. Jitter and tilt stay inside the free space of each cell.\n"
            "\n"
            "Feed the instancer path to the RBD Solver to settle a loose scatter physically."
        ),
    )
