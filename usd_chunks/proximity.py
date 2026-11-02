"""Proximity weights: 0..1 by how near a point is to a set of source points.

Ecosystem affinity / repulsion. Shared by Scatter Points' Grow Near (a weight
per candidate point) and the Proximity Field chunk (a weight per mesh vertex, as a primvar):
**inside** near a source, **outside** beyond its radius, a smooth fade between, and nothing
within a **clear** radius of it -- the trunk.

Requires: pxr (OpenUSD), numpy.
"""

from __future__ import annotations

import numpy as np
from pxr import Usd, UsdGeom

from usd_chunks import kd_query
from usd_chunks.cell_runs import CellRuns, segment_starts

# A cell (x, y, z) as one integer, so cells sort and look up as a flat array. Signed digits,
# so a neighbour's id is the cell's id plus the offset's -- and two cells only collide past a
# million cells out, which can only add a far source to a neighbourhood, never drop one.
_STRIDE = 1 << 21


def _cell_ids(cells):
    return (cells[..., 0] * _STRIDE + cells[..., 1]) * _STRIDE + cells[..., 2]


def _ring(ring):
    """The ids of the cell offsets within *ring* cells, the cell itself included."""
    span = np.arange(-ring, ring + 1, dtype=np.int64)
    return _cell_ids(np.stack(np.meshgrid(span, span, span, indexing="ij"), axis=-1).reshape(-1, 3))


_OFFSETS = _ring(1)



def _smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class Proximity:
    """The weight around (M, 3) *sources*, built once and sampled at any number of points.

    *scales* (M,) multiplies each source's *radius* and *clear*. *up*, a unit vector, makes
    distances lateral (across the ground): an instancer's points sit at the base of a trunk,
    but its canopy is overhead. The nearest-reaching source wins, so overlapping canopies
    don't add up past *inside*.
    """

    def __init__(self, sources, scales=None, *, radius, clear=0.0, softness=0.5,
                 inside=1.0, outside=0.0, up=None):
        sources = np.asarray(sources, dtype=np.float64).reshape(-1, 3)
        scales = np.ones(len(sources)) if scales is None else np.asarray(scales, dtype=np.float64)
        self._up = None if up is None else np.asarray(up, dtype=np.float64)
        self._sources = self._flatten(sources)
        self._r = np.maximum(radius * scales, 0.0)
        self._c = np.maximum(clear * scales, 0.0)
        self._soft = max(float(softness), 1e-6)
        self._inside, self._outside = float(inside), float(outside)
        # A source reaches at most one cell, so a point only looks at its 27 neighbours.
        self._cell = float(max(self._r.max(initial=0.0), self._c.max(initial=0.0)))
        # The sources by cell: a neighbourhood is then a handful of runs of one sorted order.
        self._runs = None
        if self._cell > 1e-9 and len(self._sources):
            self._runs = CellRuns(_cell_ids(np.floor(self._sources / self._cell).astype(np.int64)))
        # One radius and one clear radius for every source: the weight then only falls with
        # distance, so the nearest source alone decides it (_nearest).
        self._uniform = bool(len(self._r)) and np.ptp(self._r) == 0.0 and np.ptp(self._c) == 0.0
        self._tree = None  # _nearest's KD-tree, built on first use; False without scipy

    def _flatten(self, p):
        return p if self._up is None else p - np.outer(p @ self._up, self._up)

    def _nearest(self, points, bound):
        """(distance, source) of each of (N, 3) flattened *points*' nearest source within
        *bound*, the source len(sources) where there is none -- or None when the sources'
        radii differ, or scipy is not there, and every source in reach has to be weighed.

        A road's vertices put hundreds of sources in reach of every candidate; weighing them
        all was most of a Grow Near edit on a verge, where one KD query answers the same.
        """
        if not self._uniform or (tree := self._kdtree()) is None:
            return None
        return tree.query(points, k=1, distance_upper_bound=bound)

    def _kdtree(self):
        """The sources' KD-tree, built on first use; None without scipy."""
        if self._tree is None:
            try:
                from scipy.spatial import cKDTree
            except ImportError:  # scipy is optional: every source in reach is weighed instead
                self._tree = False
            else:
                self._tree = cKDTree(self._sources)
        return self._tree or None

    def _pairs_in_reach(self, points, bound):
        """(rows, sources): every source closer than *bound* to each of (N, 3) flattened
        *points*, rows ascending -- or None without scipy.

        What the cell neighbourhoods give, from one KD query: each point's nearest k within
        *bound*, asked again with a larger k only for the points that filled all k. Over
        321k valley pines the neighbourhood lookup (a sort and a binary search per batch) was
        most of YoungValley's Grow Near; this is a C walk per point.
        """
        if (tree := self._kdtree()) is None:
            return None
        rows, near = [np.zeros(0, dtype=np.int64)], [np.zeros(0, dtype=np.int64)]
        idx, k, n = np.arange(len(points)), 8, tree.n
        while len(idx):
            k = min(k, n)
            d, j = kd_query.query(tree, points[idx], k=k, distance_upper_bound=bound)
            if k == 1:
                d, j = d[:, None], j[:, None]
            hit = np.isfinite(d)
            more = hit[:, -1] if k < n else np.zeros(len(idx), dtype=bool)
            r, c = np.nonzero(hit[~more])
            rows.append(idx[~more][r])
            near.append(j[~more][r, c])
            idx, k = idx[more], k * 2
        rows, near = np.concatenate(rows), np.concatenate(near)
        order = np.argsort(rows, kind="stable")
        return rows[order], near[order]

    def _neighbourhoods(self, points, offsets):
        """The sources around each of (N, 3) *points*, as CellRuns.lookup gives them."""
        return self._runs.lookup(_cell_ids(np.floor(points / self._cell).astype(np.int64)), offsets)

    def _weights(self, d, near):
        """(g, h) at distances *d* from the sources *near* (same shape): g, how far inside
        each source's radius; h, how clear of its trunk."""
        r, c = self._r[near], self._c[near]
        with np.errstate(divide="ignore", invalid="ignore"):
            # 1 inside radius x (1 - softness), 0 past radius, smooth between.
            g = np.where(r > 0, _smoothstep((r - d) / (r * self._soft)), 0.0)
            # 0 on the trunk, back to 1 over the outer quarter of the clear radius.
            h = np.where(c > 0, _smoothstep((d - 0.75 * c) / (0.25 * c)), 1.0)
        return g, h

    def sample_many(self, points):
        """The weight at (N, 3) *points*, in 0..1.

        Each point against the sources in its cell's 27 neighbours, as flat (point, source)
        pairs -- a Python loop per cell of points was most of a Grow Near scatter's cook,
        thousands of cells a batch on a 500 m terrain -- except dense cells, taken whole.
        """
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        out = np.full(len(points), self._outside)
        if not len(points) or self._runs is None:
            return np.clip(out, 0.0, 1.0)
        points = self._flatten(points)
        span = self._inside - self._outside
        if (nearest := self._nearest(points, self._cell)) is not None:
            d, near = nearest
            found = near < len(self._sources)
            g, h = self._weights(d[found], near[found])
            out[found] = (self._outside + span * g) * h
            return np.clip(out, 0.0, 1.0)
        if (reach := self._pairs_in_reach(points, self._cell)) is not None:
            rows, near = reach
            if len(rows):
                g, h = self._weights(np.linalg.norm(points[rows] - self._sources[near], axis=1), near)
                first = segment_starts(rows)
                out[rows[first]] = (self._outside + span * np.maximum.reduceat(g, first)) * np.minimum.reduceat(h, first)
            return np.clip(out, 0.0, 1.0)
        inverse, lo, runs = self._neighbourhoods(points, _OFFSETS)
        sparse, whole = CellRuns.split(inverse, runs)
        for rows, u in whole:
            near = self._runs.members(lo[u], runs[u])
            g, h = self._weights(np.linalg.norm(points[rows, None, :] - self._sources[None, near, :], axis=2), near)
            out[rows] = (self._outside + span * g.max(axis=1)) * h.min(axis=1)
        for rows, near in self._runs.pairs(sparse, inverse, lo, runs):
            g, h = self._weights(np.linalg.norm(points[rows] - self._sources[near], axis=1), near)
            # Pairs come point by point, so each point's are one run.
            first = segment_starts(rows)
            out[rows[first]] = (self._outside + span * np.maximum.reduceat(g, first)) * np.minimum.reduceat(h, first)
        return np.clip(out, 0.0, 1.0)

    def within_reach(self, points, pad=None):
        """Whether each of (N, 3) *points* is within some source's radius, plus its own *pad*
        (N,) -- a triangle's centroid padded by its size answers "can anything on this
        triangle be reached". Only meaningful while *outside* is 0: beyond reach the weight is
        then 0, so a sampler can leave those triangles out entirely."""
        points = self._flatten(np.asarray(points, dtype=np.float64).reshape(-1, 3))
        pad = np.zeros(len(points)) if pad is None else np.asarray(pad, dtype=np.float64)
        out = np.zeros(len(points), dtype=bool)
        if not len(points) or self._runs is None:
            return out
        if (nearest := self._nearest(points, float(self._r[0] + pad.max()) * (1.0 + 1e-9))) is not None:
            d, near = nearest
            found = near < len(self._sources)
            out[found] = d[found] <= self._r[0] + pad[found]
            return out
        # Pads can exceed a cell, so look as many cells out as the largest one needs.
        ring = int(np.ceil((self._cell + float(pad.max())) / self._cell))
        inverse, lo, runs = self._neighbourhoods(points, _ring(ring))
        sparse, whole = CellRuns.split(inverse, runs)
        for rows, u in whole:
            near = self._runs.members(lo[u], runs[u])
            d = np.linalg.norm(points[rows, None, :] - self._sources[None, near, :], axis=2)
            out[rows] = (d <= self._r[near][None, :] + pad[rows, None]).any(axis=1)
        for rows, near in self._runs.pairs(sparse, inverse, lo, runs):
            d = np.linalg.norm(points[rows] - self._sources[near], axis=1)
            out[rows[d <= self._r[near] + pad[rows]]] = True
        return out

    def sample(self, point, normal=None):
        """The weight at one point -- the Scatter mask hook's signature; *normal* is unused."""
        return float(self.sample_many([[point[0], point[1], point[2]]])[0])


def field(points, sources, scales=None, **settings):
    """The weight at (N, 3) *points* from (M, 3) *sources*, in one call. See Proximity."""
    return Proximity(sources, scales, **settings).sample_many(points)


def source_points(stage, paths):
    """World-space points under *paths*, and each one's scale: a PointInstancer's positions
    with the mean of its per-instance scale, or any point-based prim's points at scale 1."""
    pts, scl = [], []
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    for path in paths:
        root = stage.GetPrimAtPath(path) if path else None
        if not root:
            continue
        it = iter(Usd.PrimRange(root))
        for prim in it:
            if prim.IsA(UsdGeom.PointInstancer):
                # Its prototypes are not sources themselves -- their points are local.
                it.PruneChildren()
                inst = UsdGeom.PointInstancer(prim)
                p = inst.GetPositionsAttr().Get()
                if not p:
                    continue
                p = np.asarray(p, dtype=np.float64)
                s = inst.GetScalesAttr().Get()
                s = (np.abs(np.asarray(s, dtype=np.float64)).mean(axis=1)
                     if s is not None and len(s) == len(p) else np.ones(len(p)))
            elif prim.IsA(UsdGeom.PointBased):
                p = UsdGeom.PointBased(prim).GetPointsAttr().Get()
                if not p:
                    continue
                p = np.asarray(p, dtype=np.float64)
                s = np.ones(len(p))
            else:
                continue
            m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
            pts.append(p @ m[:3, :3] + m[3, :3])
            scl.append(s)
    if not pts:
        return np.zeros((0, 3)), np.zeros(0)
    return np.concatenate(pts), np.concatenate(scl)
