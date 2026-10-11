"""Proximity Field: a 0..1 primvar from how near a mesh's vertices are to other points.

* the field is Inside at a source, Outside past Radius, and smooth between;
* Clear Radius empties the trunk, Scale With Source grows a big instance's reach;
* Along Ground ignores height, so a point overhead still counts;
* the chunk writes the primvar from a PointInstancer's instances, multiplied into a base one,
  and a Scatter Points reading it as its Density Field lands near the sources.
"""

from __future__ import annotations

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk_registry
from usd_chunks import proximity as prox
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init



def _line(n=101, length=10.0):
    x = np.linspace(0.0, length, n)
    return np.stack([x, np.zeros(n), np.zeros(n)], axis=1)


def test_inside_near_outside_far_smooth_between():
    p = _line()
    v = prox.field(p, [(0, 0, 0)], radius=4.0, softness=0.5, inside=1.0, outside=0.2)
    assert v[0] == pytest.approx(1.0)
    assert v[np.searchsorted(p[:, 0], 1.9)] == pytest.approx(1.0)   # inside R x (1 - softness)
    assert (v[p[:, 0] >= 4.0] == pytest.approx(0.2))
    assert (np.diff(v) <= 1e-12).all()                              # monotone fade
    # Inverted: shade pushes away.
    w = prox.field(p, [(0, 0, 0)], radius=4.0, inside=0.1, outside=1.0)
    assert w[0] == pytest.approx(0.1) and w[-1] == pytest.approx(1.0)


def test_clear_radius_and_scale_with_source():
    p = _line()
    v = prox.field(p, [(0, 0, 0)], radius=4.0, clear=1.0, inside=1.0, outside=0.0)
    assert (v[p[:, 0] < 0.75] == 0).all()
    assert v[np.searchsorted(p[:, 0], 1.5)] == pytest.approx(1.0)
    big = prox.field(p, [(0, 0, 0)], [2.0], radius=4.0, softness=0.0)
    assert big[np.searchsorted(p[:, 0], 7.0)] == pytest.approx(1.0)
    assert big[np.searchsorted(p[:, 0], 8.5)] == pytest.approx(0.0)


def test_along_ground_ignores_height_and_sources_dont_add():
    p = _line()
    high = [(0, 0, 30.0)]
    assert prox.field(p, high, radius=4.0)[0] == 0.0
    assert prox.field(p, high, radius=4.0, up=(0, 0, 1))[0] == pytest.approx(1.0)
    two = prox.field(p, [(0, 0, 0), (0.5, 0, 0)], radius=4.0, inside=0.6)
    assert two.max() == pytest.approx(0.6)


def test_empty_sources_is_outside_everywhere():
    assert (prox.field(_line(), np.zeros((0, 3)), radius=3.0, outside=0.3) == 0.3).all()


# ---------------------------------------------------------------------------
# The chunk in a scene
# ---------------------------------------------------------------------------


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    s = _Scene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)

    def mk(key):
        mod = reg.get_chunk_module(key)
        made = mod.create() if mod.loaded else mod.load()
        for c in made:
            s.add_chunk(c)
        return made[0]

    return qapp, stage, mk


def _ground(stage, size=20.0, n=81):
    """A Z-up grid, n x n vertices over size x size, centred."""
    xs = np.linspace(-size / 2, size / 2, n)
    gx, gy = np.meshgrid(xs, xs, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), np.zeros(n * n)], axis=1)
    idx = []
    for i in range(n - 1):
        for j in range(n - 1):
            a = i * n + j
            idx += [a, a + n, a + n + 1, a + 1]
    mesh = UsdGeom.Mesh.Define(stage, "/Ground")
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts.astype(np.float32)))
    mesh.CreateFaceVertexCountsAttr([4] * ((n - 1) ** 2))
    mesh.CreateFaceVertexIndicesAttr(idx)
    return mesh, pts


def test_chunk_paints_from_instances_and_scatter_nests(scene):
    qapp, stage, mk = scene
    mesh, pts = _ground(stage)
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "noise", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex).Set(Vt.FloatArray([0.5] * len(pts)))
    trees = UsdGeom.PointInstancer.Define(stage, "/Trees")
    trees.GetPositionsAttr().Set([Gf.Vec3f(-5, 0, 0), Gf.Vec3f(5, 0, 0)])
    trees.GetScalesAttr().Set([Gf.Vec3f(1, 1, 1), Gf.Vec3f(2, 2, 2)])

    f = mk("proximity_field")
    f.get_param("prim_path").value = "/Ground"
    f.get_param("source_prims").value = "/Trees"
    f.get_param("primvar").value = "under_trees"
    f.get_param("base_primvar").value = "noise"
    f.get_param("radius").value = 2.0
    f.get_param("softness").value = 0.0
    f.execute()
    qapp.processEvents()
    v = np.asarray(UsdGeom.PrimvarsAPI(mesh).GetPrimvar("under_trees").Get())
    assert len(v) == len(pts)
    d_small = np.hypot(pts[:, 0] + 5, pts[:, 1])
    d_big = np.hypot(pts[:, 0] - 5, pts[:, 1])
    assert (v[d_small < 1.9] == pytest.approx(0.5))        # inside, times the noise
    assert (v[(d_small > 2.1) & (d_big > 4.1)] == 0).all()  # outside
    assert (v[d_big < 3.9] == pytest.approx(0.5))          # scale 2: twice the reach
    assert "2 source points" in f.get_param("readout").value

    UsdGeom.Sphere.Define(stage, "/Plant").GetRadiusAttr().Set(0.1)
    s = mk("scatter_points")
    s.get_param("surface_prim").value = "/Ground"
    s.get_param("auto_spacing").value = False
    s.get_param("spacing").value = 0.2
    s.get_param("max_count").value = 150
    s.get_param("inst_path").value = "/Under"
    s.get_param("density_primvar").value = "under_trees"
    s.execute()
    qapp.processEvents()
    p = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/Under")).GetPositionsAttr().Get())
    assert len(p) > 50
    near = np.minimum(np.hypot(p[:, 0] + 5, p[:, 1]) - 2.0, np.hypot(p[:, 0] - 5, p[:, 1]) - 4.0)
    assert (near < 0.4).all()  # nearest-vertex lookup: within a grid cell of the rings


def test_scatter_grows_near_the_previous_layer(scene):
    """Grow Near reads the earlier instancer's points directly: exact distances, so a
    single-quad ground is enough, and Clear Radius keeps the trunk itself bare."""
    qapp, stage, mk = scene
    ground = UsdGeom.Mesh.Define(stage, "/Ground")
    ground.CreatePointsAttr([(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)])
    ground.CreateFaceVertexCountsAttr([4])
    ground.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    trees = UsdGeom.PointInstancer.Define(stage, "/Trees")
    trees.GetPositionsAttr().Set([Gf.Vec3f(-5, 0, 0), Gf.Vec3f(5, 0, 0)])
    trees.GetScalesAttr().Set([Gf.Vec3f(1, 1, 1), Gf.Vec3f(2, 2, 2)])
    UsdGeom.Sphere.Define(stage, "/Plant").GetRadiusAttr().Set(0.1)

    s = mk("scatter_points")
    for k, v in dict(surface_prim="/Ground", auto_spacing=False, spacing=0.2, max_count=200,
                     inst_path="/Under", near_prims="/Trees", near_radius=2.0, near_clear=0.5,
                     near_softness=0.0, near_outside=0.0).items():
        s.get_param(k).value = v
    s.execute()
    qapp.processEvents()
    p = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/Under")).GetPositionsAttr().Get())
    assert len(p) > 100
    d_small, d_big = np.hypot(p[:, 0] + 5, p[:, 1]), np.hypot(p[:, 0] - 5, p[:, 1])
    assert (np.minimum(d_small - 2.0, d_big - 4.0) <= 1e-4).all()       # within reach (scale 2: twice)
    assert ((d_small > 0.5 * 0.75) & (d_big > 1.0 * 0.75)).all()       # clear of the trunks
    assert (d_big < 4.0).sum() > (d_small < 2.0).sum()                  # the big tree holds more


def test_within_reach_pads_by_the_points_own_size() -> None:
    w = prox.Proximity([(0, 0, 0)], [2.0], radius=3.0, up=(0, 0, 1))      # reach 6
    pts = [(5.0, 0, 0), (7.0, 0, 0), (7.0, 0, 50.0), (9.0, 0, 0)]
    assert w.within_reach(pts).tolist() == [True, False, False, False]
    assert w.within_reach(pts, [0.0, 1.5, 1.5, 1.5]).tolist() == [True, True, True, False]


def test_grow_near_with_nothing_outside_samples_only_within_reach(scene) -> None:
    """Outside 0 leaves out the triangles no source reaches -- same result, far fewer draws.
    A fine grid with a single source in one corner: every instance still lands, all in reach."""
    qapp, stage, mk = scene
    _ground(stage, size=200.0, n=201)
    trees = UsdGeom.PointInstancer.Define(stage, "/Trees")
    trees.GetPositionsAttr().Set([Gf.Vec3f(-90, -90, 0)])
    UsdGeom.Sphere.Define(stage, "/Plant").GetRadiusAttr().Set(0.1)
    s = mk("scatter_points")
    for k, v in dict(surface_prim="/Ground", auto_spacing=False, spacing=0.3, max_count=300,
                     inst_path="/Under", near_prims="/Trees", near_radius=5.0, near_softness=0.0,
                     near_outside=0.0).items():
        s.get_param(k).value = v
    s.execute()
    qapp.processEvents()
    p = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/Under")).GetPositionsAttr().Get())
    assert len(p) == 300
    assert (np.hypot(p[:, 0] + 90, p[:, 1] + 90) <= 5.0 + 1e-4).all()


def _brute(points, sources, scales, radius, clear, softness, inside, outside, up):
    """The weight straight from its definition, every point against every source."""
    p, s = np.asarray(points, float), np.asarray(sources, float)
    if up is not None:
        up = np.asarray(up, float)
        p, s = p - np.outer(p @ up, up), s - np.outer(s @ up, up)
    d = np.linalg.norm(p[:, None] - s[None], axis=2)
    r, c = radius * np.asarray(scales), clear * np.asarray(scales)

    def ss(t):
        t = np.clip(t, 0, 1)
        return t * t * (3 - 2 * t)
    g = ss((r - d) / (r * max(softness, 1e-6)))
    h = ss((d - 0.75 * c) / (0.25 * c)) if clear > 0 else np.ones_like(d)
    return np.clip((outside + (inside - outside) * g.max(axis=1)) * h.min(axis=1), 0, 1)


@pytest.mark.parametrize("clear,inside,outside,up", [
    (0.0, 1.0, 0.0, (0, 0, 1)), (0.8, 1.0, 0.2, (0, 0, 1)), (0.5, 0.1, 1.0, None),
])
def test_the_batched_lookup_matches_the_definition(clear, inside, outside, up) -> None:
    rng = np.random.default_rng(3)
    sources = rng.uniform(-30, 30, (400, 3)) * [1, 1, 0.1]
    scales = rng.uniform(0.6, 1.6, 400)
    points = rng.uniform(-35, 35, (5000, 3)) * [1, 1, 0.1]
    w = prox.Proximity(sources, scales, radius=4.0, clear=clear, softness=0.5, inside=inside,
                       outside=outside, up=up)
    want = _brute(points, sources, scales, 4.0, clear, 0.5, inside, outside, up)
    np.testing.assert_allclose(w.sample_many(points), want, atol=1e-12)
    # The cached neighbourhoods give the same answer the second time round.
    np.testing.assert_allclose(w.sample_many(points), want, atol=1e-12)
