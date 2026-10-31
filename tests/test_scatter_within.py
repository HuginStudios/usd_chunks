"""Scatter Points finds a batch's clashes with what is already placed by a k-nearest query.

sparse_distance_matrix walked a tree of the batch against each placed tree in one thread, and
was 40 of BushesValley's 49 s on the woodland vista. _within asks each candidate for its nearest
k within reach, on every core, widening k only where all k were found. It must find exactly the
pairs sparse_distance_matrix found -- a pair missed is an overlap let through, and the layout
would change.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import numpy as np
import pytest

cKDTree = pytest.importorskip("scipy.spatial").cKDTree


def _scatter_module():
    path = Path(__file__).resolve().parent.parent / "chunks" / "modelling" / "scatter_points.py"
    spec = importlib.util.spec_from_file_location("_scatter_points_within_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module


def _pairs_from_sparse(placed, pts, reach):
    near = cKDTree(pts).sparse_distance_matrix(cKDTree(placed), reach, output_type="ndarray")
    return {(int(i), int(j)) for i, j in zip(near["i"], near["j"])}


@pytest.mark.parametrize("case", ["spread", "clustered", "one_placed", "everything_in_reach"])
def test_within_finds_the_pairs_sparse_distance_matrix_finds(case) -> None:
    within = _scatter_module()._within
    rng = np.random.default_rng(7)
    if case == "spread":
        placed, pts, reach = rng.random((5000, 3)) * 100, rng.random((3000, 3)) * 100, 2.5
    elif case == "clustered":
        # Hundreds of placed points around each candidate: k has to grow several times.
        placed = rng.normal(0, 1, (20000, 3))
        pts, reach = rng.normal(0, 1, (500, 3)), 1.5
    elif case == "one_placed":
        placed, pts, reach = np.zeros((1, 3)), rng.random((100, 3)), 0.8
    else:
        placed, pts, reach = rng.random((40, 3)), rng.random((60, 3)), 10.0
    i, j = within(cKDTree(placed), pts, reach)
    found = {(int(a), int(b)) for a, b in zip(i, j)}
    assert len(found) == len(i), "a pair was listed twice"
    assert found == _pairs_from_sparse(placed, pts, reach)


def _ground(module, n=40):
    # An n x n grid of quads, two triangles each, 100 units across.
    xs = np.linspace(0, 100, n + 1)
    gx, gy = np.meshgrid(xs, xs, indexing="ij")
    points = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1)
    a = (np.arange(n)[:, None] * (n + 1) + np.arange(n)[None, :]).ravel()
    b, c, d = a + 1, a + n + 1, a + n + 2
    return module._TriangleSoup.from_arrays(
        points, np.concatenate([a, b]), np.concatenate([b, d]), np.concatenate([c, c]))


def test_spacing_free_streams_lay_out_the_same_on_any_number_of_cores(monkeypatch) -> None:
    # Spacing-free scatters run their streams on threads, as many as there are cores: the
    # streams, not the threads, decide the layout, so one core and many must agree.
    module = _scatter_module()
    ground = _ground(module)
    avoid = module._AvoidSet(np.array([[50.0, 50.0, 0.0]]), 10.0)

    def scatter():
        return module._scatter_sized(
            ground, 50_000, random.Random(3), 0.0, random.Random(16), 0.5, 2.0, 1.0,
            avoid=avoid, vary=True)

    many = scatter()
    monkeypatch.setattr(module.os, "cpu_count", lambda: 1)
    one = scatter()
    pos = np.asarray(many[0])
    assert len(pos) == 50_000
    assert np.array_equal(pos, np.asarray(one[0]))
    assert np.array_equal(np.asarray(many[1]), np.asarray(one[1]))
    assert many[2] == one[2]
    assert (np.linalg.norm(pos - [50.0, 50.0, 0.0], axis=1) >= 10.0).all()


def test_spacing_free_covers_the_whole_mesh_evenly() -> None:
    # Spacing-free candidates come out grouped by triangle, and a round draws more than the
    # count needs. Trimming the surplus off the end would leave the last triangles bare: every
    # tenth of the ground must hold its tenth of the points.
    module = _scatter_module()
    pos = np.asarray(module._scatter_sized(
        _ground(module), 50_000, random.Random(5), 0.0, random.Random(18), 1.0, 1.0, 1.0,
        vary=True)[0])
    assert len(pos) == 50_000
    tenths = np.histogram(pos[:, 0], bins=10, range=(0, 100))[0] / len(pos)
    assert np.allclose(tenths, 0.1, atol=0.01), tenths


def test_keep_away_many_agrees_with_one_point_at_a_time() -> None:
    # near_many now finds its pairs through _within at the widest reach and holds each point to
    # its own: it must flag exactly the points near() flags, one by one.
    module = _scatter_module()
    rng = np.random.default_rng(11)
    away = rng.random((3000, 3)) * 50
    avoid = module._AvoidSet(away, 0.6, radii=rng.random(3000) * 0.4, extra_max=1.5)
    pts = rng.random((4000, 3)) * 50
    extra = rng.random(4000) * 1.5
    many = avoid.near_many(pts, extra, cKDTree)
    one = np.array([avoid.near(p, e) for p, e in zip(pts, extra)])
    assert many.any() and not many.all()
    assert (many == one).all()
