"""Scatter Points chunk -- distributes prototype instances across a mesh.

Three placement modes:

* **Surface** -- area-weighted sampling on the faces, Poisson-disk spacing to avoid overlap.
* **Volume** -- uniform fill of a closed interior.
* **Stack** -- a regular hex/grid lattice packed against the target, every instance resting
  exactly on the surface it lands on and optionally tiered into levels. This is the mode for
  "a cup of crayons standing up, tight but not intersecting": the pitch comes from the
  prototype's own oriented bounding box, so tight *is* collision-free by construction.

Prototypes are a list, each row carrying its own share of the mix (a percentage, normalised
across the rows), a scale multiplier, and optionally a variant selection written straight
into the path -- ``/asset/crayon{color=red}``. ``{color=*}`` expands to one prototype per
variant in that set, which is the cheap way to scatter a whole colour range from one row.
``%trees`` takes every member of the collection named trees, one prototype per member (a
member's own descendants are not prototypes of their own).

Physical settling (apples coming to rest in a bowl) is still the RBD Solver's job -- scatter
loosely, then settle. Stack mode is the deterministic alternative for when you want a tidy,
packed result with no simulation at all.

Region filtering goes through a single per-point mask hook, so a future paint gizmo can
supply a weight without touching the sampler/distribution/variation code.

Requires: pxr (OpenUSD), usd_chunks, numpy (bulk mesh maths -- see `_TriangleSoup`).
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from importlib.util import find_spec

import numpy as np
from pxr import Gf, Pcp, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils, kd_query, mesh_math, proximity
from usd_chunks import chunk_registry, chunk_stage

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
_PROXY_MODES = ["Off", "Bounds", "Full"]

# /asset/crayon{color=red} -- the variant value may be "*" (every variant in the set), which
# chunk_utils.split_variant_path's \w+ deliberately doesn't accept, hence the local regex.
_VARIANT_RE = re.compile(r"\{(\w+)=([\w*]+)\}")

_MAX_PROTOTYPES = 64
_FALLBACK_LABEL = "cube (default)"  # a wildcard row on a big variant set shouldn't author 500 prototypes


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
    areas -- and wired to a Scatter Brush, every stroke recooks.

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
        triangles, _, _ = mesh_math.fan_triangulate(counts, indices)
        if not len(triangles):
            continue
        fan = tuple(triangles.T)
        parts.append(_TriangleSoup.from_arrays(mesh_math.world_points(p, points), *fan))

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
    # (v0, v1, v2) and (v0, v2, v3).
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

    90 = every upward-facing face; 0 = only perfectly flat;
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


# _PoissonGrid's cell key is (x * _GRID_STRIDE + y) * _GRID_STRIDE + z, so a neighbour is the
# key plus a fixed offset: no tuple built per neighbour. Two far-apart cells can share a key,
# which only adds points to check; a neighbour is never missed, since the sum is exact.
_GRID_STRIDE = 1 << 32
_GRID_NEIGHBOURS = tuple(
    (dx * _GRID_STRIDE + dy) * _GRID_STRIDE + dz
    for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
)


class _PoissonGrid:
    """Spatial hash for O(1) min-distance rejection during sampling."""

    def __init__(self, min_dist):
        self._grid: dict = {}
        self._cell = max(min_dist, 1e-6)
        self._min_sq = min_dist * min_dist

    def _key(self, pt):
        c = self._cell
        return (math.floor(pt[0] / c) * _GRID_STRIDE + math.floor(pt[1] / c)) * _GRID_STRIDE + math.floor(pt[2] / c)

    def too_close(self, pt):
        min_sq = self._min_sq
        if min_sq <= 0:
            return False
        x, y, z = pt[0], pt[1], pt[2]
        key = self._key(pt)
        get = self._grid.get
        for offset in _GRID_NEIGHBOURS:
            cell = get(key + offset)
            if cell is not None:
                for ex, ey, ez in cell:
                    if (x - ex) ** 2 + (y - ey) ** 2 + (z - ez) ** 2 < min_sq:
                        return True
        return False

    def add(self, pt):
        self._grid.setdefault(self._key(pt), []).append((pt[0], pt[1], pt[2]))


class _AvoidSet:
    """Points to keep away from -- a curve's samples, another scatter -- on a spatial hash.

    Each point may carry a radius of its own (half a curve's width there), added to the
    distance: a wide run of water is kept clear edge to edge, not centre to centre.
    """

    def __init__(self, points, distance, radii=None, extra_max=0.0):
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        radii = np.zeros(len(points)) if radii is None else np.asarray(radii, dtype=np.float64)
        self._dist = distance + (float(radii.max()) if len(radii) else 0.0)
        self._base = distance
        # One cell spans the furthest a point can reach, so a query only ever looks one cell out.
        self._cell = max(self._dist + extra_max, 1e-6)
        self._points, self._radii = points, radii
        # Each built on first use: the grid by near(), the tree by near_many().
        self._grid: dict | None = None
        self._tree = None

    def _key(self, pt):
        c = self._cell
        return (math.floor(pt[0] / c), math.floor(pt[1] / c), math.floor(pt[2] / c))

    def near_many(self, pts, extra, kdtree):
        """near() for (N, 3) *pts*, each with its own *extra*, through a *kdtree* (scipy's
        cKDTree) of the points: the same test, as one query rather than a call per point."""
        out = np.zeros(len(pts), dtype=bool)
        if self._dist <= 0.0 or not len(self._points) or not len(pts):
            return out
        if self._tree is None:
            self._tree = kdtree(self._points)
        ext = np.maximum(extra, 0.0)
        reach = self._dist + ext
        # Every pair within the widest reach, as arrays (_within), then each point held to its
        # own below: a ball query per point built a Python list each, 19 of GrassValley's 85 s
        # on the woodland vista. A hair wider, so the query's own rounding can't drop a point
        # the exact test keeps.
        owner, flat = _within(self._tree, pts, float(reach.max()) * (1.0 + 1e-9))
        if not len(owner):
            return out
        q, e = pts[owner], self._points[flat]
        d2 = (q[:, 0] - e[:, 0]) ** 2 + (q[:, 1] - e[:, 1]) ** 2 + (q[:, 2] - e[:, 2]) ** 2
        lim = reach[owner] * reach[owner]
        bad = (d2 < lim) & (d2 < (self._base + self._radii[flat] + ext[owner]) ** 2)
        out[owner[bad]] = True
        return out

    def near(self, pt, extra=0.0):
        if self._dist <= 0.0:
            return False
        if self._grid is None:
            self._grid = {}
            for p, r in zip(self._points, self._radii):
                self._grid.setdefault(self._key(p), []).append((p, r))
        reach = self._dist + max(extra, 0.0)
        r = int(math.ceil(reach / self._cell))
        kx, ky, kz = self._key(pt)
        lim = reach * reach
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    for ex, er in self._grid.get((kx + dx, ky + dy, kz + dz), ()):
                        d2 = (pt[0] - ex[0]) ** 2 + (pt[1] - ex[1]) ** 2 + (pt[2] - ex[2]) ** 2
                        if d2 < lim and d2 < (self._base + er + max(extra, 0.0)) ** 2:
                            return True
        return False


def _avoid_points(stage, paths, step):
    """World-space points of every prim under *paths*, and a radius per point (half the
    curve's width there, 0 elsewhere); curves are sampled every *step* along."""
    out, out_r = [], []
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
            rad = np.zeros(len(pts))
            if prim.IsA(UsdGeom.Curves):
                curves = UsdGeom.Curves(prim)
                w = curves.GetWidthsAttr().Get()
                if w is None and curves.GetWidthsAttr().GetNumTimeSamples():
                    w = curves.GetWidthsAttr().Get(curves.GetWidthsAttr().GetTimeSamples()[-1])
                if w is not None and len(w) == len(pts):
                    rad = np.asarray(w, dtype=np.float64) * 0.5
                elif w is not None and len(w) == 1:
                    rad = np.full(len(pts), float(w[0]) * 0.5)
                counts = curves.GetCurveVertexCountsAttr().Get() or [len(pts)]
                dense, dense_r, k = [], [], 0
                for c in counts:
                    seg, seg_r = pts[k:k + c], rad[k:k + c]
                    k += c
                    dense.append(seg[:1])
                    dense_r.append(seg_r[:1])
                    for j in range(len(seg) - 1):
                        a, b = seg[j], seg[j + 1]
                        # A quarter of the curve's half-width apart is close enough that the
                        # discs' union is the curve's edge within ~3%. Without that floor a
                        # scatter with no Spacing and no distance sampled every half
                        # micrometre -- 370 million points along one road.
                        seg_step = max(step, 0.25 * float(min(seg_r[j], seg_r[j + 1])), 1e-9)
                        n = max(1, min(int(math.ceil(float(np.linalg.norm(b - a)) / seg_step)), 10_000))
                        t = np.arange(1, n + 1) / n
                        dense.append(a + (b - a) * t[:, None])
                        dense_r.append(seg_r[j] + (seg_r[j + 1] - seg_r[j]) * t)
                if dense:
                    pts, rad = np.concatenate(dense), np.concatenate(dense_r)
            m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
            out.append(pts @ m[:3, :3] + m[3, :3])
            out_r.append(rad)
    if not out:
        return np.zeros((0, 3)), np.zeros(0)
    return np.concatenate(out), np.concatenate(out_r)


def _draw_scale(rng, lo, hi, bias):
    """A random scale in lo..hi; *bias* above 1 favours the small end (1 = uniform)."""
    u = rng.random()
    return lo + (hi - lo) * (u ** bias if bias != 1.0 else u)


# The most candidates a spacing-free scatter draws in one round, over all its streams: about
# 0.4 GB of arrays.
_FREE_BATCH = 1 << 21
# The streams a spacing-free scatter is split into. Changing it changes every such layout.
_FREE_STREAMS = 8

def _scatter_sized(triangles, count, rng, spacing, scale_rng, scale_lo, scale_hi, bias,
                   mask=None, avoid=None, vary=True):
    """Like _scatter_on_triangles, but each candidate draws its scale first and keeps
    Spacing x scale / Scale Max from the others -- small instances pack in between big ones,
    and none overlap. Returns positions, normals and the scale each one drew.

    Candidates are drawn and tested in batches against a KD-tree of everything placed so far
    (scipy), then against each other. Testing them one at a time in Python against a hash grid
    was fine for hundreds and hopeless for condensation: a 26k-drop can took ten minutes, all
    of it in the neighbour walk once tiny drops were filling the gaps between big ones.
    """
    if not len(triangles):
        return [], [], []
    areas = triangles.areas()
    total_area = float(areas.sum())
    if total_area < 1e-10:
        return [], [], []
    try:
        from scipy.spatial import cKDTree
    except ImportError:  # scipy is optional: the same result, slowly
        cKDTree = None
    cdf = np.cumsum(areas) / total_area
    v0s, v1s, v2s = (np.asarray(a, dtype=np.float64) for a in (triangles.v0, triangles.v1, triangles.v2))
    normals_all = np.asarray(triangles.n, dtype=np.float64)
    # Radius per unit of scale -- or, with *vary* off, every instance keeps plain Spacing.
    unit = spacing / max(scale_hi, 1e-9) * 0.5 if vary else 0.0
    r_max = unit * scale_hi if vary else spacing * 0.5
    seed, scale_seed = rng.getrandbits(63), scale_rng.getrandbits(63)
    cell = max(2.0 * r_max, 1e-9)
    if r_max > 0 or cKDTree is None:
        positions, normals, scales = _sized_rounds(
            np.random.default_rng(seed), np.random.default_rng(scale_seed), count, 8192,
            cdf, v0s, v1s, v2s, normals_all, cKDTree, unit, r_max, vary, spacing, scale_lo,
            scale_hi, bias, mask, avoid, cell)
    else:
        # No spacing, so no candidate depends on another: the count is split between
        # _FREE_STREAMS streams, each with its own generators, run on threads -- numpy lets go
        # of the interpreter for the array work, which is nearly all of it. A fixed number of
        # streams, so the layout is the same on any machine; the meadow's 39 million blades
        # were 36 s on one core.
        _warm_lookups(mask, avoid, v0s[:1], cKDTree)
        shares = np.diff(np.linspace(0, count, _FREE_STREAMS + 1).astype(np.int64))
        seeds = zip(np.random.SeedSequence(seed).spawn(_FREE_STREAMS),
                    np.random.SeedSequence(scale_seed).spawn(_FREE_STREAMS))
        threads = min(_FREE_STREAMS, os.cpu_count() or 1)
        with ThreadPoolExecutor(threads, initializer=kd_query.single_thread) as pool:
            parts = list(pool.map(
                lambda job: _sized_rounds(
                    np.random.default_rng(job[1][0]), np.random.default_rng(job[1][1]),
                    int(job[0]), _FREE_BATCH // _FREE_STREAMS, cdf, v0s, v1s, v2s, normals_all,
                    cKDTree, unit, r_max, vary, spacing, scale_lo, scale_hi, bias, mask, avoid,
                    cell),
                zip(shares, seeds)))
        positions, normals, scales = ([x for part in parts for x in part[k]] for k in range(3))
    if not positions:
        return [], [], []
    # As arrays, converted to float32 once: a Gf.Vec3f built per instance was a second of a
    # 130k-instance verge, and the caller turns them back into an array anyway.
    return (Vt.Vec3fArray.FromNumpy(np.concatenate(positions).astype(np.float32)),
            Vt.Vec3fArray.FromNumpy(np.concatenate(normals).astype(np.float32)),
            np.concatenate(scales).tolist())


# Sampling: Tiles. A tile's side, in its largest instance's diameters: wide enough that the
# repeat doesn't read in a forest, small enough to fill in a second.
_TILE_DIAMETERS = 64
# Candidates thrown at a tile per smallest instance's disc of area: near enough to full that
# what is left are gaps no instance fits.
_TILE_FILL = 10.0
_TILE_MAX_CANDIDATES = 600_000
# Slope bands: a band per this much of the slope's cosine, down to the last (cos 0.4, ~66
# degrees); and the ways a slope can face, in sectors.
_TILE_BAND_STEP = 0.1
_TILE_BANDS = 7
_TILE_ASPECTS = 8
# Tiled points dropped onto the surface per block, a block per thread.
_TILE_BLOCK = 250_000
_tiles: dict = {}


_UNDECIDED, _KEPT, _REJECTED = 0, 1, 2


def _kept_in_draw_order(n, i, j):
    """Which of *n* candidates a darts fill keeps, as a bool array: taken in draw order (by
    index), each is kept unless an earlier candidate it clashes with was kept. *i* < *j* are
    the clashing pairs.

    The same answer as walking them one at a time, settled in rounds over every candidate at
    once: one whose earlier clashes are all rejected is kept, one with a kept earlier clash is
    rejected, and each round settles at least the earliest one still open. Walked one at a
    time it was a numpy call per candidate after an argsort of every pair -- 4.4 s of a 4.9 s
    tile for a birch forest's 0.8-1.6 scales.
    """
    state = np.zeros(n, dtype=np.int8)
    while len(j):
        before = state[i]
        blocked = np.bincount(j[before == _KEPT], minlength=n) > 0
        waiting = np.bincount(j[before == _UNDECIDED], minlength=n) > 0
        open_ = state == _UNDECIDED
        state[open_ & blocked] = _REJECTED
        state[open_ & ~blocked & ~waiting] = _KEPT
        # A pair matters while its later end is open and its earlier one is not rejected.
        live = (state[j] == _UNDECIDED) & (state[i] != _REJECTED)
        i, j = i[live], j[live]
    # Open with nothing left to wait on: no earlier clash was kept.
    return state != _REJECTED


#: Candidates per batch of _darts_fill: small enough that most of a batch meets the kept
#: points first, large enough that a batch's Python overhead does not add up.
_DARTS_BATCH = 4096


def _clashes(pts, rad, side, a, b):
    """Which of the pairs (*a*[k], *b*[k]) sit closer than their radii add up to, across the
    tile's edges too. One formula for every caller, so a pair is judged the same wherever."""
    diff = pts[b] - pts[a]
    diff -= side * np.round(diff / side)
    return (diff ** 2).sum(axis=1) < (rad[a] + rad[b]) ** 2


def _darts_fill(pts, rad, side, reach, cKDTree):
    """Which candidates a darts fill keeps, taken in draw order: each is kept if it clears
    every one kept before it by both radii. *reach* is the farthest any two can clash.

    Against the kept points rather than every candidate: the tile throws ~37 candidates for
    each it keeps, and finding every clashing pair among all of them was 16.7 million pairs
    to keep 5,663 (a birch forest's 0.8-1.6 scales). In batches, in draw order: a batch's
    candidates are tested against what earlier batches kept, and those that survive are
    settled among themselves (_kept_in_draw_order). The rule and the clash test are the
    same, so the tile is the same.

    The trees are periodic (boxsize), so the copies laid side by side meet as cleanly as the
    inside.
    """
    n = len(pts)
    kept = np.zeros(n, dtype=bool)
    kept_idx = np.empty(0, dtype=np.intp)
    for start in range(0, n, _DARTS_BATCH):
        batch = np.arange(start, min(start + _DARTS_BATCH, n))
        if len(kept_idx):
            near = cKDTree(pts[kept_idx], boxsize=side).sparse_distance_matrix(
                cKDTree(pts[batch], boxsize=side), reach, output_type="ndarray")
            if len(near):
                a, b = kept_idx[near["i"]], batch[near["j"]]
                hit = _clashes(pts, rad, side, a, b)
                batch = np.setdiff1d(batch, b[hit], assume_unique=False)
        if not len(batch):
            continue
        pairs = cKDTree(pts[batch], boxsize=side).query_pairs(reach, output_type="ndarray")
        if len(pairs):
            i, j = batch[pairs.min(axis=1)], batch[pairs.max(axis=1)]
            hit = _clashes(pts, rad, side, i, j)
            # Local indices, so the rounds work on this batch's survivors only.
            local = np.searchsorted(batch, np.stack([i[hit], j[hit]]))
            keep = _kept_in_draw_order(len(batch), local[0], local[1])
        else:
            keep = np.ones(len(batch), dtype=bool)
        kept[batch[keep]] = True
        kept_idx = np.flatnonzero(kept)
    return kept


def _blue_tile(seed, unit, r_lo, r_hi, scale_lo, scale_hi, bias, vary, spacing):
    """A square tile of points that repeats without a seam -- (side, (N, 2) points, scales,
    radii) -- filled the way a darts scatter fills a surface: candidates in random order, each
    kept if it clears every one kept before it by both radii. Built once per settings."""
    key = (seed, unit, r_lo, r_hi, scale_lo, scale_hi, bias, vary, spacing)
    if key in _tiles:
        return _tiles[key]
    from scipy.spatial import cKDTree
    side = _TILE_DIAMETERS * 2.0 * r_hi
    disc = math.pi * max(r_lo, 1e-6) ** 2
    if side * side * _TILE_FILL / disc > _TILE_MAX_CANDIDATES:
        side = math.sqrt(_TILE_MAX_CANDIDATES * disc / _TILE_FILL)
    gen = np.random.default_rng(seed)
    n = max(1, int(side * side * _TILE_FILL / disc))
    pts = gen.random((n, 2)) * side
    u = gen.random(n)
    sc = scale_lo + (scale_hi - scale_lo) * (u ** bias if bias != 1.0 else u)
    rad = unit * sc if vary else np.full(n, spacing * 0.5)
    kept = _darts_fill(pts, rad, side, 2.0 * r_hi * (1.0 + 1e-9), cKDTree)
    tile = (side, pts[kept], sc[kept], rad[kept])
    _tiles[key] = tile
    return tile


def _drop_onto(tree, v0s, v1s, v2s, ab, a, b, up_idx, k=8):
    """Each of (N, 2) lateral points *ab* dropped onto the highest triangle under it: (indices
    of those that land, their (M, 3) positions, their triangles). *tree* is a KD-tree of the
    triangles' lateral centres; a point's triangle is among its *k* nearest on a terrain."""
    k = min(k, len(v0s))
    _, near = kd_query.query(tree, ab, k=k)
    near = near.reshape(len(ab), k)
    p0, p1, p2 = v0s[near], v1s[near], v2s[near]
    e1, e2 = p1 - p0, p2 - p0
    q = ab[:, None, :] - p0[..., [a, b]]
    den = e1[..., a] * e2[..., b] - e1[..., b] * e2[..., a]
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (q[..., 0] * e2[..., b] - q[..., 1] * e2[..., a]) / den
        t = (e1[..., a] * q[..., 1] - e1[..., b] * q[..., 0]) / den
    eps = 1e-9
    inside = (np.abs(den) > 1e-12) & (s >= -eps) & (t >= -eps) & (s + t <= 1.0 + eps)
    height = np.where(inside, p0[..., up_idx] + s * e1[..., up_idx] + t * e2[..., up_idx], -np.inf)
    best = np.argmax(height, axis=1)
    rows = np.flatnonzero(inside[np.arange(len(ab)), best])
    pick = best[rows]
    s, t = s[rows, pick], t[rows, pick]
    pos = p0[rows, pick] + s[:, None] * e1[rows, pick] + t[:, None] * e2[rows, pick]
    return rows, pos, near[rows, pick]


def _scatter_tiles(triangles, count, rng, spacing, scale_rng, scale_lo, scale_hi, bias,
                   mask=None, avoid=None, vary=True, up_idx=2):
    """Sampling: Tiles. _scatter_sized's spacing rules, from a precomputed tile instead of
    darts: one blue-noise tile (_blue_tile) laid edge to edge across the surface's footprint,
    each point dropped onto the triangle under it, then thinned by the density, Keep Away
    From and the count. The tile is filled once; laying it is array work, in blocks on
    threads. A different layout from Darts, so opt-in.

    The surface is read from above (along *up_idx*): a terrain, not a cave -- a point lands on
    the highest face under it. Spacing is held across the ground, so on a slope instances
    stand at least that far apart along it too.
    """
    if not len(triangles):
        return [], [], []
    try:
        from scipy.spatial import cKDTree
    except ImportError:  # scipy is optional: without it, Darts' own slow path
        cKDTree = None
    unit = spacing / max(scale_hi, 1e-9) * 0.5 if vary else 0.0
    r_lo = unit * scale_lo if vary else spacing * 0.5
    r_hi = unit * scale_hi if vary else spacing * 0.5
    if r_hi <= 0 or cKDTree is None:
        return _scatter_sized(triangles, count, rng, spacing, scale_rng, scale_lo, scale_hi, bias,
                              mask=mask, avoid=avoid, vary=vary)
    tile_seed = rng.getrandbits(63)
    gen = np.random.default_rng(scale_rng.getrandbits(63))
    a, b = [i for i in range(3) if i != up_idx]
    v0s, v1s, v2s = (np.asarray(x, dtype=np.float64) for x in (triangles.v0, triangles.v1, triangles.v2))
    normals_all = np.asarray(triangles.n, dtype=np.float64)
    # Slope bands. Darts spaces instances along the surface, so a slope takes more of them per
    # metre of ground: seen from above, the same spacing is shorter along the fall line, by the
    # slope's cosine, and unchanged across it. So each band -- a range of slope and the way it
    # faces -- lays the one tile squashed along its fall line by its cosine: on the slope its
    # points then stand as far apart as the tile's do. Where bands meet, the 3D settle below
    # takes out the pairs that come too close.
    side, tile_ab, tile_sc, _ = _blue_tile(tile_seed, unit, r_lo, r_hi, scale_lo, scale_hi, bias, vary, spacing)
    up = np.zeros(3)
    up[up_idx] = 1.0
    length = np.maximum(np.linalg.norm(normals_all, axis=1), 1e-12)
    cos = np.clip(np.abs(normals_all @ up) / length, 0.0, 1.0)
    slope = np.minimum(np.floor((1.0 - cos) / _TILE_BAND_STEP).astype(np.int64), _TILE_BANDS - 1)
    # Which way it falls: the normal's lateral part, in _TILE_ASPECTS sectors (none when flat).
    aspect = np.round(np.arctan2(normals_all[:, b], normals_all[:, a]) / (2.0 * math.pi) * _TILE_ASPECTS)
    aspect = np.where(slope > 0, aspect.astype(np.int64) % _TILE_ASPECTS, 0)
    band_of = slope * _TILE_ASPECTS + aspect
    # The grid cells each band's triangles cover, so a band keeps only the tiled points near
    # its own triangles: on hilly ground every band is spread across the whole terrain, and
    # dropping every band's points over all of it was most of a valley's cook.
    lat = np.stack((v0s[:, [a, b]], v1s[:, [a, b]], v2s[:, [a, b]]))
    tri_lo, tri_hi = lat.min(axis=0), lat.max(axis=0)
    origin = tri_lo.min(axis=0)
    span = tri_hi.max(axis=0) - origin
    cell = max(float(np.median((tri_hi - tri_lo).max(axis=1))), float(span.max()) / 4096.0, 1e-6)
    first, last = np.floor((tri_lo - origin) / cell).astype(np.int64), np.floor((tri_hi - origin) / cell).astype(np.int64)
    wide = int(last[:, 1].max()) + 2
    per = (last[:, 0] - first[:, 0] + 1) * (last[:, 1] - first[:, 1] + 1)
    owner = np.repeat(np.arange(len(per)), per)
    within = np.arange(int(per.sum())) - np.repeat(np.cumsum(per) - per, per)
    rows_b = np.repeat(last[:, 1] - first[:, 1] + 1, per)
    covered = (np.repeat(first[:, 0], per) + within // rows_b) * wide + np.repeat(first[:, 1], per) + within % rows_b
    band_cells = {band: np.unique(covered[band_of[owner] == band]) for band in np.unique(band_of).tolist()}
    # Per band, the tile laid over its own triangles' footprint: (band, ab, sc).
    laid = []
    for band in np.unique(band_of).tolist():
        s, q = divmod(band, _TILE_ASPECTS)
        # The band's flattest cosine: squashed no further than its gentlest slope needs, so
        # nothing in it lands closer than its spacing along the surface.
        k = 1.0 - s * _TILE_BAND_STEP
        phi = 2.0 * math.pi * q / _TILE_ASPECTS
        fall = np.array([math.cos(phi), math.sin(phi)])
        squash = np.eye(2) - (1.0 - k) * np.outer(fall, fall)
        unsquash = np.eye(2) - (1.0 - 1.0 / k) * np.outer(fall, fall)
        sel = band_of == band
        corners = np.concatenate((v0s[sel], v1s[sel], v2s[sel]))[:, [a, b]]
        lo, hi = corners.min(axis=0), corners.max(axis=0)
        # Every copy of the tile that overlaps the band's footprint, found in the tile's own
        # (unsquashed) frame, in a fixed order.
        box = np.array([[lo[0], lo[1]], [hi[0], lo[1]], [lo[0], hi[1]], [hi[0], hi[1]]]) @ unsquash.T
        ia = np.arange(math.floor(box[:, 0].min() / side), math.floor(box[:, 0].max() / side) + 1)
        ib = np.arange(math.floor(box[:, 1].min() / side), math.floor(box[:, 1].max() / side) + 1)
        offsets = np.stack(np.meshgrid(ia, ib, indexing="ij"), axis=-1).reshape(-1, 2) * side
        ab = (offsets[:, None, :] + tile_ab[None, :, :]).reshape(-1, 2) @ squash.T
        sc = np.tile(tile_sc, len(offsets))
        inside = np.all((ab >= lo) & (ab <= hi), axis=1)
        ab, sc = ab[inside], sc[inside]
        at = np.floor((ab - origin) / cell).astype(np.int64)
        near = np.isin(at[:, 0] * wide + at[:, 1], band_cells[band], assume_unique=False)
        laid.append((band, ab[near], sc[near]))
    ab = np.concatenate([x[1] for x in laid])
    sc = np.concatenate([x[2] for x in laid])
    band_pt = np.concatenate([np.full(len(x[1]), x[0]) for x in laid])
    # The instance's own radius, which Keep Away From and the settle hold it to.
    rad = unit * sc if vary else np.full(len(sc), spacing * 0.5)
    # Drawn for every point before the blocks split, so the result is the same on any machine.
    roll = gen.random(len(ab))
    rank = gen.random(len(ab))
    # A tree per band, of its triangles' lateral centres: a point lands only on its band.
    trees = {}
    for band in np.unique(band_pt).tolist():
        idx = np.flatnonzero(band_of == band)
        trees[band] = (idx, cKDTree(((v0s[idx] + v1s[idx] + v2s[idx]) / 3.0)[:, [a, b]]))
    _warm_lookups(mask, avoid, v0s[:1], cKDTree)
    many = getattr(getattr(mask, "__self__", None), "sample_many", None)

    def block(job):
        band, s, e = job
        idx, tree = trees[band]
        rows, pos, tri = _drop_onto(tree, v0s[idx], v1s[idx], v2s[idx], ab[s:e], a, b, up_idx)
        rows, tri = rows + s, idx[tri]
        ok = np.ones(len(rows), dtype=bool)
        if mask is not None:
            if many is not None:
                ok = roll[rows] <= many(pos)
            else:
                ok = np.array([roll[r] <= mask(Gf.Vec3f(*p), Gf.Vec3f(*normals_all[t]))
                               for r, p, t in zip(rows, pos, tri)], dtype=bool)
        if avoid is not None and ok.any():
            live = np.flatnonzero(ok)
            ok[live[avoid.near_many(pos[live], rad[rows[live]], cKDTree)]] = False
        return rows[ok], pos[ok], tri[ok]

    # Blocks within a band (each band's points are one run of the arrays).
    jobs, start = [], 0
    for band, band_ab, _ in laid:
        end = start + len(band_ab)
        jobs += [(band, s, min(s + _TILE_BLOCK, end)) for s in range(start, end, _TILE_BLOCK)]
        start = end
    with ThreadPoolExecutor(max(1, min(len(jobs), os.cpu_count() or 1)), initializer=kd_query.single_thread) as pool:
        parts = list(pool.map(block, jobs))
    rows = np.concatenate([p[0] for p in parts]) if parts else np.zeros(0, dtype=np.int64)
    if not len(rows):
        return [], [], []
    pos = np.concatenate([p[1] for p in parts])
    tri = np.concatenate([p[2] for p in parts])
    # In rank order from here: the settle keeps the earlier of two that clash, and the count
    # keeps the first -- both evenly, everywhere.
    order = np.argsort(rank[rows], kind="stable")
    rows, pos, tri = rows[order], pos[order], tri[order]
    if any(band > 0 for band, _, _ in laid):
        kept = _settle_3d(cKDTree, pos, rad[rows])
        rows, pos, tri = rows[kept], pos[kept], tri[kept]
    rows, pos, tri = rows[:count], pos[:count], tri[:count]
    return (Vt.Vec3fArray.FromNumpy(pos.astype(np.float32)),
            Vt.Vec3fArray.FromNumpy(normals_all[tri].astype(np.float32)),
            sc[rows].tolist())


def _settle_3d(kdtree, pos, rad):
    """Which of (N, 3) *pos* to keep, in order: each one clear of every earlier one it kept by
    both radii, in 3D -- _settle_batch's walk, for the tiled points the bands brought close."""
    kept = np.ones(len(pos), dtype=bool)
    pairs = kdtree(pos).query_pairs(2.0 * float(rad.max()) * (1.0 + 1e-9), output_type="ndarray")
    if not len(pairs):
        return kept
    i, j = pairs.min(axis=1), pairs.max(axis=1)
    hit = ((pos[j] - pos[i]) ** 2).sum(axis=1) < (rad[i] + rad[j]) ** 2
    i, j = i[hit], j[hit]
    if len(j):
        order = np.argsort(j, kind="stable")
        i, j = i[order], j[order]
        later, starts = np.unique(j, return_index=True)
        for k, earlier in zip(later.tolist(), np.split(i, starts[1:])):
            kept[k] = not kept[earlier].any()
    return kept


def _warm_lookups(mask, avoid, pts, kdtree):
    """Build the density and keep-away trees before the streams start, so they are built once
    and not by every thread that finds them missing."""
    many = getattr(getattr(mask, "__self__", None), "sample_many", None)
    if many is not None:
        many(pts)
    if avoid is not None:
        avoid.near_many(pts, np.zeros(len(pts)), kdtree)


def _sized_rounds(gen, sgen, count, most, cdf, v0s, v1s, v2s, normals_all, cKDTree, unit, r_max,
                  vary, spacing, scale_lo, scale_hi, bias, mask, avoid, cell):
    """_scatter_sized's draw-and-test rounds for *count* instances from generators *gen* (points)
    and *sgen* (scales), at most *most* candidates a round when spacing-free. Returns the kept
    positions, normals and scales, as a list of arrays each."""
    # Placed instances by size class, each class searched only as far as its largest radius
    # reaches: searching every neighbour out to the biggest radius found hundreds of tiny
    # drops around every candidate once the surface filled up.
    bounds = [r_max, r_max / 4.0, r_max / 16.0, 0.0]
    placed_sets = [_PlacedSet() for _ in bounds[:-1]]
    # Kept points per batch, as arrays; *n_kept* counts them.
    positions, normals, scales, n_kept = [], [], [], 0
    attempts, max_attempts = 0, count * 100
    # Each triangle's share of the area, for the spacing-free draw.
    share = np.diff(cdf, prepend=0.0)
    while n_kept < count and attempts < max_attempts:
        if r_max > 0:
            # Spaced: small batches, as each candidate is held to every one kept before it and,
            # once the surface fills, nearly all of a big batch would be drawn for nothing.
            batch = int(min(most, max(512, 3 * (count - n_kept))))
        else:
            # No spacing, so no candidate depends on another: as many as the count still needs
            # at the rate the density and keep-away let through so far, in rounds of up to
            # *most*. A 39-million-blade meadow went round 5,000 times at 8,192, nearly all of
            # its 90 s in the rounds themselves.
            rate = n_kept / attempts if attempts else 1.0
            batch = int(min(most, max(512, 1.1 * (count - n_kept) / max(rate, 1e-3))))
        attempts += batch
        if r_max > 0:
            tri = np.minimum(np.searchsorted(cdf, gen.random(batch)), len(cdf) - 1)
        else:
            # How many land on each triangle, in one draw, rather than a search of the area
            # table per candidate: 4.6 s of the meadow's 39 million, against 0.1 s. They come
            # out grouped by triangle, which nothing spacing-free minds -- but see the trim.
            tri = np.repeat(np.arange(len(share)), gen.multinomial(batch, share))
        uv = gen.random((batch, 2))
        flip = uv.sum(axis=1) > 1.0
        uv[flip] = 1.0 - uv[flip]
        pts = (v0s[tri] * (1.0 - uv[:, :1] - uv[:, 1:]) + v1s[tri] * uv[:, :1] + v2s[tri] * uv[:, 1:])
        u = sgen.random(batch)
        sc = scale_lo + (scale_hi - scale_lo) * (u ** bias if bias != 1.0 else u)
        rad = unit * sc if vary else np.full(batch, spacing * 0.5)
        ok = np.ones(batch, dtype=bool)
        roll = gen.random(batch) if mask is not None else None
        # Against everything already placed -- first, as once the surface fills up it rejects
        # nearly everything, and the density lookup below is per candidate.
        if r_max > 0:
            for cls, placed_set in enumerate(placed_sets):
                first = True
                for cp, cr, tree in placed_set.parts(cKDTree):
                    if first or tree is None:
                        first = False
                        cand = np.flatnonzero(ok)
                        if not len(cand):
                            break
                    if tree is not None:
                        # Every candidate-placed pair as far apart as any clash can be, as two
                        # arrays (_within): a ball query per part built a Python list per
                        # candidate, and that, not the search, was most of its cost.
                        i, flat = _within(tree, pts[cand], (r_max + bounds[cls]) * (1.0 + 1e-9))
                        if not len(i):
                            continue
                        owner = cand[i]
                        d = np.linalg.norm(cp[flat] - pts[owner], axis=1)
                        ok[owner[d < rad[owner] + cr[flat]]] = False
                    else:
                        cand = np.flatnonzero(ok)
                        for i in cand:
                            if (np.linalg.norm(cp - pts[i], axis=1) < rad[i] + cr).any():
                                ok[i] = False
        if mask is not None:
            live = np.flatnonzero(ok)
            many = getattr(getattr(mask, "__self__", None), "sample_many", None)
            if many is not None:
                ok[live] = roll[live] <= many(pts[live])
            else:
                for i in live:
                    ok[i] = roll[i] <= mask(Gf.Vec3f(*pts[i]), Gf.Vec3f(*normals_all[tri[i]]))
        # Against each other, in draw order, and the keep-away set. Spacing-free, every survivor
        # is kept and the surplus trimmed below.
        need = count - n_kept if r_max > 0 else batch
        if cKDTree is not None:
            keep = _settle_batch(cKDTree, pts, rad, np.flatnonzero(ok), avoid, r_max, need)
        else:
            keep, new_p, new_r, grid = [], [], [], {}
            for i in np.flatnonzero(ok):
                pt, r = pts[i], rad[i]
                if avoid is not None and avoid.near(pt, r):
                    continue
                key = (math.floor(pt[0] / cell), math.floor(pt[1] / cell), math.floor(pt[2] / cell))
                if r_max > 0:
                    clash = False
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for dz in (-1, 0, 1):
                                for j in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                                    q = new_p[j]
                                    if (pt[0] - q[0]) ** 2 + (pt[1] - q[1]) ** 2 + (pt[2] - q[2]) ** 2 < (r + new_r[j]) ** 2:
                                        clash = True
                                        break
                                if clash:
                                    break
                            if clash:
                                break
                    if clash:
                        continue
                    grid.setdefault(key, []).append(len(new_p))
                new_p.append(pt)
                new_r.append(r)
                keep.append(i)
                if len(keep) >= need:
                    break
            keep = np.asarray(keep, dtype=np.int64)
        if r_max <= 0 and len(keep) > count - n_kept:
            # The round drew more than the count still needs. Grouped by triangle, the first
            # ones would leave the mesh's last triangles bare: drop the surplus at random.
            keep = np.delete(keep, gen.choice(len(keep), len(keep) - (count - n_kept), replace=False))
        before = n_kept
        if len(keep):
            n_kept += len(keep)
            positions.append(pts[keep])
            normals.append(normals_all[tri[keep]])
            scales.append(sc[keep])
            new_p, new_r = pts[keep], rad[keep]
            for cls, placed_set in enumerate(placed_sets):
                sel = (new_r <= bounds[cls]) & (new_r > bounds[cls + 1])
                if sel.any():
                    placed_set.add(new_p[sel], new_r[sel])
        # Full is full: when fewer than 1 candidate in 500 still finds room, the surface has
        # taken what it can -- the last gap-fillers are not worth minutes of max_attempts.
        if n_kept - before < batch / 500.0:
            break
    return positions, normals, scales


def _within(tree, pts, reach):
    """(i, j) for every pts[i] and tree point j closer than *reach*, as two index arrays.

    What a tree of *pts* against *tree* gives with sparse_distance_matrix, which walked the two
    trees against each other in one thread -- 40 of BushesValley's 49 s on the vista, for 800
    pairs a call. This asks for each point's nearest k within *reach*, on every core, and asks
    again with a larger k only for the points that filled all k: arrays throughout, and no
    pair left out.
    """
    n = tree.n
    rows, cols = [np.zeros(0, dtype=np.int64)], [np.zeros(0, dtype=np.int64)]
    idx = np.arange(len(pts))
    k = 8
    while len(idx):
        k = min(k, n)
        d, j = kd_query.query(tree, pts[idx], k=k, distance_upper_bound=reach)
        if k == 1:
            d, j = d[:, None], j[:, None]
        hit = np.isfinite(d)
        more = hit[:, -1] if k < n else np.zeros(len(idx), dtype=bool)
        r, c = np.nonzero(hit[~more])
        rows.append(idx[~more][r])
        cols.append(j[~more][r, c])
        idx = idx[more]
        k *= 4
    return np.concatenate(rows), np.concatenate(cols)


class _PlacedSet:
    """One size class of the instances placed so far, as a few parts that double in size, each
    with its KD-tree built once.

    One tree of the whole class, rebuilt every batch, rebuilt everything placed so far each
    time -- 130k points about forty times over for a verge. Here a batch's points are a part
    of their own, merged into the one before only once the two are near in size, so a point
    is rebuilt into a tree a handful of times and a query looks at a handful of trees.
    """

    __slots__ = ("_parts",)

    def __init__(self):
        self._parts: list = []  # [points, radii, tree or None]

    def add(self, points, radii):
        self._parts.append([points, radii, None])
        while len(self._parts) > 1 and len(self._parts[-2][0]) < 2 * len(self._parts[-1][0]):
            (p0, r0, _), (p1, r1, _) = self._parts[-2:]
            self._parts[-2:] = [[np.concatenate([p0, p1]), np.concatenate([r0, r1]), None]]

    def parts(self, kdtree):
        """(points, radii, tree) per part; the tree None when there is no *kdtree*."""
        for part in self._parts:
            if kdtree is not None and part[2] is None:
                part[2] = kdtree(part[0])
            yield part[0], part[1], part[2]


def _settle_batch(kdtree, pts, rad, cand, avoid, r_max, need):
    """The candidates (indices into *pts*, in draw order) the one-at-a-time pass keeps: clear
    of the keep-away set, and clear of every earlier candidate it kept. At most *need*.

    The same answer as walking them in order, without the walk. Keep Away From doesn't depend
    on the order, so it is one query for the batch. Of the rest, a candidate that clashes with
    no earlier one is kept outright, so only those that do clash are walked -- and each looks
    at its own clashes alone. On a 130k-plant verge the walk was a hash-grid lookup per
    candidate, 27 cells each, and a third of the cook.
    """
    if avoid is not None and len(cand):
        cand = cand[~avoid.near_many(pts[cand], rad[cand], kdtree)]
    if r_max > 0 and len(cand) > 1:
        p, r = pts[cand], rad[cand]
        # Two radii apart at most: nothing further can clash. query_pairs gives i < j.
        pairs = kdtree(p).query_pairs(2.0 * r_max * (1.0 + 1e-9), output_type="ndarray")
        if len(pairs):
            i, j = pairs[:, 0], pairs[:, 1]
            # Later minus earlier, later radius plus earlier, as the walk computes it.
            d2 = (p[j, 0] - p[i, 0]) ** 2 + (p[j, 1] - p[i, 1]) ** 2 + (p[j, 2] - p[i, 2]) ** 2
            hit = d2 < (r[j] + r[i]) ** 2
            i, j = i[hit], j[hit]
            kept = np.ones(len(cand), dtype=bool)
            if len(j):
                order = np.argsort(j, kind="stable")
                i, j = i[order], j[order]
                later, starts = np.unique(j, return_index=True)
                # Ascending, so every earlier candidate is settled before a later one asks.
                for k, earlier in zip(later.tolist(), np.split(i, starts[1:])):
                    kept[k] = not kept[earlier].any()
            cand = cand[kept]
    return cand[:need]


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
_SAMPLING_ONLY = ("auto_spacing", "spacing", "max_count", "density", "seed", "max_slope", "sampling")


def _update_sampling_params(owner) -> None:
    """Hide the distribution controls while the positions are being handed to us.

    With Painted Points connected, Auto Spacing, Spacing, Max Count and Seed cannot affect
    the result. A control that cannot affect the result is worse than an absent one -- it
    invites you to reach for it when something looks wrong.
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


def _painted_points(stage, points_path, up=None):
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

    up = up or Gf.Vec3f(0, 0, 1)
    # Straight off the arrays: a Vt array iterates as Gf.Vec3f already, where unpacking each
    # point into a new one was most of reading a vine's 30k leaf points.
    positions = list(Vt.Vec3fArray(points))
    facing = list(Vt.Vec3fArray(normals)) if normals else []
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

        point_parts.append(mesh_math.world_points(p, mesh_points))
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

    __slots__ = ("_points", "_weights", "_origin", "_cell", "_cells", "_neighbourhood", "_tree")

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
        self._tree = None  # sample_many's KD-tree, built on first use; False without scipy

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

    def sample_many(self, points):
        """sample() for an (N, 3) array at once: the same nearest-vertex weight, 0 where no
        vertex is in the neighbourhood. A per-point Python call was most of a condensation
        scatter's cook -- a quarter of a million candidates.

        With scipy, a KD-tree's nearest vertex within two cells -- six times the grid's speed
        on a terrain, where a few candidates per cell made the per-cell loop below mostly
        Python. Where two vertices are equally near it may pick the other one.
        """
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        out = np.zeros(len(points))
        if not len(points):
            return out
        if self._tree is None:
            try:
                from scipy.spatial import cKDTree
            except ImportError:  # scipy is optional: the grid below, near enough the same
                self._tree = False
            else:
                self._tree = cKDTree(self._points)
        if self._tree is not False:
            # On every core: one thread was 21 of GrassValley's 85 s on the woodland vista.
            _, nearest = kd_query.query(self._tree, points, k=1, distance_upper_bound=2.0 * self._cell)
            found = nearest < len(self._points)
            out[found] = self._weights[nearest[found]]
            return out
        keys = self._keys(points)
        # Grouped by cell once. Finding each cell's points with `keys == key` scanned the
        # whole batch per cell -- candidates x cells -- which was most of this lookup's time.
        uniq, inverse = np.unique(keys, return_inverse=True)
        lists = [self._nearby(key) for key in uniq.tolist()]
        sizes = np.fromiter(map(len, lists), dtype=np.int64, count=len(lists))
        widest = int(sizes.max()) if len(sizes) else 0
        if not widest:
            return out
        if widest <= 4 * max(1.0, float(np.median(sizes[sizes > 0]))):
            # Neighbourhoods all about one size -- a ground, a wall: every point against its
            # cell's vertices in one padded array, in blocks. The padding vertex sits too far
            # away to ever be nearest and weighs 0, which is also the answer for a point with
            # no vertex near it at all.
            table = np.full((len(uniq), widest), len(self._points), dtype=np.int64)
            for u, near in enumerate(lists):
                table[u, :len(near)] = near
            index = table[inverse]
            far = np.vstack([self._points, np.full((1, 3), 1e150)])
            weights = np.append(self._weights, 0.0)
            block = max(1, 4_000_000 // widest)
            for lo in range(0, len(points), block):
                idx = index[lo:lo + block]
                diff = points[lo:lo + block, None, :] - far[idx]
                nearest = np.einsum("ijk,ijk->ij", diff, diff).argmin(axis=1)
                out[lo:lo + block] = weights[idx[np.arange(len(idx)), nearest]]
            return out
        # Uneven (a scan, dense in places): per cell, so no point pays for the densest one.
        order = np.argsort(inverse, kind="stable")
        bounds = np.searchsorted(inverse[order], np.arange(len(uniq) + 1))
        for u, nearby in enumerate(lists):
            if not len(nearby):
                continue
            rows = order[bounds[u]:bounds[u + 1]]
            diff = points[rows, None, :] - self._points[nearby][None, :, :]
            out[rows] = self._weights[nearby[np.einsum("ijk,ijk->ij", diff, diff).argmin(axis=1)]]
        return out

    def sample(self, point, normal=None):
        """The painted weight nearest `point`, in 0..1. `normal` is unused but is part of the
        hook's signature -- a future falloff-by-facing would want it.

        Through sample_many, so one point and a batch take the same path -- the KD-tree's,
        or the grid's without scipy -- and agree at the edge of the painted region.
        """
        return float(self.sample_many(np.asarray([[point[0], point[1], point[2]]], dtype=np.float64))[0])


class _MaskProduct:
    """Two mask hooks multiplied: the painted Density Field and Grow Near, say. Keeps the
    batched `sample_many` the sampler looks for, when both halves have one."""

    __slots__ = ("_a", "_b")

    def __init__(self, a, b):
        self._a, self._b = a, b

    @staticmethod
    def _many(hook, points):
        many = getattr(getattr(hook, "__self__", None), "sample_many", None)
        if many is not None:
            return np.asarray(many(points), dtype=np.float64)
        return np.fromiter((hook(Gf.Vec3f(*p), None) for p in points), dtype=np.float64, count=len(points))

    def sample_many(self, points):
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        return self._many(self._a, points) * self._many(self._b, points)

    def sample(self, point, normal=None):
        return self._a(point, normal) * self._b(point, normal)


def _combine_masks(*hooks):
    """One mask hook from several (None ones skipped), or None when there are none -- so a
    scatter with neither a Density Field nor Grow Near never pays for the lookup."""
    live = [h for h in hooks if h is not None]
    if not live:
        return None
    mask = live[0]
    for h in live[1:]:
        mask = _MaskProduct(mask, h).sample
    return mask


def _batched(mask):
    """A mask hook's array form: the `sample_many` of the object whose bound `sample` the hook
    is, or None when it has none."""
    return getattr(getattr(mask, "__self__", None), "sample_many", None)


#: How many random numbers _scatter_on_triangles draws ahead at a time, and how many candidates
#: it asks a batched mask about at once.
_DRAW_BLOCK = 16384
_MASK_BLOCK = 2048


def _scatter_on_triangles(triangles, count, rng, min_dist=0, mask=None):
    """Area-weighted point sampling with min-distance rejection.

    *mask*: optional per-point hook ``mask(point, normal) -> 0..1`` -- the paint
    gizmo will pass a weight function here; a candidate is kept with that
    probability. None means accept everything that passes spacing.

    The draw is unchanged since it was written: a candidate takes three numbers from *rng*
    (triangle, u, v), and one more for the mask's roll if it passes spacing, so a given seed
    still produces the scatter it always did. Only the work around each draw is in arrays:
    the numbers are drawn ahead in blocks, every block's candidates are built at once, and a
    mask with a `sample_many` is asked about candidates a block at a time. One at a time, the
    triangle search, the point and the mask lookup were most of a wall-ivy cook. The spacing
    test stays one candidate at a time, as it depends on the points already kept. Numbers
    drawn ahead but not used are given back, so *rng* ends where the old loop left it.
    """
    if not len(triangles):
        return [], []

    areas = triangles.areas()
    total_area = float(areas.sum())
    if total_area < 1e-10:
        return [], []
    cdf = np.cumsum(areas) / total_area
    last_tri = len(triangles) - 1

    v0s, v1s, v2s, normals_all = triangles.v0, triangles.v1, triangles.v2, triangles.n
    many = _batched(mask) if mask is not None else None
    positions, normals = [], []
    grid = _PoissonGrid(min_dist)
    max_attempts = count * 100
    attempts = 0

    start_state = rng.getstate()
    draw = rng.random
    numbers = np.empty(0)  # drawn, from the global draw index `base` on
    base = 0
    used = 0  # global index of the next number to use
    tri = points = coords = weights = None

    def refill():
        """Keep the unused numbers, draw a block more, and build a candidate at each index."""
        nonlocal numbers, base, tri, points, coords, weights
        numbers = np.concatenate([numbers[used - base:], np.fromiter(
            (draw() for _ in range(_DRAW_BLOCK)), dtype=np.float64, count=_DRAW_BLOCK)])
        base = used
        r, u, v = numbers[:-2], numbers[1:-1].copy(), numbers[2:].copy()
        tri = np.minimum(np.searchsorted(cdf, r, side="left"), last_tri)
        flip = u + v > 1.0
        u[flip], v[flip] = 1.0 - u[flip], 1.0 - v[flip]
        w = 1.0 - u - v
        # The old loop's arithmetic, in its order, then to float32 as Gf.Vec3f did.
        points = (w[:, None] * v0s[tri] + u[:, None] * v1s[tri] + v[:, None] * v2s[tri]).astype(np.float32)
        coords = points.tolist()
        weights = np.full(len(points), np.nan) if many is not None else None

    while len(positions) < count and attempts < max_attempts:
        if used - base + 4 > len(numbers):
            refill()
        at = used - base
        attempts += 1
        used += 3
        pt = coords[at]
        if grid.too_close(pt):
            continue
        if mask is not None:
            if many is not None:
                weight = weights[at]
                if weight != weight:  # not asked yet: ask about this block of candidates
                    stop = min(at + _MASK_BLOCK, len(points))
                    weights[at:stop] = many(points[at:stop].astype(np.float64))
                    weight = weights[at]
            else:
                weight = mask(Gf.Vec3f(*pt), Gf.Vec3f(*normals_all[tri[at]]))
            roll = numbers[used - base]
            used += 1
            if roll > weight:
                continue
        positions.append(Gf.Vec3f(*pt))
        normals.append(Gf.Vec3f(*normals_all[tri[at]]))
        grid.add(pt)

    # Give back what was drawn ahead: rng moves on by exactly the numbers used.
    rng.setstate(start_state)
    for _ in range(used):
        draw()
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
# rotation helpers -- internal math in Quatf, converted to Quath only on write
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


def _normal_to_quat(normal, up=None):
    """Rotation taking the prototype's up to the given normal direction.

    A prototype stands along the stage's up axis -- a House builds along whichever one the
    stage has -- so that, not +Z, is what turns onto the normal. Taking +Z on a Y-up stage
    (Houdini's) tipped every instance on its side while the source prim stood upright.
    """
    return _align_quat(up or Gf.Vec3f(0, 0, 1), normal)


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
        return _normal_to_quat(normal, world_up)
    if mode == "Random":
        return _random_quat(rng)
    if mode == "Long Axis Up" and proto is not None:
        return _align_quat(proto.long_axis_dir(), world_up or Gf.Vec3f(0, 0, 1))
    if mode == "Long Axis to Normal" and proto is not None:
        return _align_quat(proto.long_axis_dir(), normal)
    # "Keep Original" / "Up" -- prototype's own orientation.
    return Gf.Quatf(1, 0, 0, 0)


def _spun_quats(count, axis, amount_deg, rng):
    """A random spin for *count* upright instances, as (count, 4) float32 (i, j, k, real) --
    the layout Vt.QuathArray takes from numpy. The same spin _apply_random_rotation gives one
    instance: about *axis* ("X"/"Y"/"Z", or "All" for a random axis), up to *amount_deg*
    either way.

    Drawn as arrays from a numpy generator seeded off *rng*, not one instance at a time: the
    per-instance gauss/uniform calls were a third of a 9M-instance forest's cook.
    """
    out = np.zeros((count, 4), dtype=np.float32)
    out[:, 3] = 1.0
    if axis == "Off" or not count:
        return out
    gen = np.random.default_rng(rng.getrandbits(63))
    amount_rad = math.radians(amount_deg)
    spun = np.ones(count, dtype=bool)
    if axis == "All":
        ax = gen.standard_normal((count, 3))
        length = np.sqrt(np.einsum("ij,ij->i", ax, ax))
        spun = length >= 1e-8      # as _apply_random_rotation: no axis, no spin
        ax /= np.where(spun, length, 1.0)[:, None]
    else:
        ax = np.zeros((count, 3))
        ax[:, "XYZ".index(axis)] = 1.0
    half = gen.uniform(-amount_rad, amount_rad, count) * 0.5
    quat = np.empty((count, 4), dtype=np.float32)
    quat[:, :3] = ax * np.sin(half)[:, None]
    quat[:, 3] = np.cos(half)
    out[spun] = quat[spun]
    # Times the identity base: every component carries over exactly.
    return out


def _vectors(vectors, count):
    """A list of Gf.Vec3f (or 3-sequences) as a (count, 3) float64 array, through Vt's buffer
    rather than numpy reading each vector as a sequence."""
    try:
        return np.asarray(Vt.Vec3fArray(vectors), dtype=np.float64).reshape(count, 3)
    except (TypeError, ValueError):
        return np.asarray(vectors, dtype=np.float64).reshape(count, 3)


def _align_quats(from_dirs, to_dirs):
    """_align_quat for many direction pairs at once, as (N, 4) float64 (real, i, j, k).

    Each argument is (N, 3), or one (3,) direction for all of them. The same cases as
    _align_quat: no turn for a zero vector or a near-parallel pair, a half turn about a
    perpendicular axis for a near-opposite one.
    """
    a = np.asarray(from_dirs, dtype=np.float64)
    b = np.asarray(to_dirs, dtype=np.float64)
    n = max(len(a) if a.ndim == 2 else 1, len(b) if b.ndim == 2 else 1)
    a = np.broadcast_to(a, (n, 3))
    b = np.broadcast_to(b, (n, 3))
    out = np.zeros((n, 4))
    out[:, 0] = 1.0
    la = np.sqrt(np.einsum("ij,ij->i", a, a))
    lb = np.sqrt(np.einsum("ij,ij->i", b, b))
    live = (la >= 1e-8) & (lb >= 1e-8)
    a = a / np.where(live, la, 1.0)[:, None]
    b = b / np.where(live, lb, 1.0)[:, None]
    dot = np.einsum("ij,ij->i", a, b)
    opposite = live & (dot < -0.9999)
    if opposite.any():
        axis = np.cross(a[opposite], [1.0, 0.0, 0.0])
        short = np.sqrt(np.einsum("ij,ij->i", axis, axis)) < 1e-6
        axis[short] = np.cross(a[opposite][short], [0.0, 1.0, 0.0])
        axis /= np.sqrt(np.einsum("ij,ij->i", axis, axis))[:, None]
        out[opposite] = np.column_stack([np.zeros(len(axis)), axis])
    turned = live & (dot <= 0.9999) & ~opposite
    if turned.any():
        axis = np.cross(a[turned], b[turned])
        axis /= np.sqrt(np.einsum("ij,ij->i", axis, axis))[:, None]
        half = np.arccos(np.clip(dot[turned], -1.0, 1.0)) * 0.5
        out[turned] = np.column_stack([np.cos(half), axis * np.sin(half)[:, None]])
    return out


def _quat_mul_arrays(a, b):
    """_quat_mul for (N, 4) (real, i, j, k) arrays."""
    aw, ax, ay, az = a.T
    bw, bx, by, bz = b.T
    return np.column_stack([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ])


def _rotate_arrays(quats, vectors):
    """(N, 3) vectors turned by (N, 4) (real, i, j, k) quaternions."""
    w = quats[:, :1]
    u = quats[:, 1:]
    uv = np.cross(u, vectors)
    return vectors + 2.0 * w * uv + 2.0 * np.cross(u, uv)


#: Up Along Surface menu entries -> the prototype axis that should point up the surface.
_ALIGN_UP_AXES = {
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}


def _twist_up(base, normals, axis, up):
    """The turn about each instance's own up axis that points *axis* up the surface.

    After base, the prototype's up axis lies along the normal and *axis* somewhere in the
    surface plane. This turns it toward world up as seen on the surface -- straight up a
    wall -- so a strand hangs down any wall it is on. Where the surface faces up or down
    there is no "up the surface", and nothing turns.

    Args:
        base: (N, 4) quaternions taking the prototype's up axis to each normal.
        normals: (N, 3) unit normals.
        axis: (3,) the prototype axis, at right angles to its up axis.
        up: (3,) world (and prototype) up.

    Returns:
        (N, 4) quaternions about the prototype's up axis, to apply before base.
    """
    n = np.asarray(normals, dtype=np.float64)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, length, out=np.zeros_like(n), where=length > 1e-12)
    turned = _rotate_arrays(base, np.broadcast_to(np.asarray(axis, dtype=np.float64), n.shape))
    target = up[None, :] - np.einsum("ij,j->i", n, up)[:, None] * n
    length = np.linalg.norm(target, axis=1)
    on_slope = length > 1e-6
    target[on_slope] /= length[on_slope, None]
    angle = np.arctan2(np.einsum("ij,ij->i", np.cross(turned, target), n), np.einsum("ij,ij->i", turned, target))
    angle[~on_slope] = 0.0
    half = angle * 0.5
    return np.column_stack([np.cos(half), up[None, :] * np.sin(half)[:, None]])


def _orientations(count, mode, normals, protos, assignment, world_up, axis, amount_deg, rng, align_up="Off"):
    """The loop's _compute_orientation then _apply_random_rotation, for every instance at once.

    The random numbers come off *rng* one Python call at a time, in the loop's order -- a
    Random mode's three, then the spin's -- so each instance gets the turn it always did.
    Only the quaternion maths is in arrays: as Gf objects, one instance at a time, it was a
    fifth of a wall-ivy cook.

    Args:
        count: How many instances.
        mode: The orient mode.
        normals: (count, 3) surface normals.
        protos: The resolved prototypes.
        assignment: (count,) prototype index of each instance.
        world_up: The stage's up axis, as a Gf.Vec3f.
        axis: The random spin axis: "Off", "X", "Y", "Z" or "All".
        amount_deg: The most the spin turns either way.
        rng: The orient random.Random, left where the loop would have left it -- or a list
            of one per instance, as painted points seed each their own.
        align_up: Align to Normal only: the prototype axis turned up the surface ("+X", ...),
            or "Off".

    Returns:
        (count, 4) float64 quaternions, (real, i, j, k).
    """
    up = np.asarray(tuple(world_up), dtype=np.float64)
    random_mode = mode == "Random"
    amount_rad = math.radians(amount_deg)
    per_instance = isinstance(rng, list)
    if not per_instance:
        rnd, gauss, uniform = rng.random, rng.gauss, rng.uniform
    shoemake = np.zeros((count, 3)) if random_mode else None
    spin_axes = np.zeros((count, 3))
    angles = np.zeros(count)
    spun = np.zeros(count, dtype=bool)
    fixed = None if axis in ("Off", "All") else np.eye(3)["XYZ".index(axis)]
    for i in range(count):
        if per_instance:
            rnd, gauss, uniform = rng[i].random, rng[i].gauss, rng[i].uniform
        if random_mode:
            shoemake[i] = (rnd(), rnd(), rnd())
        if axis == "Off":
            continue
        if fixed is None:
            gx, gy, gz = gauss(0, 1), gauss(0, 1), gauss(0, 1)
            length = math.sqrt(gx * gx + gy * gy + gz * gz)
            if length < 1e-8:
                continue  # as _apply_random_rotation: no axis, no spin, no angle drawn
            spin_axes[i] = (gx / length, gy / length, gz / length)
        else:
            spin_axes[i] = fixed
        angles[i] = uniform(-amount_rad, amount_rad)
        spun[i] = True

    if mode == "Align to Normal":
        base = _align_quats(up, normals)
        turn_axis = np.asarray(_ALIGN_UP_AXES.get(align_up, (0.0, 0.0, 0.0)))
        if np.abs(np.cross(turn_axis, up)).sum() > 1e-6:  # an axis along up has nothing to turn
            base = _quat_mul_arrays(base, _twist_up(base, np.asarray(normals, dtype=np.float64), turn_axis, up))
    elif random_mode:
        u1, u2, u3 = shoemake.T
        s1, s2 = np.sqrt(1 - u1), np.sqrt(u1)
        base = np.column_stack([s2 * np.cos(2 * np.pi * u3), s1 * np.sin(2 * np.pi * u2),
                                s1 * np.cos(2 * np.pi * u2), s2 * np.sin(2 * np.pi * u3)])
    elif mode in ("Long Axis Up", "Long Axis to Normal"):
        long_axes = np.asarray([tuple(p.long_axis_dir()) for p in protos], dtype=np.float64)[assignment]
        base = _align_quats(long_axes, up if mode == "Long Axis Up" else normals)
    else:
        base = np.zeros((count, 4))
        base[:, 0] = 1.0
    if not spun.any():
        return base
    half = angles * 0.5
    spin = np.column_stack([np.cos(half), spin_axes * np.sin(half)[:, None]])
    out = base.copy()
    if mode == "Align to Normal":
        # About the instance's own axes, so a spin about up turns it about the normal and it
        # stays on the surface. About the world's, a leaf on a wall spun about Z stood out of
        # the wall: the same spin as on the ground, where the two agree.
        out[spun] = _quat_mul_arrays(base[spun], spin[spun])
    else:
        out[spun] = _quat_mul_arrays(spin[spun], base[spun])
    return out


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

    ``bmin``/``bmax`` are the *untransformed* local bounds -- the prototype reference has its
    xformOpOrder cleared, so that is what the instancer actually instances -- and ``scale`` is
    the source prim's world scale, which the instancer re-applies per instance. Effective
    world size is therefore ``(bmax - bmin) * scale * instance_scale``. A turned source is
    the exception: its prototype keeps the source's rotation and scale (_source_turn), the
    bounds include them, and ``scale`` is 1.
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


def _resolve_prototypes(node, scratch_stage, missing=None):
    """Turn the Prototypes list rows into _Proto entries.

    Handles, per row: glob expansion (one prototype per matched prim), a %collection (one
    prototype per topmost member, deactivated members included), an explicit variant
    selection, and ``{set=*}`` wildcards (one prototype per variant in the set, the row's
    weight split evenly between them). Paths that match no prim are appended to missing, when
    given, for the readout to name.
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
            # Inactive prims match too: a library entry is deactivated so it is never drawn,
            # and the prototype that references it switches it back on.
            matched = (
                chunk_utils.resolve_prim_paths(scratch_stage, base, include_inactive=True)
                if scratch_stage
                else [base]
            )
            if base.startswith("%"):
                # A collection that includes /Library/oak (expandPrims, the default) has every
                # mesh under the oak as a member too; the oak is the prototype, not its parts.
                matched = _topmost(matched)
            # A plain path comes back as typed whether or not the prim exists (only a glob is
            # matched against the stage), so existence is checked here. A path that carries a
            # variant selection is not a prim path, so only "matched nothing" counts for it.
            if missing is not None and scratch_stage is not None and (
                    not matched
                    or ("{" not in token and not any(scratch_stage.GetPrimAtPath(m) for m in matched))):
                missing.append(token)
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


def _topmost(paths):
    """The paths with no ancestor among them, in their order: /a and /b from /a, /a/x, /b."""
    kept = []
    for path in sorted(paths, key=len):
        if not any(path.startswith(k.rstrip("/") + "/") for k in kept):
            kept.append(path)
    keep = set(kept)
    return [p for p in paths if p in keep]


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
            # Unknown set (or a prim that isn't there yet) -- drop the wildcard rather than
            # authoring a bogus selection.
            continue
        combos = [c + ((vset_name, n),) for c in combos for n in names]
    return combos


def _proto_label(path, selections):
    name = path.rsplit("/", 1)[-1] or path
    if selections:
        name += "".join(f"{{{s}={v}}}" for s, v in selections)
    return name


def _written_under(path, layers):
    """Whether any of these layers authors something below path (not on path itself).

    A Reference chunk that brings in a library entry authors only at its own path; a chunk that
    writes *into* the entry -- a scatter's instancer, a Noise Field primvar, a Transform on a
    child -- leaves child specs below it.
    """
    for layer in layers:
        spec = layer.GetPrimAtPath(path)
        if spec and len(spec.nameChildren):
            return True
    return False


def _other_chunk_layers(own):
    """Every chunk and graph layer except own: whatever else in the scene authors opinions."""
    own_id = own.identifier if own is not None else None
    return [lyr for _parent, lyr in chunk_registry.ChunkLayerManager().iter_chunk_layers()
            if lyr.identifier != own_id]


def _author_prototypes(stage, inst_path, protos, scratch_stage, hide_sources=False, drop_purpose=None,
                       foreign_layers=()):
    """Author one prototype prim per _Proto under <inst_path>/Prototypes.

    Each is an internal reference to the source prim with its xformOpOrder cleared (the
    instancer supplies the transform) plus any variant selection for that entry -- the same
    asset can therefore appear several times, each row selecting a different variant. The
    reference takes the source as authored, active = false included, so the prototype is
    always switched on: a library of deactivated entries is never drawn itself, only
    through the prototypes that use it. hide_sources deactivates each source that resolved
    (in the edit target), short of one that holds the instancer. A source that another chunk
    writes inside (foreign_layers author below it) is hidden with visibility instead: USD refuses
    to author under an inactive prim, so deactivating it would make that chunk's next cook fail
    ("Failed to define UsdPrim"); its prototypes are switched visible again. (Visibility only
    hides an Imageable prim -- a Reference chunk's Xform is one; a typeless source stays visible.)
    drop_purpose
    deactivates whatever inside a prototype is authored with that purpose (see _drop_purpose).

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
        ref_prim.SetActive(True)
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
        xformable = UsdGeom.Xformable(ref_prim)
        xformable.GetXformOpOrderAttr().Set([])
        turn = _source_turn(src)
        if turn is not None:
            # A suffix of its own: the source may have authored a plain xformOp:transform.
            xformable.AddTransformOp(opSuffix="scatterSource").Set(turn)

        proto.path = path
        _measure_proto(proto, ref_prim, src, baked=turn is not None)
        if drop_purpose:
            _drop_purpose(ref_prim, drop_purpose)
        resolved.append(proto)

    if hide_sources:
        inst = Sdf.Path(inst_path)
        for proto in resolved:
            src_path = Sdf.Path(proto.src_path)
            if inst.HasPrefix(src_path):
                continue
            if _written_under(src_path, foreign_layers):
                UsdGeom.Imageable(stage.OverridePrim(src_path)).CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
                UsdGeom.Imageable(stage.GetPrimAtPath(proto.path)).CreateVisibilityAttr().Set(UsdGeom.Tokens.inherited)
            else:
                stage.OverridePrim(src_path).SetActive(False)

    if not resolved:
        path = f"{proto_scope}/proto_0"
        cube = UsdGeom.Cube.Define(stage, path)
        cube.GetSizeAttr().Set(1.0)
        fallback = _Proto(label=_FALLBACK_LABEL, path=path)
        resolved.append(fallback)
    return resolved


def _drop_purpose(prim, purpose):
    """Deactivate the prims under prim authored with this purpose.

    With a viewport proxy the render instancer is purpose=render and its sibling
    purpose=proxy, but an asset that carries its own render/proxy split keeps both meshes
    in every prototype. USD says the instancer's purpose wins over theirs; Hydra resolves
    purpose inside the prototype, where the asset's own wins -- so Storm drew the render
    instancer's proxy meshes (every instance, the whole cost the proxy was meant to cut), and
    a Hydra renderer would draw the proxy instancer's render meshes too. Each instancer
    therefore keeps only the half of the asset its purpose is for.
    """
    doomed = [
        p for p in Usd.PrimRange(prim)
        if p != prim and p.IsA(UsdGeom.Imageable)
        and (attr := UsdGeom.Imageable(p).GetPurposeAttr()).HasAuthoredValue()
        and attr.Get() == purpose
    ]
    for p in doomed:
        p.SetActive(False)


def _author_proxy(stage, proxy_path, mode, protos, specs, scratch_stage, keep, arrays):
    """A purpose=proxy sibling instancer: a subset of the instances, optionally as boxes.

    A sibling and not a child, because purpose inherits: under the render instancer it would
    be render too. It has its own prototypes for the same reason -- the render instancer's
    inherit its purpose -- and so that Bounds can swap each one for a box.

    Bounds boxes are built from the prototype's measured local bounds, at the prototype
    root, so the instance transforms (which already carry proto.scale) place them exactly
    where the real prototype would sit.
    """
    proxy = UsdGeom.PointInstancer.Define(stage, proxy_path)
    proxy.CreatePurposeAttr().Set(UsdGeom.Tokens.proxy)

    if mode == "Bounds":
        stage.DefinePrim(f"{proxy_path}/Prototypes", "Scope")
        targets = []
        for i, proto in enumerate(protos):
            path = f"{proxy_path}/Prototypes/proto_{i}"
            box = UsdGeom.Cube.Define(stage, path)
            box.GetSizeAttr().Set(1.0)
            # A box authored on an earlier cook into a layer nobody cleared keeps its ops;
            # cleared first, so adding them again reuses the attributes rather than raising.
            box.ClearXformOpOrder()
            centre = (proto.bmin + proto.bmax) * 0.5
            size = proto.bmax - proto.bmin
            box.AddTranslateOp().Set(Gf.Vec3d(centre[0], centre[1], centre[2]))
            box.AddScaleOp().Set(Gf.Vec3f(max(size[0], 1e-6), max(size[1], 1e-6), max(size[2], 1e-6)))
            targets.append(Sdf.Path(path))
    else:
        # Copies: _author_prototypes writes the path it authored back onto each spec, and
        # the render instancer's protos must keep theirs.
        proxies = _author_prototypes(stage, proxy_path, [replace(s) for s in specs], scratch_stage,
                                     drop_purpose=UsdGeom.Tokens.render)
        targets = [Sdf.Path(p.path) for p in proxies]
    proxy.GetPrototypesRel().SetTargets(targets)

    positions, indices, scales, orients = arrays
    proxy.GetPositionsAttr().Set(Vt.Vec3fArray([positions[i] for i in keep]))
    proxy.GetProtoIndicesAttr().Set(Vt.IntArray([indices[i] for i in keep]))
    proxy.GetScalesAttr().Set(Vt.Vec3fArray([scales[i] for i in keep]))
    proxy.GetOrientationsAttr().Set(Vt.QuathArray([orients[i] for i in keep]))


def _source_turn(src_prim):
    """The source's world rotation and scale, for a source that is turned or mirrored.

    Args:
        src_prim (Usd.Prim): The prim a prototype references

    Returns:
        Gf.Matrix4d | None: Its world transform without the translation, or None when it
        is only scaled along its axes (proto.scale carries that).
    """
    world = Gf.Matrix4d(UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(src_prim))
    axis_aligned = all(
        (world[i][j] > 1e-9) if i == j else abs(world[i][j]) <= 1e-9
        for i in range(3) for j in range(3)
    )
    if axis_aligned:
        return None
    world.SetTranslateOnly(Gf.Vec3d(0, 0, 0))
    return world


def _measure_proto(proto, proto_prim, src_prim, baked=False):
    """Fill in proto.bmin/bmax (local, variant-aware) and proto.scale (source world scale).

    Args:
        proto (_Proto): The entry to fill in
        proto_prim (Usd.Prim): The prototype prim
        src_prim (Usd.Prim): The prim it references
        baked (bool): The prototype carries the source's rotation and scale itself (see
            _source_turn); its bounds then include them and proto.scale stays 1.
    """
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    bound = bbox_cache.ComputeLocalBound(proto_prim) if baked else bbox_cache.ComputeUntransformedBound(proto_prim)
    rng = bound.ComputeAlignedRange()
    if not rng.IsEmpty():
        mn, mx = rng.GetMin(), rng.GetMax()
        proto.bmin = Gf.Vec3f(mn[0], mn[1], mn[2])
        proto.bmax = Gf.Vec3f(mx[0], mx[1], mx[2])
    if baked:
        proto.scale = Gf.Vec3f(1, 1, 1)
        return

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

    Returns [(point, normal, level)] in fill order -- a whole level before the next one
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
        # stack on.
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


def _on_orient_changed(owner, notice, sender):
    """Up Along Surface only does something with Align to Normal: hide it otherwise."""
    chunk_obj = owner.get_chunk()
    var = chunk_obj.get_param("Variation") if chunk_obj else None
    if var:
        stack = (chunk_obj.get_param("scatter_mode") or owner).value == "Stack"
        _set_hidden(var.get_param("align_up"), stack or owner.value != "Align to Normal")


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
        _set_hidden(dist.get_param("density"), mode != "Surface")
    var = chunk_obj.get_param("Variation")
    if var:
        _set_hidden(var.get_param("orient_mode"), mode == "Stack")
        orient = var.get_param("orient_mode")
        _set_hidden(var.get_param("align_up"), mode == "Stack" or (orient is not None and orient.value != "Align to Normal"))
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


def _on_proxy_mode_changed(owner, notice, sender):
    """The share only means something while there is a proxy."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    grp = chunk_obj.get_param("Viewport Proxy")
    _set_hidden(grp.get_param("proxy_share") if grp else None, owner.value == "Off")


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


# What a scatter reads besides its params, for the cook cache (cook_cache.py): the params
# are in its key already, these are the prims they name -- read off the same upstream stage
# a cook reads, the same way each reader does, so a change any reader would see misses.
_POINT_ARRAYS = ("positions", "orientations", "scales", "protoIndices", "points", "normals",
                 "widths", "curveVertexCounts", "faceVertexCounts", "faceVertexIndices")


def _hash_prims(stage, paths, prune_instancers=False, primvar=None) -> str:
    """One digest of every prim under *paths* a reader here reads: its type, world
    transform and point arrays, and *primvar* where it has it. *prune_instancers* skips an
    instancer's prototypes, as Grow Near does; Keep Away From reads them too."""
    h = hashlib.blake2b(digest_size=16)
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    for path in paths:
        root = stage.GetPrimAtPath(path) if path else None
        h.update(f"|{path}:{bool(root)}".encode())
        if not root:
            continue
        it = iter(Usd.PrimRange(root))
        for prim in it:
            if not prim.IsA(UsdGeom.PointBased) and not prim.IsA(UsdGeom.PointInstancer):
                continue
            if prune_instancers and prim.IsA(UsdGeom.PointInstancer):
                it.PruneChildren()
            h.update(f"|{prim.GetPath()}:{prim.GetTypeName()}".encode())
            h.update(np.asarray(cache.GetLocalToWorldTransform(prim), dtype=np.float64).tobytes())
            for name in _POINT_ARRAYS:
                attr = prim.GetAttribute(name)
                if attr and attr.HasValue():
                    h.update(name.encode())
                    h.update(np.asarray(attr.Get()).tobytes())
            if primvar and (pv := UsdGeom.PrimvarsAPI(prim).GetPrimvar(primvar)) and pv.HasValue():
                h.update(f"{primvar}:{pv.GetInterpolation()}".encode())
                h.update(np.asarray(pv.Get()).tobytes())
                if pv.IsIndexed():
                    h.update(np.asarray(pv.GetIndices()).tobytes())
    return h.hexdigest()


def _layer_stamps(prim) -> list:
    """Size and mtime of every file the prim's references and payloads reach, transitively --
    its asset file, the files that one references, and on. A re-exported asset misses.

    Not the stage's own layers: the network file is one, and saving it would change the key
    of everything in it. What a cook reads from those is hashed by _hash_prims instead."""
    local = {layer.identifier for layer in prim.GetStage().GetLayerStack(includeSessionLayers=True)}
    seen, stamps = set(local), []
    pending = [arc.GetTargetLayer() for arc in Usd.PrimCompositionQuery(prim).GetCompositionArcs()
               if arc.GetArcType() in (Pcp.ArcTypeReference, Pcp.ArcTypePayload)]
    while pending:
        layer = pending.pop()
        if layer is None or layer.identifier in seen:
            continue
        seen.add(layer.identifier)
        if layer.realPath and os.path.exists(layer.realPath):
            stat = os.stat(layer.realPath)
            stamps.append((layer.realPath, stat.st_size, stat.st_mtime_ns))
        for asset in layer.GetCompositionAssetDependencies():
            child = Sdf.Layer.FindOrOpen(layer.ComputeAbsolutePath(asset))
            if child is not None:
                pending.append(child)
    return sorted(stamps)


def cache_key(node):
    """The cook cache's key beyond the params: the surface, its density field, what Keep
    Away From and Grow Near read, the painted points, and each prototype's asset files."""
    stage = node.get_stage()
    if not stage:
        return None
    scratch = chunk_stage.get_upstream_stage(stage, node.get_scene(), node)
    paths = lambda name: [p.strip() for p in (node.get_param(name).value or "").split(",") if p.strip()]  # noqa: E731
    surfaces = node.get_param("surface_prim").resolve_paths()[:1]
    return (
        ("surface", _hash_prims(scratch, surfaces, primvar=node.get_param("density_primvar").value or None)),
        ("avoid", _hash_prims(scratch, paths("avoid_prims"))),
        ("near", _hash_prims(scratch, paths("near_prims"), prune_instancers=True)),
        ("placed", _hash_prims(scratch, paths("points_prim"))),
        ("prototypes", tuple(
            (p.src_path, repr(p.variants), _layer_stamps(scratch.GetPrimAtPath(p.src_path)))
            for p in _resolve_prototypes(node, scratch) if scratch.GetPrimAtPath(p.src_path)
        )),
    )


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
    # scipy is a requirement, not an extra: without its KD-tree the spacing tests run in
    # Python, and a full scene cooks for minutes -- while Blender's install prompt, which
    # waits for an idle moment, cannot appear. Place nothing until it is installed; the
    # prompt recooks this chunk after a yes.
    if find_spec("scipy") is None:
        if readout := node.get_param("readout"):
            readout.value = "needs scipy -- install it when asked, or pip install scipy"
        logger.warning("Scatter Points %s placed nothing: scipy is not installed", node.name)
        return

    stage = node.get_stage()

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    surface_paths = node.get_param("surface_prim").resolve_paths()

    # `.value` is already typed and defaults when unset -- no cast/`or` needed. inst_path
    # keeps its fallback because an empty path (user cleared the field) is invalid here.
    inst_path = node.get_param("inst_path", is_s).value or "/ScatterInstancer"
    scatter_mode = node.get_param("scatter_mode", is_s).value
    max_count = max(1, int(node.get_param("max_count", is_i).value))
    density_per_m2 = node.get_param("density", is_f).value if node.get_param("density", is_f) else 0.0
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
    sampling = node.get_param("sampling").value if node.get_param("sampling") is not None else "Darts"
    avoid_paths = [p.strip() for p in node.get_param("avoid_prims", is_s).value.split(",") if p.strip()]
    avoid_distance = max(0.0, node.get_param("avoid_distance", is_f).value)
    normal_offset = node.get_param("normal_offset", is_f).value
    orient_mode = node.get_param("orient_mode", is_s).value
    align_up = node.get_param("align_up", is_s).value if node.get_param("align_up", is_s) else "Off"
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
    proxy_mode = node.get_param("proxy_mode", is_s).value
    proxy_share = min(1.0, max(0.0, node.get_param("proxy_share", is_f).value))

    layer = node.get_edit_context()
    if not layer:
        return

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
    # A path given that names no prim: place nothing rather than the no-surface preview grid.
    surface_text = node.get_param("surface_prim", is_s).value.strip()
    surface_missing = surface_text if surface_text and not surface_prim else ""

    _migrate_legacy_proto(node)
    missing: list[str] = []
    specs = _resolve_prototypes(node, scratch_stage, missing)
    # A script's chunk cooks once before it is wired (chunk_scene.place), when the surface
    # and prototypes it names are above it in a moment but not yet: scattering then placed
    # nothing, and said "surface prim not found" for a network that was right -- every
    # benchmark agent saw it and some went looking for the fault. Wait for the wire; a prim
    # still missing once wired is reported as before.
    if (surface_missing or missing) and getattr(node, "_wiring_pending", False):
        return

    triangles = _collect_triangles(surface_prim)
    # None whenever the surface carries no painted weight, so an unpainted scatter never pays
    # for the lookup -- see _density_mask.
    density = _density_mask(surface_prim, node.get_param("density_primvar").value)
    rng = random.Random(seed)
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    world_up = Gf.Vec3f(0, 1, 0) if up_idx == 1 else Gf.Vec3f(0, 0, 1)
    lat_a, lat_b = [i for i in range(3) if i != up_idx]
    axis_vec = [Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0), Gf.Vec3f(0, 0, 1)]

    # Grow Near: weight each candidate by how near it is to what an earlier layer placed --
    # under its canopy, around its base, out of its shade. Measured across the ground, since
    # an instancer's points sit at the base of a trunk. Multiplied into the Density Field.
    near_paths = [p.strip() for p in node.get_param("near_prims", is_s).value.split(",") if p.strip()]
    near = None
    near_outside = node.get_param("near_outside", is_f).value
    if near_paths:
        near_pts, near_scales = proximity.source_points(scratch_stage, near_paths)
        near = proximity.Proximity(
            near_pts, near_scales if node.get_param("near_scaled", is_b).value else None,
            radius=max(0.0, node.get_param("near_radius", is_f).value),
            clear=max(0.0, node.get_param("near_clear", is_f).value),
            softness=node.get_param("near_softness", is_f).value,
            inside=node.get_param("near_inside", is_f).value,
            outside=near_outside,
            up=tuple(world_up),
        )
    mask = _combine_masks(density, near.sample if near is not None else None)

    # Read here, used at the dispatch below. Nothing about a painted cloud depends on the
    # prototypes, so it does not need to wait for them.
    points_input = node.get_param("points_prim").value
    placed, placed_normals = _painted_points(scratch_stage, points_input, world_up)
    sizes = None  # scales the sampler already drew, when spacing follows scale

    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)
        protos = _author_prototypes(
            stage, inst_path, specs, scratch_stage, hide_sources=bool(node.get_param("hide_sources").value),
            drop_purpose=UsdGeom.Tokens.proxy if proxy_mode != "Off" else None,
            foreign_layers=_other_chunk_layers(layer),
        )
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
            positions, normals = _fill_in_volume(triangles, max_count, rng, spacing, mask=mask)
            normals = [world_up] * len(positions)
            levels = [0] * len(positions)
        else:
            # Surface -- slope pre-filters the triangle set (correct area weighting,
            # no wasted samples). The paint mask, being per-point, will instead be
            # passed as the `mask=` hook above.
            surf_tris = triangles.select(triangles.slope_mask(world_up, max_slope))
            if near is not None and near_outside <= 0.0 and len(surf_tris):
                # Grow Near with nothing beyond reach: a triangle no source reaches can never
                # take an instance, so it is left out of the draw rather than sampled and
                # rejected. Trees grown near a shot's centre on a 500 m terrain drew six
                # candidates for every one that could land.
                v0, v1, v2 = (np.asarray(v, dtype=np.float64) for v in (surf_tris.v0, surf_tris.v1, surf_tris.v2))
                centre = (v0 + v1 + v2) / 3.0
                size = np.max([np.linalg.norm(v - centre, axis=1) for v in (v0, v1, v2)], axis=0)
                surf_tris = surf_tris.select(near.within_reach(centre, size))
            if density_per_m2 > 0.0:
                # Density: the count is per square metre of the faces left to scatter on, so
                # it follows the surface's size and Max Slope instead of being worked out by
                # hand. A painted Density Field still thins it; Spacing can still cap it.
                metres = UsdGeom.GetStageMetersPerUnit(stage)
                area_m2 = float(np.sum(surf_tris.areas())) * metres * metres if len(surf_tris) else 0.0
                max_count = int(round(density_per_m2 * area_m2))
            avoid = None
            if avoid_paths:
                pts_, radii_ = _avoid_points(scratch_stage, avoid_paths, max(avoid_distance, spacing, 1e-6) * 0.5)
                avoid = _AvoidSet(pts_, avoid_distance, radii_, extra_max=spacing * 0.5)
                if avoid._dist <= 0.0:
                    avoid = None
            if len(surf_tris) and sampling == "Tiles" and spacing > 0:
                # Sampling: Tiles -- the same rules from a laid blue-noise tile (opt-in: a
                # different layout).
                positions, normals, drawn = _scatter_tiles(
                    surf_tris, max_count, rng, spacing, random.Random(seed + 13),
                    scale_min, scale_max, scale_bias, mask=mask, avoid=avoid, vary=size_spacing,
                    up_idx=up_idx)
                sizes = drawn
            elif len(surf_tris) and (size_spacing or avoid is not None):
                # Spacing Follows Scale / Keep Away From: the scale is drawn with the point,
                # so the spacing and the clearance can both take the instance's size.
                positions, normals, drawn = _scatter_sized(
                    surf_tris, max_count, rng, spacing, random.Random(seed + 13),
                    scale_min, scale_max, scale_bias, mask=mask, avoid=avoid, vary=size_spacing)
                sizes = drawn
            elif len(surf_tris):
                positions, normals = _scatter_on_triangles(surf_tris, max_count, rng, spacing, mask=mask)
            elif surface_missing:
                positions, normals = [], []
            else:
                # No mesh / no qualifying faces -- fall back to a jittered grid.
                sp = max(spacing, 2.0)
                n = int(math.sqrt(max_count))
                off = (n - 1) * sp / 2
                # On the ground plane of the stage's own up axis: X/Y when Z is up, X/Z when Y
                # is. Two draws per point in the same order, so a Z-up grid is unchanged.
                positions = []
                for x in range(n):
                    for y in range(n):
                        a = x * sp - off + rng.uniform(-sp * 0.3, sp * 0.3)
                        b = y * sp - off + rng.uniform(-sp * 0.3, sp * 0.3)
                        positions.append(axis_vec[lat_a] * a + axis_vec[lat_b] * b)
                normals = [world_up] * len(positions)
            levels = [0] * len(positions)

        count = len(positions)

        # Exact mix: quota per prototype, then shuffled so the shares are spread over the
        # scatter instead of arriving in blocks.
        # A numpy shuffle: random.shuffle, an element at a time, was a sixth of a big cook.
        quota = _quota([p.weight for p in protos], count)
        assignment = np.zeros(count, dtype=np.int64)
        assignment[:sum(quota)] = np.repeat(np.arange(len(quota)), quota)
        np.random.default_rng(seed + 31).shuffle(assignment)

        scale_rng = random.Random(seed + 13)
        orient_rng = random.Random(seed + 7)
        tilt_rng = random.Random(seed + 19)

        out_positions = Vt.Vec3fArray(count)
        out_scales = Vt.Vec3fArray(count)
        out_orients = Vt.QuathArray(count)
        out_indices = Vt.IntArray(count)

        level_step = height + pack_level_gap

        fast = (count and scatter_mode != "Stack" and not placed and not sit_on_surface
                and orient_mode in ("Up", "Keep Original") and abs(normal_offset) <= 1e-9)
        if fast:
            # The common case -- every instance upright, spun at random -- as arrays: the
            # loop's per-instance Gf quaternions were a tenth of a 45k-grass cook. The spin is
            # the loop's, drawn differently, so the two paths don't match instance for instance.
            s_all = sizes if sizes else [_draw_scale(scale_rng, scale_min, scale_max, scale_bias)
                                         for _ in range(count)]
            out_positions = Vt.Vec3fArray.FromNumpy(np.asarray(positions, dtype=np.float32).reshape(-1, 3))
            out_indices = Vt.IntArray.FromNumpy(np.asarray(assignment, dtype=np.int32))
            out_orients = Vt.QuathArray.FromNumpy(
                _spun_quats(count, rand_axis, rand_amount, orient_rng).astype(np.float16))
            mult = np.asarray([p.scale_mult for p in protos], dtype=np.float64)[assignment]
            per = np.asarray([tuple(p.scale) for p in protos], dtype=np.float64)[assignment]
            sm = np.asarray(s_all, dtype=np.float64) * mult
            out_scales = Vt.Vec3fArray.FromNumpy((sm[:, None] * per).astype(np.float32))

        # Turned to the surface, pushed off it, or painted: the loop's per-instance draws, in
        # arrays. Only Stack and Sit on Surface still need the loop below.
        arrays = bool(count) and not fast and scatter_mode != "Stack" and not sit_on_surface
        if arrays:
            if placed:
                # Seeded per point, as the loop does: see there.
                scale_rngs = [random.Random(seed + 13 + i) for i in range(count)]
                orient_rng = [random.Random(seed + 7 + i) for i in range(count)]
            else:
                scale_rngs = itertools.repeat(scale_rng, count)
            s_all = sizes if sizes else [_draw_scale(r, scale_min, scale_max, scale_bias) for r in scale_rngs]
            normals_np = _vectors(normals, count)
            positions_np = _vectors(positions, count).astype(np.float32)
            if abs(normal_offset) > 1e-9:
                # As the loop: the offset rounded to float32, then a float32 sum.
                positions_np = positions_np + (normals_np * normal_offset).astype(np.float32)
            out_positions = Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(positions_np))
            out_indices = Vt.IntArray.FromNumpy(np.asarray(assignment, dtype=np.int32))
            quats = _orientations(count, orient_mode, normals_np, protos, assignment, world_up,
                                  rand_axis, rand_amount, orient_rng, align_up)
            # Through float32 as the loop's Gf.Quatf did, then half, (i, j, k, real) for Vt.
            out_orients = Vt.QuathArray.FromNumpy(
                np.ascontiguousarray(quats[:, [1, 2, 3, 0]].astype(np.float32).astype(np.float16)))
            mult = np.asarray([p.scale_mult for p in protos], dtype=np.float64)[assignment]
            per = np.asarray([tuple(p.scale) for p in protos], dtype=np.float64)[assignment]
            sm = np.asarray(s_all, dtype=np.float64) * mult
            out_scales = Vt.Vec3fArray.FromNumpy((sm[:, None] * per).astype(np.float32))

        # Rest on Surface with Align to Normal: every turn in one call before the loop. The
        # orient generator is only drawn from for the turn here, so the draws are the loop's.
        loop_quats = None
        if count and not (fast or arrays) and scatter_mode != "Stack" and orient_mode == "Align to Normal":
            loop_rngs = [random.Random(seed + 7 + i) for i in range(count)] if placed else orient_rng
            loop_quats = _orientations(count, orient_mode, _vectors(normals, count), protos, assignment,
                                       world_up, rand_axis, rand_amount, loop_rngs, align_up)

        for i in range(0 if fast or arrays else count):
            pidx = int(assignment[i])
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
                    # Lean about a random lateral axis -- a pile that isn't machine-perfect.
                    tilt_axis = Gf.Cross(world_up, _random_lateral(tilt_rng, lat_a, lat_b, axis_vec))
                    quat = _quat_mul(_axis_quat(tilt_axis, math.radians(tilt_rng.uniform(0.0, pack_tilt))), quat)
                quat = _apply_random_rotation(quat, rand_axis, rand_amount, orient_rng)
            elif orient_mode == "Align to Normal":
                w, x, y, z = loop_quats[i]
                quat = Gf.Quatf(float(w), float(x), float(y), float(z))
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

        if proxy_mode != "Off":
            # The viewport draws proxy, a final render draws render: usdview shows the
            # purpose=proxy instancer by default, V-Ray and friends render this one. The
            # subset is seeded and kept in order, so it is stable while other params change.
            instancer.CreatePurposeAttr().Set(UsdGeom.Tokens.render)
            keep = sorted(random.Random(seed + 43).sample(range(count), round(count * proxy_share)))
            _author_proxy(stage, f"{inst_path}_proxy", proxy_mode, protos, specs, scratch_stage,
                          keep, (out_positions, out_indices, out_scales, out_orients))

    triangles = sum(q * _triangle_count(stage.GetPrimAtPath(p.path)) for p, q in zip(protos, quota) if q)
    _report(node, protos, quota, count, scatter_mode, pack_levels, missing, triangles, surface_missing)


def _random_lateral(rng, lat_a, lat_b, axis_vec):
    """A random unit direction in the ground plane (for tilt axes)."""
    ang = rng.uniform(0.0, 2.0 * math.pi)
    return axis_vec[lat_a] * math.cos(ang) + axis_vec[lat_b] * math.sin(ang)


#: Expanded triangles past which the readout warns. Aurora crashed on 22 pine saplings, 45M.
_TRIANGLE_BUDGET = 20_000_000


def _triangle_count(prim) -> int:
    """Triangles in the meshes under prim that render: guide and proxy prims are left out."""
    if not prim:
        return 0
    total = 0
    skip = (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy)
    it = iter(Usd.PrimRange(prim, Usd.TraverseInstanceProxies()))
    for p in it:
        imageable = UsdGeom.Imageable(p)
        if imageable and imageable.GetPurposeAttr().Get() in skip:
            it.PruneChildren()
            continue
        if p.IsA(UsdGeom.Mesh):
            counts = UsdGeom.Mesh(p).GetFaceVertexCountsAttr().Get()
            if counts:
                counts = np.asarray(counts)
                total += int(counts.sum()) - 2 * len(counts)
    return total


def _millions(n: int) -> str:
    """A triangle count for the readout: 45200000 -> "45.2M"."""
    return f"{n / 1e6:.1f}M" if n >= 100_000 else f"{n:,}"


def _report(node, protos, quota, count, mode, levels, missing=(), triangles=0, surface_missing=""):
    """Fill the readout label + the output params other chunks can read.

    Prototype paths that matched nothing are named, and logged: a typo in one row otherwise
    passes for a working scatter. When no row resolved, the first line says the default cube
    is being drawn, since a readout of "7000 instances" alone looks like success. A surface
    path that names no prim is named and logged the same way.

    The triangles all the instances add up to are given too, with a warning past
    _TRIANGLE_BUDGET: a renderer that runs out of memory gives no hint why.
    """
    if count and protos:
        mix = "\n".join(
            f"{p.label} {q * 100.0 / count:.0f}% ({q})" for p, q in zip(protos, quota) if q
        )
    else:
        mix = ""
    suffix = f", {levels} levels" if mode == "Stack" and levels > 1 else ""
    fallback = len(protos) == 1 and protos[0].label == _FALLBACK_LABEL
    head = f"{count} instances · {mode}{suffix}" + (" · no prototype found, drawing the default cube" if fallback else "")
    text = head if mix else f"{head}\nnothing placed — check the surface mesh and prototype paths"
    if triangles:
        text += f"\n{_millions(triangles)} triangles expanded"
    if triangles > _TRIANGLE_BUDGET:
        text += f", over {_millions(_TRIANGLE_BUDGET)}: a renderer may run out of memory"
        logger.warning(
            "%s: %s triangles expanded, over %s; a renderer may run out of memory. Use fewer "
            "instances or a lighter prototype (Poly Reduce).",
            node.name, _millions(triangles), _millions(_TRIANGLE_BUDGET),
        )
    if surface_missing:
        text += f"\nsurface not found: {surface_missing}"
        logger.warning("%s: surface prim not found: %s", node.name, surface_missing)
    if missing:
        text += "\nnot found: " + ", ".join(missing)
        logger.warning("%s: prototype path(s) not found: %s", node.name, ", ".join(missing))

    if readout := node.get_param("readout"):
        readout.value = text
    if mix_readout := node.get_param("mix_readout"):
        mix_readout.value = mix
    if out_count := node.get_param("out_count"):
        out_count.value = count
    if out_protos := node.get_param("out_prototypes"):
        out_protos.value = len(protos)

    logger.debug("%s | %s", text.replace(chr(10), " | "), mix.replace(chr(10), "  "))


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
    """Collapse a group by default -- the chunk is tall, so only the target is open."""
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
        # PRIM_PATHS: a cook reads these points, so a new stroke must re-run it.
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_PATHS,
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
        flags=_GF | chunk.ChunkFlags.GroupFlags.LIST,  # foldable, open by default
        tooltip="One row per prototype. Mix % is relative — the rows are normalised, so "
        "75/25 and 3/1 mean the same thing.",
    )
    protos.add_param(
        chunk.LineEdit(
            "proto_path",
            "Prim",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="Prim to instance. Globs work (/props/crayon_*), %name takes every member of "
            "the collection called name (%trees), and a variant selection can be written into "
            "the path:\n"
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
    chunk_api.add_param(
        chunk.Checkbox(
            "hide_sources",
            "Hide Sources",
            default_value=True,
            tooltip="Deactivate the prims the prototypes come from, so only the scattered "
            "instances are drawn -- a library of assets to pick from stays out of the render "
            "(and its materials out of the renderer). A source another chunk writes inside is "
            "made invisible instead, so that chunk can still cook. Turn off to keep the "
            "originals visible when scattering copies of objects placed in the scene.",
        )
    )

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
            tooltip="What the last cook produced.",
        )
    )
    # How the mix landed, one line per prototype: a glob row expands into dozens, which ran
    # the full height of the node, so it folds away and starts folded.
    mix_grp = chunk.Group("Mix", flags=_GF)
    mix_grp.add_param(
        chunk.Label(
            "mix_readout",
            default_value="",
            tooltip="Each prototype's share of the last cook, and its instance count.",
        )
    )
    chunk_api.add_param(_collapsed(mix_grp))

    # --- Distribution ---
    dist_grp = chunk.Group("Distribution", icon="scatter", flags=_GF)
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
        chunk.Menu(
            "sampling",
            "Sampling",
            menu_items=["Darts", "Tiles"],
            default_value="Darts",
            tooltip="Surface mode, with Spacing set. Darts places instances a batch at a time, "
            "each held to every one before it. Tiles lays one blue-noise tile across the "
            "ground and drops it onto the surface -- the same spacing, many times faster on "
            "big terrains, read from above (a terrain, not a cave). A different layout: "
            "switching moves every instance.",
        )
    )
    dist_grp.add_param(
        chunk.LineEdit(
            "avoid_prims",
            "Keep Away From",
            # PRIM_PATHS: a cook reads these prims, so an edit to them must re-run it.
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_PATHS,
            tooltip="Surface mode: prims whose points (curves along their length) instances "
            "stay clear of -- e.g. a Trails output. Comma-separate several.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "avoid_distance",
            "Keep Away Distance",
            default_value=0.0,
            tooltip="How far an instance's edge stays from Keep Away From -- from a curve's "
            "edge where it has widths.",
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
        chunk.Slider(
            "density",
            "Density (per m²)",
            min_value=0.0,
            max_value=100.0,
            default_value=0.0,
            tooltip="Surface mode: instances per square metre of the faces within Max Slope, "
            "in place of Max Count. 0 = use Max Count. Spacing may place fewer if the surface "
            "fills up first.",
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

    # --- Grow Near (ecosystem affinity / repulsion) ---
    near_grp = chunk.Group("Grow Near", icon="sprout", flags=_GF)
    near_grp.add_param(
        chunk.LineEdit(
            "near_prims",
            "Near",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="Surface/Volume: what this layer grows in relation to -- usually an earlier "
            "Scatter Points' instancer. Comma-separate several. Empty: off.",
        )
    )
    near_grp.add_param(chunk.Slider("near_radius", "Radius", min_value=0.0, max_value=20.0, default_value=3.0,
                                    tooltip="How far each of them reaches, scene units -- a canopy's radius."))
    near_grp.add_param(chunk.Slider("near_clear", "Clear Radius", min_value=0.0, max_value=5.0, default_value=0.0,
                                    tooltip="Nothing within this of them -- the trunk. 0 = no clearing."))
    near_grp.add_param(chunk.Slider("near_inside", "Inside", min_value=0.0, max_value=1.0, default_value=1.0,
                                    tooltip="Density near them. 1 draws this layer in (understory)."))
    near_grp.add_param(chunk.Slider("near_outside", "Outside", min_value=0.0, max_value=1.0, default_value=0.1,
                                    tooltip="Density beyond Radius. Above Inside pushes this layer away (shade)."))
    near_grp.add_param(chunk.Slider("near_softness", "Softness", min_value=0.0, max_value=1.0, default_value=0.5,
                                    tooltip="How much of the Radius fades out, from the edge in. 0 = a hard edge."))
    near_grp.add_param(chunk.Checkbox("near_scaled", "Scale With Source", default_value=True,
                                      tooltip="Radius and Clear Radius grow with each instance's scale."))
    chunk_api.add_param(_collapsed(near_grp))

    # --- Stacking (Stack mode only) ---
    stack_grp = chunk.Group("Stacking", icon="layers", flags=_GF)
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
    region_grp = chunk.Group("Region", icon="grid", flags=_GF)
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
    var_grp = chunk.Group("Variation", icon="shuffle", flags=_GF)
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
        chunk.Menu(
            "align_up",
            "Up Along Surface",
            menu_items=["Off", *_ALIGN_UP_AXES],
            default_value="Off",
            tooltip="Align to Normal only. Which prototype axis points up the surface, so on a wall\n"
            "it points straight up and on a slope uphill. A strand modelled hanging along -X\n"
            "takes +X: it then hangs down whatever wall it is on. Off: the prototype is\n"
            "only tipped onto the surface, and how it is turned on it depends on which\n"
            "way the surface faces. Surfaces facing straight up or down are left as Off.",
        )
    )
    var_grp.get_param("orient_mode").add_callback(_on_orient_changed)
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

    # --- Viewport proxy ---
    proxy_grp = chunk.Group("Viewport Proxy", icon="eye", flags=_GF)
    proxy_mode = chunk.Menu(
        "proxy_mode",
        "Proxy",
        menu_items=_PROXY_MODES,
        default_value="Off",
        tooltip="A lighter stand-in for the viewport, beside the full scatter.\n"
        "Off: one instancer, drawn everywhere.\n"
        "Bounds: the proxy draws each instance as its prototype's bounding box.\n"
        "Full: the proxy draws the real prototypes, just fewer of them.\n"
        "When on, the full instancer gets purpose=render and a sibling <Instancer Path>_proxy "
        "gets purpose=proxy -- a viewport showing proxy (usdview's default) draws that one, a "
        "final render draws the full one.",
    )
    proxy_mode.add_callback(_on_proxy_mode_changed)
    proxy_grp.add_param(proxy_mode)
    proxy_share = chunk.Slider(
        "proxy_share",
        "Proxy Share",
        default_value=0.1,
        min_value=0.0,
        max_value=1.0,
        tooltip="Fraction of the instances the proxy keeps -- a seeded, stable subset. 1 keeps "
        "them all (useful with Bounds).",
    )
    proxy_share.set_state(chunk.ChunkFlags.State.HIDDEN)  # Off is the default
    proxy_grp.add_param(proxy_share)
    chunk_api.add_param(_collapsed(proxy_grp))

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
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="scatter",
        # The mesh maths runs on arrays -- see `_TriangleSoup` for what it replaced and why.
        # scipy's KD-tree is what keeps spacing and Grow Near fast: without it the fallbacks
        # test each candidate against every placed point in Python, which on a full road
        # scene ran for minutes in Blender (whose Python had no scipy) instead of seconds.
        requirements=["numpy>=2,<3", "scipy>=1.11,<2"],
        # Placing a forest is minutes; reading it back is a layer. cache_key names what it
        # reads besides its params.
        cache_cook=True,
        # A new scatter samples with Tiles; a saved one keeps the Darts it was made with (the
        # param's default, so its layout doesn't move on reopen).
        new_values={"sampling": "Tiles"},
        description=(
            "Scatters instances of one or more prototypes across a mesh as a PointInstancer.\n"
            "\n"
            "Prototypes are a list: each row takes a prim (globs and %collection allowed), a share of the mix\n"
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
            "How many: Max Count [max_count], or in Surface mode Density (per m²) [density],\n"
            "instances per square metre of the faces within Max Slope [max_slope]. Seed [seed]\n"
            "picks the pattern. Auto Spacing keeps instances apart and may place fewer.\n"
            "\n"
            "Feed the instancer path to the RBD Solver to settle a loose scatter physically."
        ),
    )
