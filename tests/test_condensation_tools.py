"""Surface Flow, Trails, Noise Field, and Scatter Points' spacing / keep-away options.

Together they make condensation -- clusters of drops, runs of water between them -- but each
is a general tool, so each is tested on its own terms:

* Surface Flow keeps points on the surface and moves them only along it: down a wall, not
  off a flat top; each runs no further than its Travel; a GeomSubset limits the surface;
* Trails draws one curve per moving point through where it has been, dropping samples where
  it sat still, and can animate the curves growing;
* Noise Field writes a 0..1 primvar, zero outside the named GeomSubsets;
* Scatter Points with Spacing Follows Scale never overlaps instances of different sizes,
  keeps clear of Keep Away From, and Scale Bias skews sizes small.
"""

from __future__ import annotations

import importlib
import math

import numpy as np
import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

flow = importlib.import_module("chunks.dynamics.surface_flow")
trails = importlib.import_module("chunks.modelling.trails")
noise = importlib.import_module("chunks.modelling.noise_field")


def _cylinder(stage, path="/Can", radius=0.05, height=0.2, nu=48, nv=24, capped=True):
    """A Z-up can: quad sides, fan top; the sides are GeomSubset "side", the top "top"."""
    pts, counts, idx = [], [], []
    for j in range(nv + 1):
        for i in range(nu):
            a = 2 * math.pi * i / nu
            pts.append(Gf.Vec3f(radius * math.cos(a), radius * math.sin(a), height * j / nv))
    for j in range(nv):
        for i in range(nu):
            a, b = j * nu + i, j * nu + (i + 1) % nu
            counts.append(4)
            idx += [a, b, b + nu, a + nu]
    side = list(range(len(counts)))
    top = []
    if capped:
        c = len(pts)
        pts.append(Gf.Vec3f(0, 0, height))
        ring = nv * nu
        for i in range(nu):
            top.append(len(counts))
            counts.append(3)
            idx += [ring + i, ring + (i + 1) % nu, c]
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateFaceVertexIndicesAttr(idx)
    UsdGeom.Subset.CreateGeomSubset(mesh, "side", UsdGeom.Tokens.face, side)
    if top:
        UsdGeom.Subset.CreateGeomSubset(mesh, "top", UsdGeom.Tokens.face, top)
    return mesh


@pytest.fixture
def can():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    _cylinder(stage)
    return stage


def _run(surface, starts, **kw):
    settings = dict(force=(0, 0, -1), frames=48, fps=24.0, speed=0.05, wander=0.0, stickiness=0.0,
                    travel=1.0, travel_variation=0.0, release=0.0, seed=1)
    settings.update(kw)
    return flow.simulate(surface, np.asarray(starts, dtype=np.float64), **settings)


# ---------------------------------------------------------------------------
# Surface Flow
# ---------------------------------------------------------------------------


def test_flow_runs_down_a_wall_and_stays_on_it(can):
    surface = flow.collect_surface(can, ["/Can"], 0)
    starts = [(0.06 * math.cos(a), 0.06 * math.sin(a), 0.15) for a in np.linspace(0, 6, 5)]
    pos, nrm, dist = _run(surface, starts, wander=0.4)
    # Dropped onto the wall first, then down it: never off the surface, always lower.
    radial = np.linalg.norm(pos[:, :, :2], axis=2)
    assert np.allclose(radial, 0.05, atol=0.002)
    assert (pos[-1, :, 2] < pos[0, :, 2] - 0.05).all()
    # Down a vertical wall at Speed, give or take the wander.
    assert dist == pytest.approx(np.full(5, 0.05 * 48 / 24.0), rel=0.15)
    # Normals face out of the can.
    assert (np.einsum("ij,ij->i", nrm[-1, :, :2], pos[-1, :, :2]) > 0).all()


def test_flow_stops_on_a_flat_top_and_at_its_travel(can):
    surface = flow.collect_surface(can, ["/Can"], 0)
    pos, _, dist = _run(surface, [(0.0, 0.01, 0.25), (0.05, 0.0, 0.18)], travel=0.03)
    assert dist[0] == 0.0  # on the lid: gravity presses into it, nothing moves
    assert np.allclose(pos[-1, 0], pos[0, 0])
    assert 0.03 <= dist[1] < 0.03 + 0.05 / 24.0 + 1e-6


def test_flow_hangs_at_the_edge_of_a_subset(can):
    """A GeomSubset is the surface: a run reaching its edge stops there."""
    surface = flow.collect_surface(can, ["/Can/side"], 0)
    assert len(surface.v0) == 48 * 24 * 2  # the sides' quads, as triangles; not the lid
    pos, _, _ = _run(surface, [(0.05, 0.0, 0.03)], frames=96)
    assert pos[-1, 0, 2] == pytest.approx(0.0, abs=0.003)


def test_flow_is_repeatable_and_the_seed_varies_it(can):
    surface = flow.collect_surface(can, ["/Can"], 0)
    starts = [(0.05, 0.0, 0.18), (0.0, 0.05, 0.18)]
    a = _run(surface, starts, wander=0.8, stickiness=0.3, release=1.0)[0]
    b = _run(surface, starts, wander=0.8, stickiness=0.3, release=1.0)[0]
    c = _run(surface, starts, wander=0.8, stickiness=0.3, release=1.0, seed=2)[0]
    assert np.array_equal(a, b)
    assert not np.allclose(a, c)


# ---------------------------------------------------------------------------
# Trails
# ---------------------------------------------------------------------------


def _moving_points(stage, path="/Pts"):
    """Two points: one moves 1 unit per frame along X for frames 1..10, one sits still."""
    pts = UsdGeom.Points.Define(stage, path)
    for f in range(1, 11):
        pts.GetPointsAttr().Set([Gf.Vec3f(f - 1, 0, 0), Gf.Vec3f(0, 5, 0)], Usd.TimeCode(f))
    return pts.GetPrim()


def test_trails_draw_one_curve_per_moving_point():
    stage = Usd.Stage.CreateInMemory()
    prim = _moving_points(stage)
    paths = trails.sample_paths(stage, prim, list(range(1, 11)))
    keep = trails.build_trails(paths, 2.5)
    assert keep[0] == [0, 3, 6, 9] and keep[1] == [0]  # still points drop out
    pts, counts, widths = trails._curves_at(paths, keep, 9, np.array([0.2, 0.2]), 0.5)
    assert counts == [4]
    assert widths[0] == pytest.approx(0.1) and widths[-1] == pytest.approx(0.2)
    # Growing: at frame index 4 the trail reaches the point's position then, not further.
    pts, counts, _ = trails._curves_at(paths, keep, 4, np.array([0.2, 0.2]), 1.0)
    assert counts == [3] and tuple(pts[-1]) == (4.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Noise Field
# ---------------------------------------------------------------------------


def test_noise_field_is_smooth_and_in_range():
    rng = np.random.default_rng(0)
    p = rng.uniform(-1, 1, size=(2000, 3))
    v = noise.field(p, size=0.3, threshold=0.5, softness=0.3, seed=4)
    assert v.min() >= 0.0 and v.max() <= 1.0
    assert 0.1 < (v > 0.5).mean() < 0.9
    # Nearby points get nearby values.
    w = noise.field(p + 1e-4, size=0.3, threshold=0.5, softness=0.3, seed=4)
    assert np.abs(v - w).max() < 0.05
    assert not np.allclose(v, noise.field(p, size=0.3, threshold=0.5, softness=0.3, seed=5))


# ---------------------------------------------------------------------------
# The chunks in a scene
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


def test_noise_field_chunk_paints_only_the_named_subsets(scene):
    qapp, stage, mk = scene
    mesh = _cylinder(stage)
    field = mk("noise_field")
    field.get_param("prim_path").value = "/Can"
    field.get_param("subsets").value = "top"
    field.get_param("threshold").value = 0.0
    field.execute()
    qapp.processEvents()
    v = np.asarray(UsdGeom.PrimvarsAPI(mesh).GetPrimvar("density").Get())
    assert len(v) == len(mesh.GetPointsAttr().Get())
    ring = 24 * 48
    assert (v[:ring] == 0).all()  # the side's own vertices, below the lid's rim
    assert (v[ring:] > 0).all()   # the lid


def test_flow_and_trails_chunks_chain(scene):
    qapp, stage, mk = scene
    _cylinder(stage)
    seeds = UsdGeom.Points.Define(stage, "/Seeds")
    seeds.GetPointsAttr().Set([Gf.Vec3f(0.06, 0, 0.15), Gf.Vec3f(-0.06, 0, 0.12)])
    f = mk("surface_flow")
    f.get_param("source").value = "/Seeds"
    f.get_param("surface").value = "/Can"
    f.get_param("frames").value = 24
    f.get_param("release").value = 0.0
    f.get_param("stickiness").value = 0.0
    f.execute()
    t = mk("trails")
    t.get_param("source").value = "/SurfaceFlow"
    t.get_param("grow").value = True
    t.execute()
    qapp.processEvents()
    pts = UsdGeom.Points(stage.GetPrimAtPath("/SurfaceFlow"))
    assert pts.GetPointsAttr().GetNumTimeSamples() == 25
    curves = UsdGeom.BasisCurves(stage.GetPrimAtPath("/Trails"))
    assert len(curves.GetCurveVertexCountsAttr().Get()) == 2
    # Grow: at the first frame nothing has moved yet, so there is no curve.
    assert len(curves.GetCurveVertexCountsAttr().Get(1)) == 0
    assert "2 trails" in t.get_param("readout").value


def _scatter(mk, **params):
    s = mk("scatter_points")
    s.get_param("surface_prim").value = "/Can"
    s.get_param("auto_spacing").value = False
    for k, v in params.items():
        s.get_param(k).value = v
    s.execute()
    return s


def test_spacing_follows_scale_never_overlaps(scene):
    qapp, stage, mk = scene
    _cylinder(stage)
    UsdGeom.Sphere.Define(stage, "/Drop").GetRadiusAttr().Set(0.5)
    _scatter(mk, spacing=0.01, size_spacing=True, scale_min=0.2, scale_max=1.0, scale_bias=2.0,
             max_count=600, max_slope=180.0, inst_path="/Drops")
    qapp.processEvents()
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/Drops"))
    p = np.asarray(inst.GetPositionsAttr().Get())
    s = np.asarray(inst.GetScalesAttr().Get())[:, 0]
    assert len(p) > 100
    r = 0.01 * 0.5 * s / 1.0
    d = np.linalg.norm(p[:, None] - p[None], axis=2) + np.eye(len(p)) * 1e9
    assert (d >= (r[:, None] + r[None]) - 1e-6).all()
    # ...and small ones do pack closer than plain Spacing would ever allow.
    assert d.min() < 0.01 * 0.6
    # Bias 2: most small.
    assert np.median(s) < 0.45


def test_scatter_keeps_away_from_curves(scene):
    qapp, stage, mk = scene
    _cylinder(stage)
    UsdGeom.Sphere.Define(stage, "/Drop").GetRadiusAttr().Set(0.5)
    run = UsdGeom.BasisCurves.Define(stage, "/Run")
    run.GetPointsAttr().Set([Gf.Vec3f(0.05, 0, 0.2), Gf.Vec3f(0.05, 0, 0.0)])
    run.GetCurveVertexCountsAttr().Set([2])
    _scatter(mk, spacing=0.004, max_count=800, max_slope=180.0, inst_path="/Drops",
             avoid_prims="/Run", avoid_distance=0.01)
    qapp.processEvents()
    p = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/Drops")).GetPositionsAttr().Get())
    assert len(p) > 200
    # Distance to the run's line (x = 0.05, y = 0): clear by the distance plus half the spacing.
    d = np.hypot(p[:, 0] - 0.05, p[:, 1])
    assert d.min() >= 0.01 + 0.002 - 1e-6


def test_trails_swell_and_sink_into_the_surface():
    """Width Variation swells and thins a trail along its length; Sink pushes it into the
    surface along the normals by that fraction of its half-width."""
    paths = np.zeros((11, 1, 3))
    paths[:, 0, 2] = -np.arange(11) * 0.5  # straight down a wall facing -Y
    normals = np.tile((0.0, -1.0, 0.0), (11, 1, 1))
    keep = trails.build_trails(paths, 0.1)
    kw = dict(normals=normals, phases=np.array([[0.3, 1.1]]))
    pts, _, w = trails._curves_at(paths, keep, 10, np.array([0.4]), 1.0, variation=0.5, frequency=0.4, **kw)
    assert w.max() > 0.4 * 1.2 and w.min() < 0.4 * 0.8
    assert np.allclose(pts[:, 1], 0.0)  # no sink: on the surface
    pts, _, w = trails._curves_at(paths, keep, 10, np.array([0.4]), 1.0, variation=0.5, frequency=0.4,
                                  sink=0.6, **kw)
    assert np.allclose(pts[:, 1], 0.5 * w * 0.6)  # into the wall, which faces -Y


def test_keeping_off_a_wide_curve_needs_no_spacing(scene):
    """Spacing 0 and Keep Away Distance 0 sampled the curve every half micrometre -- 370
    million points along a road, out of memory. The curve's own width sets the step."""
    qapp, stage, mk = scene
    _cylinder(stage)
    UsdGeom.Sphere.Define(stage, "/Drop").GetRadiusAttr().Set(0.5)
    run = UsdGeom.BasisCurves.Define(stage, "/Run")
    run.GetPointsAttr().Set([Gf.Vec3f(0.05, 0, 0.2), Gf.Vec3f(0.05, 0, 0.0)])
    run.GetCurveVertexCountsAttr().Set([2])
    run.GetWidthsAttr().Set([0.02])
    _scatter(mk, spacing=0.0, max_count=600, max_slope=180.0, inst_path="/Drops", size_spacing=True,
             avoid_prims="/Run", avoid_distance=0.0)
    qapp.processEvents()
    p = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/Drops")).GetPositionsAttr().Get())
    assert len(p) > 200
    assert np.hypot(p[:, 0] - 0.05, p[:, 1]).min() >= 0.01 * 0.97


@pytest.mark.parametrize("axis,amount", [("Z", 360.0), ("X", 30.0), ("All", 25.0), ("Off", 0.0)])
def test_the_batched_spin_is_the_loops_kind_of_spin(axis, amount) -> None:
    """Scatter's upright-and-spun fast path spins as the per-instance loop does -- unit
    quaternions about the axis, up to the amount either way -- and the same seed gives the
    same spin. Not the loop's draws: those are made as arrays."""
    import random
    sp = importlib.import_module("chunks.modelling.scatter_points")
    q = sp._spun_quats(3000, axis, amount, random.Random(7))
    assert q.shape == (3000, 4) and q.dtype == np.float32
    assert np.array_equal(q, sp._spun_quats(3000, axis, amount, random.Random(7)))
    assert np.allclose(np.linalg.norm(q, axis=1), 1.0, atol=1e-6)
    angle = 2.0 * np.degrees(np.arctan2(np.linalg.norm(q[:, :3], axis=1), q[:, 3]))
    assert angle.max() <= amount + 1e-3
    if axis == "Off":
        assert np.array_equal(q, np.tile(np.float32([0, 0, 0, 1]), (3000, 1)))
        return
    assert not np.array_equal(q, sp._spun_quats(3000, axis, amount, random.Random(8)))
    if axis != "All":
        others = [i for i in range(3) if i != "XYZ".index(axis)]
        assert not q[:, others].any()
        # Both ways round, and most of the range: a spread, not a constant.
        assert angle.max() > amount * 0.9 and (q[:, "XYZ".index(axis)] < 0).any()
    else:
        assert (q[:, :3] < 0).any(axis=0).all() and (q[:, :3] > 0).any(axis=0).all()


@pytest.mark.parametrize("pocket", [False, True])
def test_the_batched_density_lookup_is_the_single_point_lookup(pocket) -> None:
    """sample_many takes a padded fast path when the mesh's neighbourhoods are all about one
    size (a ground) and a per-cell path when they are not (a dense pocket). Both must give
    exactly what sample() gives one point at a time.

    The grid's paths, as without scipy: with it, both take the KD-tree's instead."""
    sp = importlib.import_module("chunks.modelling.scatter_points")
    rng = np.random.default_rng(2)
    xs = np.linspace(-50, 50, 60)
    gx, gy = np.meshgrid(xs, xs, indexing="ij")
    verts = np.stack([gx.ravel(), gy.ravel(), rng.uniform(-2, 2, gx.size)], 1)
    if pocket:
        verts = np.vstack([verts, rng.normal(0, 0.3, (4000, 3))])
    w = sp._NearestWeight(verts, rng.random(len(verts)))
    w._tree = False  # no KD-tree: the grid
    pts = np.stack([rng.uniform(-60, 60, 3000), rng.uniform(-60, 60, 3000), rng.uniform(-3, 3, 3000)], 1)
    want = np.array([w.sample(p) for p in pts])
    assert np.array_equal(w.sample_many(pts), want)
