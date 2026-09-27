"""Building a collider must stay cheap enough that a high-poly one is usable.

Two costs used to sit between an artist and a real production collider, both of them work
the solver did not need to do:

* the heightfield's triangle rasteriser ran for essentially every triangle in the mesh,
  including the millions that are smaller than one grid cell and whose corners had already
  supplied that cell's height;
* the collider's meshes were walked twice per run -- once to count triangles against the
  budget, then again to build the geoms.

Neither is about the simulation being slow to *step*; both are setup, paid on every single
Run Simulation. The assertions below are written against a cheap in-test baseline rather
than an absolute wall-clock bound, so they mean the same thing on a fast machine and a
slow one.
"""

from __future__ import annotations

import time

import pytest
from pxr import Usd, UsdGeom, Vt

import chunks.dynamics.rbd_solver as rbd_mod

if not rbd_mod._HAS_MUJOCO:
    pytest.skip("mujoco not installed", allow_module_level=True)

import numpy as np  # noqa: E402


def _dense_terrain(stage, path="/terrain", n=320):
    """A quad grid with real relief: ~200k triangles, far finer than the 96-cell grid it
    gets sampled into -- a production terrain or a scanned asset, in other words."""
    xs = np.linspace(-100.0, 100.0, n)
    X, Y = np.meshgrid(xs, xs)
    Z = 5.0 * np.sin(X * 0.05) * np.cos(Y * 0.05) + 2.0 * np.sin(X * 0.31)
    pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], axis=1).astype(np.float32)
    idx = np.arange(n * n).reshape(n, n)
    quads = np.stack([idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]], -1).reshape(-1, 4)

    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts))
    mesh.GetFaceVertexCountsAttr().Set([4] * len(quads))
    mesh.GetFaceVertexIndicesAttr().Set(quads.ravel().tolist())
    return stage.GetPrimAtPath(path)


@pytest.fixture
def terrain():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    prim = _dense_terrain(stage)
    yield prim  # yield, not return: the prim handle expires the moment the stage does
    del stage


def test_a_dense_collider_heightfields_in_the_time_it_takes_to_read_it(terrain):
    """Reading every point and face of the mesh is the irreducible cost of building any
    collider from it. Sampling those into a grid is a handful of vectorised passes, so it
    belongs in the same order of magnitude -- it is not entitled to a per-triangle Python
    loop on top. It used to take ~17x the read; the ceiling here is well clear of the ~1.4x
    it costs now and well below that."""
    t0 = time.perf_counter()
    _, tris = rbd_mod._collider_triangles(terrain)
    read = max(time.perf_counter() - t0, 1e-3)
    assert len(tris) > 150_000, "the point of this test is a mesh far denser than the grid"

    t0 = time.perf_counter()
    hf = rbd_mod._build_heightfield(terrain, 2, 96, min_cell=0.75)
    build = time.perf_counter() - t0

    assert hf is not None, "a terrain with relief must be heightfieldable"
    assert build < read * 8.0, (
        f"heightfield build took {build:.2f}s against a {read:.2f}s read of the same mesh "
        f"({build / read:.1f}x) -- the per-triangle rasteriser is back"
    )


def test_the_grid_still_describes_the_surface(terrain):
    """The speedup must not have cost accuracy: every sampled cell has to land on the
    analytic surface the mesh was built from, not somewhere under it."""
    hf = rbd_mod._build_heightfield(terrain, 2, 96, min_cell=0.75)
    H = np.asarray(hf["heights"]).reshape(hf["nrow"], hf["ncol"]) * hf["size"][2] + hf["pos"][2]

    g = np.linspace(-100.0, 100.0, hf["ncol"])
    X, Y = np.meshgrid(g, g)
    expect = 5.0 * np.sin(X * 0.05) * np.cos(Y * 0.05) + 2.0 * np.sin(X * 0.31)

    # Interior only: the border cells average in the grid edge, and the smoothing pass is
    # clamped there. What is left is grid resolution and that smoothing -- both deliberate,
    # both bounded; a cell filled from the wrong triangle is not, and lands far outside this.
    err = np.abs(H - expect)[2:-2, 2:-2]
    relief = float(expect.max() - expect.min())
    assert err.max() < 0.12 * relief, f"grid deviates by {err.max():.2f} of {relief:.2f} relief"


def test_the_collider_is_read_once_per_run(monkeypatch):
    """The triangle count and the geoms come from the same walk of the mesh. Doing it twice
    is free to write and invisible in a unit test with a cube in it, and doubles the setup
    cost of every real collider.

    Deliberately a SMALL mesh: this is the triangle-soup path, which costs one MuJoCo geom
    per triangle, so a collider dense enough to make the duplicate read hurt is also one
    far too expensive to build inside a test.
    """
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    prim = _dense_terrain(stage, n=16)

    calls = []
    real = rbd_mod._collider_triangles
    monkeypatch.setattr(
        rbd_mod, "_collider_triangles",
        lambda p, world=True: (calls.append(p.GetPath()), real(p, world))[1],
    )

    spec = rbd_mod._mj.MjSpec()
    rbd_mod._add_colliders(
        spec, [prim], up_idx=2, friction=0.5, contact_tc=0.02, dyn_radius=5.0,
        hfield_res=96, collider_mode="Mesh", tri_budget=200_000,
    )

    assert spec.compile().ngeom > 100, "this must have gone down the triangle-soup path"
    assert calls.count(prim.GetPath()) == 1, (
        f"the collider's meshes were walked {calls.count(prim.GetPath())} times for one run"
    )
