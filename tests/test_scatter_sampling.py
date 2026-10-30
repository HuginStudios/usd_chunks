"""Scatter Points' surface sampling gives the scatter it always did, only faster.

The draw loop was rewritten to build candidates and ask the density mask in blocks. A given
seed must still place exactly the same points, so saved scenes keep their layout: these tests
hold it to a verbatim copy of the old loop.
"""
from __future__ import annotations

import bisect
import importlib
import math
import random

import numpy as np
import pytest
from pxr import Gf

from usd_chunks import Scene, chunk

sp = importlib.import_module("chunks.modelling.scatter_points")


class _OldGrid:
    """The spacing grid as it was, tuple keys and all."""

    def __init__(self, min_dist):
        self._grid = {}
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


def _old_scatter_on_triangles(triangles, count, rng, min_dist=0, mask=None):
    """The sampler as it was before the rewrite, line for line."""
    if not len(triangles):
        return [], []
    areas = triangles.areas()
    total_area = float(areas.sum())
    if total_area < 1e-10:
        return [], []
    cdf = (np.cumsum(areas) / total_area).tolist()
    v0s, v1s, v2s, normals_all = triangles.v0, triangles.v1, triangles.v2, triangles.n
    positions, normals = [], []
    grid = _OldGrid(min_dist)
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


def _bumpy_plane(n=24, size=10.0):
    """An n x n grid of quads, split into triangles, over a wavy surface."""
    xs = np.linspace(0.0, size, n + 1)
    x, y = np.meshgrid(xs, xs, indexing="ij")
    z = 0.4 * np.sin(x) * np.cos(0.7 * y)
    points = np.stack([x, y, z], axis=-1).reshape(-1, 3)
    i = np.arange(n)[:, None] * (n + 1)
    j = np.arange(n)[None, :]
    a = (i + j).ravel()
    b, c, d = a + (n + 1), a + (n + 2), a + 1
    return sp._TriangleSoup.from_arrays(points, np.concatenate([a, a]), np.concatenate([b, c]),
                                        np.concatenate([c, d])), points


def _painted(points):
    """A density field: full on one side of the plane, fading to nothing on the other."""
    return sp._NearestWeight(points, np.clip(points[:, 0] / 6.0 - 0.3, 0.0, 1.0)).sample


@pytest.mark.parametrize(
    ("count", "spacing", "mask"),
    [
        (400, 0.2, None),
        (400, 0.0, None),
        (3000, 0.25, None),  # more than fits: runs out of attempts, over many blocks
        (400, 0.2, "painted"),  # a mask with sample_many, asked in blocks
        (400, 0.2, "plain"),  # a plain function, asked one candidate at a time
        (3000, 0.25, "painted"),
    ],
)
def test_the_same_seed_places_the_same_points(count, spacing, mask) -> None:
    triangles, points = _bumpy_plane()
    hook = {"painted": _painted(points), "plain": lambda pt, n: 0.5 + 0.5 * math.sin(pt[1]), None: None}[mask]
    old_rng, new_rng = random.Random(7), random.Random(7)

    old = _old_scatter_on_triangles(triangles, count, old_rng, spacing, mask=hook)
    new = sp._scatter_on_triangles(triangles, count, new_rng, spacing, mask=hook)

    assert len(new[0]) == len(old[0]) > 0
    assert new[0] == old[0], "positions"
    assert new[1] == old[1], "normals"
    assert new_rng.random() == old_rng.random(), "the generator ends where the old loop left it"


def test_the_spacing_grid_answers_as_before() -> None:
    gen = np.random.default_rng(3)
    old, new = _OldGrid(0.3), sp._PoissonGrid(0.3)
    for p in gen.uniform(-5.0, 5.0, (3000, 3)).astype(np.float32).tolist():
        pt = Gf.Vec3f(*p)
        assert new.too_close(pt) == old.too_close(pt)
        if not old.too_close(pt):
            old.add(pt)
            new.add(pt)


class _Proto:
    """Just enough of a prototype for the Long Axis modes."""

    def __init__(self, axis):
        self.axis = axis

    def long_axis_dir(self):
        v = [0.0, 0.0, 0.0]
        v[self.axis] = 1.0
        return Gf.Vec3f(*v)


def _loop_orientations(count, mode, normals, protos, assignment, up, axis, amount, rngs):
    """The per-instance loop's orientations, written to half precision as the instancer stores them."""
    out = []
    for i in range(count):
        rng = rngs[i] if isinstance(rngs, list) else rngs
        q = sp._compute_orientation(mode, Gf.Vec3f(*normals[i]), rng, protos[assignment[i]], up)
        if mode == "Align to Normal":
            # Spun about the instance's own axes: the spin first, then the turn onto the surface.
            q = sp._quat_mul(q, sp._apply_random_rotation(Gf.Quatf(1, 0, 0, 0), axis, amount, rng))
        else:
            q = sp._apply_random_rotation(q, axis, amount, rng)
        h = sp._to_quath(q)
        out.append((h.GetReal(), *h.GetImaginary()))
    return np.asarray(out, dtype=np.float64)


@pytest.mark.parametrize("up", [Gf.Vec3f(0, 0, 1), Gf.Vec3f(0, 1, 0)], ids=["z-up", "y-up"])
@pytest.mark.parametrize("mode", ["Align to Normal", "Random", "Long Axis Up", "Long Axis to Normal", "Up"])
@pytest.mark.parametrize("axis", ["Off", "Z", "All"])
@pytest.mark.parametrize("per_point", [False, True], ids=["one-rng", "painted"])
def test_array_orientations_match_the_loop(up, mode, axis, per_point) -> None:
    """Each instance gets the turn the loop gave it, to within half-precision rounding, and the
    generator is left where the loop left it."""
    count = 600
    gen = np.random.default_rng(2)
    normals = gen.standard_normal((count, 3))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    normals[:4] = [[0, 0, 1], [0, 0, -1], [0, 1, 0], [0, -1, 0]]  # along and against either up
    normals = normals.astype(np.float32).astype(np.float64)
    protos = [_Proto(0), _Proto(1), _Proto(2)]
    assignment = gen.integers(0, 3, count)

    def rngs():
        return [random.Random(100 + i) for i in range(count)] if per_point else random.Random(5)

    old_rngs, new_rngs = rngs(), rngs()
    old = _loop_orientations(count, mode, normals, protos, assignment, up, axis, 40.0, old_rngs)
    new = sp._orientations(count, mode, normals, protos, assignment, up, axis, 40.0, new_rngs)
    new = new.astype(np.float32).astype(np.float16).astype(np.float64)

    # One step of a half near 1 is about 0.0005.
    assert np.abs(new - old).max() <= 0.001
    assert np.mean(np.all(new == old, axis=1)) > 0.99
    last = (lambda r: r[-1]) if per_point else (lambda r: r)
    assert last(new_rngs).random() == last(old_rngs).random()


def _turn(quat, vector):
    """vector turned by a (real, i, j, k) quaternion row."""
    return sp._rotate_arrays(np.asarray(quat, dtype=np.float64)[None, :], np.asarray([vector], dtype=np.float64))[0]


@pytest.mark.parametrize("normal", [(0, 1, 0), (1, 0, 0), (-0.6, -0.8, 0), (0, 0.8, 0.6)])
def test_up_along_surface_points_the_axis_up_any_wall(normal) -> None:
    """Live-reported: hanging ivy strands, modelled along -X, stuck out sideways or even up
    from the side walls. With +X up the surface they hang down every wall."""
    up = Gf.Vec3f(0, 0, 1)
    n = np.asarray([normal], dtype=np.float64)
    n /= np.linalg.norm(n)
    q = sp._orientations(1, "Align to Normal", n, [_Proto(0)], np.array([0]), up, "Off", 0.0, random.Random(1), "+X")[0]
    assert np.allclose(_turn(q, (0, 0, 1)), n[0], atol=1e-6), "still facing out of the surface"
    up_on_surface = np.array([0.0, 0.0, 1.0]) - n[0][2] * n[0]
    up_on_surface /= np.linalg.norm(up_on_surface)
    assert np.allclose(_turn(q, (1, 0, 0)), up_on_surface, atol=1e-6), "+X points up the surface"


def test_a_spun_instance_stays_on_the_wall() -> None:
    """Spinning a wall leaf about Z turned it about the world's Z, which stood it out of the wall."""
    up = Gf.Vec3f(0, 0, 1)
    normals = np.tile([[0.0, 1.0, 0.0]], (200, 1))
    quats = sp._orientations(200, "Align to Normal", normals, [_Proto(0)], np.zeros(200, int), up, "Z", 180.0,
                             random.Random(3))
    faces = sp._rotate_arrays(quats, np.tile([[0.0, 0.0, 1.0]], (200, 1)))
    assert np.allclose(faces, normals, atol=1e-6)


def test_up_along_surface_leaves_a_floor_alone() -> None:
    up = Gf.Vec3f(0, 0, 1)
    plain = sp._orientations(1, "Align to Normal", np.array([[0.0, 0.0, 1.0]]), [_Proto(0)], np.array([0]), up, "Off",
                             0.0, random.Random(1))
    turned = sp._orientations(1, "Align to Normal", np.array([[0.0, 0.0, 1.0]]), [_Proto(0)], np.array([0]), up,
                              "Off", 0.0, random.Random(1), "+X")
    assert np.all(np.isfinite(turned)) and np.allclose(plain, turned)



def test_up_along_surface_on_a_y_up_stage() -> None:
    """On a Y-up stage the prototype stands along Y, so +X is turned up a wall toward +Y."""
    up = Gf.Vec3f(0, 1, 0)
    n = np.array([[0.0, 0.0, 1.0]])  # a wall facing +Z
    q = sp._orientations(1, "Align to Normal", n, [_Proto(0)], np.array([0]), up, "Off", 0.0, random.Random(1), "+X")[0]
    assert np.allclose(_turn(q, (0, 1, 0)), n[0], atol=1e-6)
    assert np.allclose(_turn(q, (1, 0, 0)), (0, 1, 0), atol=1e-6)


def test_up_along_surface_takes_unnormalised_normals() -> None:
    """Painted points bring their normals from the file, not always of length 1."""
    up = Gf.Vec3f(0, 0, 1)
    unit = sp._orientations(1, "Align to Normal", np.array([[0.0, 1.0, 0.0]]), [_Proto(0)], np.array([0]), up,
                            "Off", 0.0, random.Random(1), "+X")
    long = sp._orientations(1, "Align to Normal", np.array([[0.0, 3.0, 0.0]]), [_Proto(0)], np.array([0]), up,
                            "Off", 0.0, random.Random(1), "+X")
    assert np.allclose(unit, long, atol=1e-9)


def test_up_along_surface_shows_only_with_align_to_normal() -> None:
    hidden = chunk.ChunkFlags.State.HIDDEN
    with Scene() as scene:
        node = scene.create.scatter_points(name="s", inst_path="/World/s")
        align_up = node.get_param("align_up")
        assert not align_up.has_state(hidden)
        node.get_param("orient_mode").value = "Up"
        assert align_up.has_state(hidden)
        node.get_param("orient_mode").value = "Align to Normal"
        assert not align_up.has_state(hidden)
