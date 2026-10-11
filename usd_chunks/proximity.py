"""Proximity weights: 0..1 by how near a point is to a set of source points.

Geo-Scatter's ecosystem affinity / repulsion. Shared by Scatter Points' Grow Near (a weight
per candidate point) and the Proximity Field chunk (a weight per mesh vertex, as a primvar):
**inside** near a source, **outside** beyond its radius, a smooth fade between, and nothing
within a **clear** radius of it -- the trunk.

Requires: pxr (OpenUSD), numpy.
"""

from __future__ import annotations

import numpy as np
from pxr import Usd, UsdGeom

_OFFSETS = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]


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
        self._grid: dict = {}
        if self._cell > 1e-9:
            for i, k in enumerate(map(tuple, np.floor(self._sources / self._cell).astype(np.int64))):
                self._grid.setdefault(k, []).append(i)

    def _flatten(self, p):
        return p if self._up is None else p - np.outer(p @ self._up, self._up)

    def sample_many(self, points):
        """The weight at (N, 3) *points*, in 0..1."""
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        out = np.full(len(points), self._outside)
        if not len(points) or not self._grid:
            return np.clip(out, 0.0, 1.0)
        points = self._flatten(points)
        keys = np.floor(points / self._cell).astype(np.int64)
        uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
        inverse = inverse.reshape(-1)
        order = np.argsort(inverse, kind="stable")
        bounds = np.searchsorted(inverse[order], np.arange(len(uniq) + 1))
        for u, (kx, ky, kz) in enumerate(uniq.tolist()):
            near = [i for dx, dy, dz in _OFFSETS for i in self._grid.get((kx + dx, ky + dy, kz + dz), ())]
            if not near:
                continue
            rows = order[bounds[u]:bounds[u + 1]]
            near = np.asarray(near)
            d = np.linalg.norm(points[rows, None, :] - self._sources[None, near, :], axis=2)
            r, c = self._r[near], self._c[near]
            with np.errstate(divide="ignore", invalid="ignore"):
                # 1 inside radius x (1 - softness), 0 past radius, smooth between.
                g = np.where(r > 0, _smoothstep((r - d) / (r * self._soft)), 0.0)
                # 0 on the trunk, back to 1 over the outer quarter of the clear radius.
                h = np.where(c > 0, _smoothstep((d - 0.75 * c) / (0.25 * c)), 1.0)
            out[rows] = (self._outside + (self._inside - self._outside) * g.max(axis=1)) * h.min(axis=1)
        return np.clip(out, 0.0, 1.0)

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
