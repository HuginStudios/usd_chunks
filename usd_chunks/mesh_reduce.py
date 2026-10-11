"""Reduce a mesh to the fewest triangles that keep its shape, up to a cap.

A flat cabinet panel needs a handful of triangles and a fern thousands; one count for both
either wastes the budget on the panel or starves the fern. So a triangle budget here is a
*cap*, and `reduce_within` stops as soon as fewer triangles would visibly change the shape:
the surface may stray at most `max_error` (a fraction of the mesh's size) from the source.

Geometry only -- no UVs, subsets or primvars ride along, which is what a remeshed proxy
needs (its colour is baked onto the reduced vertices afterwards). Poly Reduce, which must
carry all of those, keeps its own decimation.

Needs numpy, scipy and fast-simplification, which the framework itself does not: only the
chunks that declare them import this module.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

try:
    import fast_simplification
    from scipy.spatial import cKDTree

    HAS_DEPS = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheels
    fast_simplification = cKDTree = None
    HAS_DEPS = False

# The fewest triangles a closed surface can have.
MIN_TRIANGLES = 4

# Decimation aggressiveness: 7 (fast_simplification's own default) cut slabs off a
# remeshed armchair at 200:1 (-13% volume); 5 loses ~1% and still reaches the count.
AGGRESSIVENESS = 5.0

# Source points the error is measured with, and how densely a candidate is sampled.
_PROBES = 20_000
_SAMPLES = 200_000


def simplify(points, triangles, count):
    """Quadric decimation of a triangle mesh to about `count` triangles: (points, triangles)."""
    count = int(max(MIN_TRIANGLES, min(int(count), len(triangles))))
    if count >= len(triangles):
        return np.asarray(points, dtype=np.float64), np.asarray(triangles, dtype=np.int64)
    points32 = np.ascontiguousarray(points, dtype=np.float32)
    triangles32 = np.ascontiguousarray(triangles, dtype=np.int32)
    # The budget is a cap: where the gentle setting stops short of it (it can, far down),
    # take the looser ones rather than hand back more triangles than were allowed.
    for aggressiveness in (AGGRESSIVENESS, 7.0, 10.0):
        out_points, out_tris = fast_simplification.simplify(
            points32, triangles32, target_count=count, agg=aggressiveness
        )
        if len(out_tris) <= count:
            break
    return np.asarray(out_points, dtype=np.float64), np.asarray(out_tris, dtype=np.int64)


def surface_points(points, triangles, count):
    """`count` area-weighted points on a triangle mesh, the same ones every call."""
    a, b, c = points[triangles[:, 0]], points[triangles[:, 1]], points[triangles[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    total = area.sum()
    if total <= 0.0:
        return points[: min(len(points), count)]
    rng = np.random.default_rng(0)
    picks = rng.choice(len(triangles), size=count, p=area / total)
    r1 = np.sqrt(rng.random(count))[:, None]
    r2 = rng.random(count)[:, None]
    return a[picks] * (1 - r1) + b[picks] * (r1 * (1 - r2)) + c[picks] * (r1 * r2)


class Reference(NamedTuple):
    """A source surface, as reductions of it are measured against it."""

    tree: object  # cKDTree over its vertices and dense surface points
    probes: np.ndarray  # a fixed subset of those


def reference(points, triangles) -> Reference:
    """The source's vertices *and* points scattered densely over it: vertices alone are a
    voxel apart on a remeshed proxy but can be a hand apart on a coarse mesh, and a measure
    can't resolve finer than the gaps in what it measures against."""
    surface = np.concatenate([points, surface_points(points, triangles, _SAMPLES)])
    rng = np.random.default_rng(0)
    probes = surface[rng.choice(len(surface), size=min(len(surface), _PROBES), replace=False)]
    return Reference(cKDTree(surface), probes)


def deviation(ref: Reference, points, triangles) -> float:
    """How far a reduction strays from its source: the 95th percentile of the distances
    both ways. Both ways, because a reduction can bulge out past its source *and* cut a
    corner off it, and each is only seen from one side."""
    sampled = surface_points(points, triangles, _SAMPLES)
    out = np.percentile(ref.tree.query(sampled[:: max(1, len(sampled) // _PROBES)])[0], 95)
    back = np.percentile(cKDTree(sampled).query(ref.probes)[0], 95)
    return float(max(out, back))


def reduce_within(points, triangles, cap, max_error):
    """The fewest triangles, at most `cap`, within `max_error` x the mesh's diagonal of it.

    Reducing the source is the slow step (a second on a 600k-triangle remesh), so it runs
    twice at most: once to the cap, and once at the count found -- which is found by
    bisecting reductions of the cap's result, a few thousand triangles and milliseconds
    each. Two reduction paths to one count don't end in quite the same place, so the final
    one is checked and stepped back up if it strays.

    Returns `(points, triangles)`.
    """
    best = simplify(points, triangles, cap)
    if max_error <= 0.0 or len(best[1]) <= MIN_TRIANGLES:
        return best
    diag = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0))) or 1.0
    tolerance = max_error * diag
    ref = reference(points, triangles)

    def within(mesh):
        return len(mesh[1]) > 0 and deviation(ref, *mesh) <= tolerance

    if not within(best):
        return best  # the cap is already as coarse as this shape can go
    low, high = float(MIN_TRIANGLES), float(len(best[1]))
    while high / low > 1.25:
        middle = int(round(np.sqrt(low * high)))
        quick = simplify(*best, middle)
        if within(quick):
            high = float(len(quick[1]))
        else:
            low = float(middle)
    count = int(high)
    while count < len(best[1]):
        final = simplify(points, triangles, count)
        if within(final):
            return final
        count = int(count * 1.25) + 1
    return best
