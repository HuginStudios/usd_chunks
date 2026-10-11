"""mesh_reduce: a triangle budget is a cap, and the shape decides how far under it to go."""

from __future__ import annotations

import numpy as np
import pytest

from usd_chunks import mesh_reduce

pytestmark = pytest.mark.skipif(not mesh_reduce.HAS_DEPS, reason="needs fast-simplification and scipy")


def _box(n=40, size=(2.0, 1.0, 0.5)):
    """A closed, finely tessellated box -- flat faces, as a remeshed cabinet has."""
    faces = []
    points = []
    for axis in range(3):
        for sign in (-1.0, 1.0):
            u, v = [a for a in range(3) if a != axis]
            g = np.linspace(-0.5, 0.5, n)
            gu, gv = np.meshgrid(g, g, indexing="ij")
            p = np.zeros((n * n, 3))
            p[:, axis] = sign * 0.5
            p[:, u], p[:, v] = gu.ravel(), gv.ravel()
            base = len(points) and sum(len(x) for x in points)
            i = np.arange(n - 1)[:, None] * n + np.arange(n - 1)[None, :]
            quads = np.stack([i, i + n, i + n + 1, i + 1], axis=-1).reshape(-1, 4) + base
            tris = np.concatenate([quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]])
            if (sign > 0) == (axis == 1):
                tris = tris[:, ::-1]
            points.append(p)
            faces.append(tris)
    points = np.concatenate(points) * np.asarray(size)
    # weld the shared edges so it is one closed surface
    unique, inverse = np.unique(np.round(points, 9), axis=0, return_inverse=True)
    return unique, inverse.reshape(-1)[np.concatenate(faces)]


def _sphere(n=120):
    lat = np.linspace(0, np.pi, n + 1)[1:-1]
    lon = np.linspace(0, 2 * np.pi, 2 * n, endpoint=False)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    pts = np.vstack([[0, 0, 1], np.stack([np.sin(la) * np.cos(lo), np.sin(la) * np.sin(lo), np.cos(la)], -1).reshape(-1, 3), [0, 0, -1]])
    seg = 2 * n
    ring = lambda r, s: 1 + r * seg + s % seg  # noqa: E731
    tris = [[0, ring(0, s), ring(0, s + 1)] for s in range(seg)]
    for r in range(n - 2):
        for s in range(seg):
            tris += [[ring(r, s), ring(r + 1, s), ring(r + 1, s + 1)], [ring(r, s), ring(r + 1, s + 1), ring(r, s + 1)]]
    tris += [[ring(n - 2, s), len(pts) - 1, ring(n - 2, s + 1)] for s in range(seg)]
    return pts, np.asarray(tris)


def test_a_flat_box_stops_far_under_the_cap() -> None:
    """A plane is two triangles, a box twelve: not the budget."""
    points, tris = _box()
    _, reduced = mesh_reduce.reduce_within(points, tris, 3000, 0.005)
    assert len(reduced) < 100


def test_a_curved_surface_uses_more_and_less_with_a_looser_error() -> None:
    points, tris = _sphere()
    _, tight = mesh_reduce.reduce_within(points, tris, 3000, 0.002)
    _, loose = mesh_reduce.reduce_within(points, tris, 3000, 0.02)
    assert len(loose) < len(tight) <= 3000
    assert len(tight) > 200


def test_the_cap_is_never_exceeded_and_zero_error_means_exactly_the_cap() -> None:
    points, tris = _sphere()
    _, exact = mesh_reduce.reduce_within(points, tris, 1000, 0.0)
    assert len(exact) == pytest.approx(1000, rel=0.02)
    _, capped = mesh_reduce.reduce_within(points, tris, 300, 0.0001)
    assert len(capped) <= 300
