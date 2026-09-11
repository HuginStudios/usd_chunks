"""A built collider is reused across runs, and is never reused when it shouldn't be.

Building a Mesh collider is one MuJoCo geom per triangle and MjSpec construction is
quadratic in element count -- 10k triangles is ~14s, 20k is ~61s -- so the same collider
was rebuilt from nothing on every Run Simulation. Copying a built spec is ~0.02s at any
size, so it is built once and copied thereafter.

Every test here except the first is about the risk that buys: a cache that hands back a
stale collider is far worse than a slow one. Friction and the contact timeconstant are
deliberately outside the key -- they are what an artist re-runs to tune, and they change
no geometry -- so they are patched onto the copy instead, which the tests below pin down.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom

import scripts.rbd_solver as rbd_mod

if not rbd_mod._HAS_MUJOCO:
    pytest.skip("mujoco not installed", allow_module_level=True)

import numpy as np  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_cache():
    """The cache is module state and outlives a test; every test here starts cold."""
    rbd_mod._COLLIDER_SPEC_CACHE.clear()
    yield
    rbd_mod._COLLIDER_SPEC_CACHE.clear()


class _Scene:
    """A dish collider plus the handles a test needs. Held as an object because a
    Usd.Prim handle expires the moment its stage is collected."""

    def __init__(self, z=0.0, n=14):
        self.stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        g = np.linspace(-100.0, 100.0, n)
        X, Y = np.meshgrid(g, g)
        Z = z + 0.004 * (X ** 2 + Y ** 2)
        pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1)
        idx = np.arange(n * n).reshape(n, n)
        quads = np.stack([idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]], -1).reshape(-1, 4)
        m = UsdGeom.Mesh.Define(self.stage, "/dish")
        m.GetPointsAttr().Set([Gf.Vec3f(*p) for p in pts])
        m.GetFaceVertexCountsAttr().Set([4] * len(quads))
        m.GetFaceVertexIndicesAttr().Set(quads.ravel().tolist())
        self.mesh = m
        self.prim = self.stage.GetPrimAtPath("/dish")


def _spec(scene, friction=0.5, contact_tc=0.02):
    return rbd_mod._collider_spec(
        [scene.prim], up_idx=2, friction=friction, contact_tc=contact_tc,
        dyn_radius=5.0, hfield_res=96, collider_mode="Mesh", tri_budget=200_000,
    )


def _geom_count(spec):
    return spec.compile().ngeom


def _lowest_geom(spec) -> float:
    """World height of the lowest collider geom in a compiled spec."""
    model = spec.compile()
    data = rbd_mod._mj.MjData(model)
    rbd_mod._mj.mj_forward(model, data)
    return float(data.geom_xpos[:, 2].min())


def test_the_same_collider_is_built_once_and_copied_after(monkeypatch):
    scene = _Scene()
    builds = []
    real = rbd_mod._add_colliders
    monkeypatch.setattr(
        rbd_mod, "_add_colliders",
        lambda *a, **k: (builds.append(1), real(*a, **k))[1],
    )

    first, _ = _spec(scene)
    second, _ = _spec(scene)

    assert len(builds) == 1, f"the collider was built {len(builds)} times for two runs"
    assert _geom_count(first) == _geom_count(second) > 100, "both runs need the same colliders"


def test_the_copy_is_not_the_cached_spec():
    """The caller adds its dynamic bodies to what it gets back. If that were the cached
    spec itself, run two would inherit run one's bodies -- and run three, two runs' worth."""
    scene = _Scene()
    first, _ = _spec(scene)
    n_before = _geom_count(first)

    body = first.worldbody.add_body()
    body.name = "dynamic"
    body.add_joint().type = rbd_mod._mj.mjtJoint.mjJNT_FREE
    g = body.add_geom()
    g.type = rbd_mod._mj.mjtGeom.mjGEOM_SPHERE
    g.size = [1.0, 0, 0]

    second, _ = _spec(scene)
    assert _geom_count(second) == n_before, "the cached collider picked up the caller's body"
    assert rbd_mod._mj.mj_name2id(
        second.compile(), rbd_mod._mj.mjtObj.mjOBJ_BODY, "dynamic"
    ) == -1


def test_friction_reaches_a_cached_collider(monkeypatch):
    """Friction is out of the cache key on purpose -- it changes no geometry, and it is the
    knob most likely to be re-run. It still has to actually land on the geoms."""
    scene = _Scene()
    builds = []
    real = rbd_mod._add_colliders
    monkeypatch.setattr(
        rbd_mod, "_add_colliders",
        lambda *a, **k: (builds.append(1), real(*a, **k))[1],
    )

    _spec(scene, friction=0.5)
    warm, _ = _spec(scene, friction=0.05, contact_tc=0.04)

    assert len(builds) == 1, "changing friction must not rebuild the collider"
    model = warm.compile()
    assert np.allclose(model.geom_friction[:, 0], 0.05), (
        f"cached collider kept its old friction: {model.geom_friction[:, 0][:3]}"
    )
    assert np.allclose(model.geom_solref[:, 0], 0.04), "contact timeconst did not reach the copy"


def test_moving_the_collider_rebuilds_it():
    scene_low = _Scene(z=0.0)
    scene_high = _Scene(z=40.0)

    low, _ = _spec(scene_low)
    high, _ = _spec(scene_high)

    assert len(rbd_mod._COLLIDER_SPEC_CACHE) == 2, "a moved collider must be a different entry"
    # World positions after a forward pass: a mesh geom's own frame sits at the origin, so
    # the height has to be read off the composed model rather than off geom_pos.
    lo, hi = (_lowest_geom(spec) for spec in (low, high))
    assert hi > lo + 30.0, f"the moved collider is still at the old height ({lo:.1f} vs {hi:.1f})"


def test_editing_the_mesh_rebuilds_it():
    """Not just the transform: the points themselves are hashed, so a deformed or
    re-referenced collider misses too."""
    scene = _Scene()
    before, _ = _spec(scene)

    pts = list(scene.mesh.GetPointsAttr().Get())
    pts[len(pts) // 2] = Gf.Vec3f(pts[len(pts) // 2][0], pts[len(pts) // 2][1], 55.0)
    scene.mesh.GetPointsAttr().Set(pts)

    _spec(scene)
    assert len(rbd_mod._COLLIDER_SPEC_CACHE) == 2, "an edited mesh reused its old collider"


def test_animating_the_collider_rebuilds_it():
    """The triangles are read at Default(), so keying a collider that stays put at frame 0
    changes nothing the digest would see -- but it does change the collider from a static
    body into a mocap one, which is the whole reason it stays keyed."""
    scene = _Scene()
    _spec(scene)

    op = UsdGeom.Xformable(scene.prim).AddTranslateOp()
    op.Set(Gf.Vec3d(0, 0, 0), Usd.TimeCode(1))
    op.Set(Gf.Vec3d(0, 0, 30), Usd.TimeCode(24))

    animated, mocap = _spec(scene)
    assert len(rbd_mod._COLLIDER_SPEC_CACHE) == 2, "an animated collider reused the static one"
    assert mocap, "an animated collider has to come back as a mocap body to be driven"


def test_a_collider_with_no_mesh_under_it_still_rebuilds_when_resized():
    """A UsdGeom.Cube contributes no triangles at all, so the digest of its geometry is the
    digest of nothing. Its bounds are what separate one from another."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    cube = UsdGeom.Cube.Define(stage, "/box")
    cube.GetSizeAttr().Set(20.0)
    prim = stage.GetPrimAtPath("/box")

    def build():
        return rbd_mod._collider_spec(
            [prim], up_idx=2, friction=0.5, contact_tc=0.02, dyn_radius=5.0,
            hfield_res=96, collider_mode="Mesh", tri_budget=200_000,
        )[0]

    small = build().compile().geom_size[-1].copy()
    cube.GetSizeAttr().Set(200.0)
    big = build().compile().geom_size[-1].copy()

    assert big.max() > small.max() * 5, (
        f"resized cube kept its old collider ({small.max():.1f} -> {big.max():.1f})"
    )


def test_the_cache_does_not_grow_without_bound():
    scenes = [_Scene(z=float(i)) for i in range(rbd_mod._COLLIDER_SPEC_CACHE_MAX + 3)]
    for scene in scenes:
        _spec(scene)
    assert len(rbd_mod._COLLIDER_SPEC_CACHE) == rbd_mod._COLLIDER_SPEC_CACHE_MAX
