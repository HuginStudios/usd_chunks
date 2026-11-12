"""Scatter Points' Sampling: Tiles -- the spacing rules of Darts from one laid blue-noise tile.

Opt-in, since it is a different layout: Darts stays the default and keeps every saved scene's
layout. Tiles must still hold every instance clear of the others by both radii, fill flat
ground as fully as Darts does, honour the density, Keep Away From and the count, land on the
surface, and give the same layout every time.
"""
from __future__ import annotations

import importlib
import random

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import Scene

sp = importlib.import_module("chunks.modelling.scatter_points")
cKDTree = pytest.importorskip("scipy.spatial").cKDTree

SIDE = 120.0


def _plane(side=SIDE, tilt=0.0):
    """Two triangles making a square, rising *tilt* in z per unit of x."""
    v0 = np.array([[0, 0, 0], [side, side, side * tilt]], float)
    v1 = np.array([[side, 0, side * tilt], [0, side, 0]], float)
    v2 = np.array([[side, side, side * tilt], [0, 0, 0]], float)
    normal = np.array([-tilt, 0.0, 1.0]) / np.hypot(tilt, 1.0)
    return sp._TriangleSoup(v0, v1, v2, np.array([normal, normal]))


def _tiles(tris, count=10_000_000, mask=None, avoid=None, seed=1, vary=True):
    pos, nor, sc = sp._scatter_tiles(tris, count, random.Random(seed), 3.0, random.Random(seed + 13),
                                     0.3, 0.65, 1.0, mask=mask, avoid=avoid, vary=vary, up_idx=2)
    return np.asarray(pos, dtype=np.float64).reshape(-1, 3), np.asarray(nor).reshape(-1, 3), np.asarray(sc)


def _radii(scales, vary=True):
    return 3.0 / 0.65 * 0.5 * scales if vary else np.full(len(scales), 1.5)


def _kept_one_at_a_time(pts, rad, side, reach):
    """The fill as it was first written: every candidate in draw order, kept unless an
    earlier one it clashes with was kept. Slow, and the rule the fast fill must match."""
    pairs = cKDTree(pts, boxsize=side).query_pairs(reach, output_type="ndarray")
    kept = np.ones(len(pts), dtype=bool)
    if not len(pairs):
        return kept
    i, j = pairs.min(axis=1), pairs.max(axis=1)
    hit = sp._clashes(pts, rad, side, i, j)
    earlier: dict = {}
    for a, b in zip(i[hit].tolist(), j[hit].tolist()):
        earlier.setdefault(b, []).append(a)
    for k in sorted(earlier):
        kept[k] = not kept[earlier[k]].any()
    return kept


@pytest.mark.parametrize("lo, hi", [(1.0, 1.0), (0.8, 1.6), (0.4, 2.0)])
def test_the_batched_fill_keeps_what_one_at_a_time_did(lo, hi):
    """_darts_fill tests candidates against what was kept, in batches, instead of finding
    every clashing pair among all of them (12x on a birch forest's tile, 27x on wide
    scales). Same rule, so the same points -- across batch edges too."""
    gen = np.random.default_rng(4)
    side = 60.0
    n = 3 * sp._DARTS_BATCH + 123  # several batches, the last one partial
    pts = gen.random((n, 2)) * side
    rad = lo + (hi - lo) * gen.random(n)
    reach = 2.0 * hi * (1.0 + 1e-9)
    fast = sp._darts_fill(pts, rad, side, reach, cKDTree)
    assert np.array_equal(fast, _kept_one_at_a_time(pts, rad, side, reach))
    assert 0 < fast.sum() < n


def test_kept_in_draw_order_follows_a_chain():
    """0 clashes 1 clashes 2: 0 kept, so 1 is not, so 2 is."""
    keep = sp._kept_in_draw_order(3, np.array([0, 1]), np.array([1, 2]))
    assert keep.tolist() == [True, False, True]


def test_every_instance_keeps_both_radii_from_the_others():
    pos, _, sc = _tiles(_plane())
    r = _radii(sc)
    pairs = cKDTree(pos[:, :2]).query_pairs(2.0 * r.max(), output_type="ndarray")
    d = np.linalg.norm(pos[pairs[:, 0], :2] - pos[pairs[:, 1], :2], axis=1)
    assert not (d < (r[pairs[:, 0]] + r[pairs[:, 1]]) * (1.0 - 1e-6)).any()


def test_plain_spacing_too():
    pos, _, _ = _tiles(_plane(), vary=False)
    nearest = cKDTree(pos[:, :2]).query(pos[:, :2], k=2)[0][:, 1]
    assert nearest.min() >= 3.0 * (1.0 - 1e-6)


def test_fills_flat_ground_as_fully_as_darts():
    tris = _plane()
    darts, _, darts_sc = sp._scatter_sized(tris, 10_000_000, random.Random(1), 3.0, random.Random(14),
                                           0.3, 0.65, 1.0, vary=True)
    pos, _, sc = _tiles(tris)
    assert len(pos) == pytest.approx(len(darts), rel=0.08)
    assert sc.mean() == pytest.approx(np.mean(darts_sc), rel=0.05)


@pytest.mark.parametrize("tilt", [0.58, 1.0])
def test_slopes_fill_like_darts_and_keep_spacing_along_them(tilt):
    """Darts spaces instances along the surface, so a slope takes more per metre of ground;
    Tiles squashes its tile along the fall line to match, and holds the spacing in 3D."""
    tris = _plane(tilt=tilt)
    darts, _, _ = sp._scatter_sized(tris, 10_000_000, random.Random(1), 3.0, random.Random(14),
                                    0.3, 0.65, 1.0, vary=True)
    flat, _, _ = _tiles(_plane())
    pos, _, sc = _tiles(tris)
    assert len(pos) > 1.05 * len(flat)
    assert len(pos) >= 0.85 * len(darts)
    r = _radii(sc)
    pairs = cKDTree(pos).query_pairs(2.0 * r.max(), output_type="ndarray")
    d = np.linalg.norm(pos[pairs[:, 0]] - pos[pairs[:, 1]], axis=1)
    assert not (d < (r[pairs[:, 0]] + r[pairs[:, 1]]) * (1.0 - 1e-6)).any()


def test_same_layout_every_time():
    a, _, sa = _tiles(_plane())
    b, _, sb = _tiles(_plane())
    assert np.array_equal(a, b) and np.array_equal(sa, sb)


def test_another_seed_another_layout():
    a, _, _ = _tiles(_plane(), seed=1)
    b, _, _ = _tiles(_plane(), seed=2)
    assert len(a) != len(b) or not np.array_equal(a, b)


def test_lands_on_the_surface_with_its_normal():
    tilt = 0.5
    pos, nor, _ = _tiles(_plane(tilt=tilt))
    assert np.allclose(pos[:, 2], pos[:, 0] * tilt, atol=1e-3)
    assert np.allclose(nor, np.array([-tilt, 0.0, 1.0]) / np.hypot(tilt, 1.0), atol=1e-5)
    assert ((pos[:, :2] >= -1e-6) & (pos[:, :2] <= SIDE + 1e-6)).all()


def test_count_thins_evenly():
    full, _, _ = _tiles(_plane())
    pos, _, _ = _tiles(_plane(), count=len(full) // 4)
    assert len(pos) == len(full) // 4
    # Evenly: each half of the square keeps about half.
    left = (pos[:, 0] < SIDE / 2).mean()
    assert left == pytest.approx(0.5, abs=0.05)


def test_density_thins_by_its_weight():
    full, _, _ = _tiles(_plane())

    class Half:
        def sample_many(self, pts):
            return np.where(np.asarray(pts)[:, 0] < SIDE / 2, 1.0, 0.25)

    pos, _, _ = _tiles(_plane(), mask=Half().sample_many)
    left = (pos[:, 0] < SIDE / 2).sum()
    right = len(pos) - left
    assert left == pytest.approx((full[:, 0] < SIDE / 2).sum(), rel=0.02)
    assert right / left == pytest.approx(0.25, abs=0.04)


def test_keeps_away():
    centre = np.array([[SIDE / 2, SIDE / 2, 0.0]])
    avoid = sp._AvoidSet(centre, 10.0)
    pos, _, sc = _tiles(_plane(), avoid=avoid)
    d = np.linalg.norm(pos[:, :2] - centre[0, :2], axis=1)
    assert (d >= 10.0 + _radii(sc) - 1e-6).all()


def _scene_scatter(sampling=None, expect=None):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    mesh = UsdGeom.Mesh.Define(stage, "/ground")
    mesh.CreatePointsAttr([Gf.Vec3f(0, 0, 0), Gf.Vec3f(60, 0, 0), Gf.Vec3f(60, 60, 0), Gf.Vec3f(0, 60, 0)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    UsdGeom.Xform.Define(stage, "/protos/rock")
    UsdGeom.Cube.Define(stage, "/protos/rock/geo")
    with Scene(stage) as scene:
        extra = {"sampling": sampling} if sampling else {}
        s = scene.create.scatter_points(surface_prim="/ground", inst_path="/scatter", max_count=100_000,
                                        auto_spacing=False, spacing=2.0, size_spacing=True, seed=3,
                                        **{"prototypes.0.proto_path": "/protos/rock"}, **extra)
        scene.set_output(s)
        assert s.get_param("sampling").value == (expect or sampling)
        inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/scatter"))
        return np.asarray(inst.GetPositionsAttr().Get())


def test_a_new_scatter_samples_with_tiles_and_darts_is_a_choice():
    tiles = _scene_scatter(expect="Tiles")
    darts = _scene_scatter("Darts")
    assert len(darts) and len(tiles)
    assert len(darts) != len(tiles) or not np.array_equal(darts, tiles)
    assert ((tiles[:, :2] >= 0) & (tiles[:, :2] <= 60)).all() and np.allclose(tiles[:, 2], 0.0, atol=1e-4)


def test_a_saved_scatter_without_sampling_loads_as_darts(tmp_path):
    """Saved before Tiles existed (no sampling attribute): it keeps the layout it was made with."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    with Scene(stage) as scene:
        scene.create.scatter_points(name="Old", sampling="Darts")
        path = str(tmp_path / "old.usda")
        scene.save(path)
    layer = Sdf.Layer.FindOrOpen(path)
    assert '"Darts"' not in layer.ExportToString()  # the default, so not written
    with Scene.open(path) as scene:
        assert scene.get_chunk("Old").get_param("sampling").value == "Darts"


def test_sampling_is_saved_with_the_network(tmp_path):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    with Scene(stage) as scene:
        s = scene.create.scatter_points(sampling="Tiles")
        path = str(tmp_path / "net.usda")
        scene.save(path)
        name = s.name
    saved = Sdf.Layer.FindOrOpen(path).ExportToString()
    assert name in saved and '"Tiles"' in saved
