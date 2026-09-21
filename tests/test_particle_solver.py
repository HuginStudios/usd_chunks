"""Particle Solver: the geometry helpers, and the chunk wired to Scatter Points and to an
RBD-style bake. The solver runs on a coarse grid and a handful of frames to stay quick."""

import numpy as np
import pytest
from pxr import Gf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

from scripts.dynamics import particle_solver as ps


def _box(stage, path, lo, hi):
    """A closed, outward-facing box mesh."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set([
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ])
    mesh.GetFaceVertexCountsAttr().Set([4] * 6)
    mesh.GetFaceVertexIndicesAttr().Set([
        0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4, 1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7,
    ])
    return mesh


def _floor(stage, path, half, z):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set([(-half, -half, z), (half, -half, z), (half, half, z), (-half, half, z)])
    mesh.GetFaceVertexCountsAttr().Set([4])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
    return mesh


# ---------------------------------------------------------------------------
# geometry, no Taichi
# ---------------------------------------------------------------------------


def test_fill_triangles_fills_a_closed_box_and_nothing_outside():
    stage = Usd.Stage.CreateInMemory()
    _box(stage, "/box", (0, 0, 0), (10, 10, 10))
    tris = ps._world_triangles(stage.GetPrimAtPath("/box"))
    pts = ps._fill_triangles(*tris, 1.0, np.random.default_rng(0))

    assert len(pts) == pytest.approx(1000, rel=0.05)
    assert pts.min() > -0.3 and pts.max() < 10.3


def test_voxelize_normals_face_the_side_each_node_is_on():
    g = 16
    v0 = np.array([[0, 0, 0.5], [0, 0, 0.5]])
    v1 = np.array([[1, 0, 0.5], [1, 1, 0.5]])
    v2 = np.array([[1, 1, 0.5], [0, 1, 0.5]])
    normals = ps._voxelize(v0, v1, v2, g, np.random.default_rng(0))

    # Node z=9 sits above the plane at z=8 (0.5 * 16), node z=7 below it.
    assert normals[8, 8, 9, 0, 2] == pytest.approx(1.0)
    assert normals[8, 8, 7, 0, 2] == pytest.approx(-1.0)
    assert not normals[8, 8, 12].any()


def test_voxelize_keeps_both_surfaces_where_a_wall_meets_a_floor():
    g = 16
    # Floor at z = 0.25 and a wall at x = 0.25, both spanning the domain.
    v0 = np.array([[0, 0, 0.25], [0, 0, 0.25], [0.25, 0, 0], [0.25, 0, 0]])
    v1 = np.array([[1, 0, 0.25], [1, 1, 0.25], [0.25, 1, 0], [0.25, 1, 1]])
    v2 = np.array([[1, 1, 0.25], [0, 1, 0.25], [0.25, 1, 1], [0.25, 0, 1]])
    normals = ps._voxelize(v0, v1, v2, g, np.random.default_rng(0))

    # The node in the inside corner, just above the floor and just off the wall.
    both = {tuple(np.round(n).astype(int)) for n in normals[5, 8, 5]}
    assert both == {(0, 0, 1), (1, 0, 0)}


def test_blob_frames_recovers_a_rigid_turn():
    rng = np.random.default_rng(1)
    blob = rng.normal(size=(200, 3)) * [3.0, 1.0, 0.5]
    turn = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])  # 90 degrees about Z
    moved = blob @ turn.T + [5.0, 0.0, 0.0]
    centres, rotations = ps._blob_frames(np.stack((blob, moved)), np.zeros(200, np.int64), 1)

    assert centres[1, 0] == pytest.approx(blob.mean(axis=0) @ turn.T + [5.0, 0.0, 0.0])
    expected = np.array([np.cos(np.pi / 4), 0.0, 0.0, np.sin(np.pi / 4)])
    assert np.abs(rotations[1, 0]) == pytest.approx(np.abs(expected), abs=1e-6)


def test_surface_points_are_the_top_layer_only():
    xs, ys, zs = np.meshgrid(np.arange(10), np.arange(10), np.arange(5), indexing="ij")
    column = np.stack((xs.ravel(), ys.ravel(), zs.ravel()), axis=1).astype(float)
    top = ps._surface_points(column, 2, 1.0, 0.0)

    assert len(top) == 100
    assert np.all(top[:, 2] > 4.0)


# ---------------------------------------------------------------------------
# the chunk
# ---------------------------------------------------------------------------

needs_taichi = pytest.mark.skipif(not ps._HAS_TAICHI, reason="taichi not installed")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage
        self.selected = []

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return self.selected


@pytest.fixture
def graph():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene = _Scene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def make(name):
        mod = reg.get_chunk_module(name)
        chunks = mod.create() if mod.loaded else mod.load()
        for c in chunks:
            scene.add_chunk(c)
        return chunks[0], mod.module

    make.scene = scene
    return stage, make


def _solver(make, **params):
    node, module = make("particle_solver")
    defaults = dict(grid_res=24, frame_count=8, fps=24.0)
    for name, value in {**defaults, **params}.items():
        node.get_param(name).value = value
    return node, module


def _press_run(node, module):
    button = node.get_param("run_sim")
    module._run(button, None, button)


@needs_taichi
def test_water_poured_on_a_floor_lands_on_it_and_feeds_scatter(graph):
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    _box(stage, "/tank", (-10, -10, 20), (10, 10, 40))

    solver, module = _solver(make, source_prim="/tank", collision_path="/floor", frame_count=24)
    _press_run(solver, module)

    points = UsdGeom.Points(stage.GetPrimAtPath("/ParticleSim/Particles")).GetPointsAttr()
    assert points.GetNumTimeSamples() == 24
    first, last = np.asarray(points.Get(1)), np.asarray(points.Get(24))
    assert last[:, 2].mean() < first[:, 2].mean() - 5.0, "the water should have fallen"
    assert last[:, 2].min() > -2.0, "and stopped on the floor, not gone through it"
    assert solver.get_param("surface_out").value == "/ParticleSim/SurfacePoints"

    # Surface Points is what Scatter Points' Painted Points input reads.
    surface = UsdGeom.Points(stage.GetPrimAtPath("/ParticleSim/SurfacePoints")).GetPointsAttr().Get()
    assert len(surface) > 0
    scatter, _ = make("scatter_points")
    scatter.get_param("points_prim").value = solver.get_param("surface_out").value
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ScatterInstancer"))
    assert len(instancer.GetPositionsAttr().Get()) == len(surface)


@needs_taichi
def test_instanced_spheres_carry_the_material(graph):
    """V-Ray draws a Points cloud as one point however wide, so anything to be rendered needs
    real geometry: a sphere per particle, with the material on the prototype."""
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    _box(stage, "/tank", (-10, -10, 20), (10, 10, 30))
    material = UsdShade.Material.Define(stage, "/Looks/Water")

    solver, module = _solver(make, source_prim="/tank", collision_path="/floor",
                             render_as=ps._AS_SPHERES, material_prim="/Looks/Water")
    _press_run(solver, module)

    spheres = UsdGeom.PointInstancer(stage.GetPrimAtPath("/ParticleSim/ParticleSpheres"))
    particles = UsdGeom.Points(stage.GetPrimAtPath("/ParticleSim/Particles")).GetPointsAttr()
    assert spheres.GetPositionsAttr().GetNumTimeSamples() == particles.GetNumTimeSamples()
    assert len(spheres.GetPositionsAttr().Get(1)) == len(particles.Get(1))

    proto = stage.GetPrimAtPath("/ParticleSim/ParticleSpheres/Prototypes/Particle")
    assert proto.IsA(UsdGeom.Sphere)
    for prim in (proto, stage.GetPrimAtPath("/ParticleSim/Particles")):
        bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        assert bound.GetPrim() == material.GetPrim()


@needs_taichi
def test_brush_points_become_blobs(graph):
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    # As the brush writes them: on the surface, with the surface's facing recorded.
    painted = UsdGeom.Points.Define(stage, "/painted")
    painted.GetPointsAttr().Set([(0, 0, 0), (30, 0, 0)])
    painted.GetNormalsAttr().Set([(0, 0, 1), (0, 0, 1)])

    solver, module = _solver(make, source_prim="/painted", collision_path="/floor",
                             blob_radius=4.0, material=ps.SNOW)
    _press_run(solver, module)

    first = np.asarray(UsdGeom.Points(stage.GetPrimAtPath("/ParticleSim/Particles")).GetPointsAttr().Get(1))
    assert first[:, 2].min() > -0.5, "each ball starts on top of the surface, not half inside it"
    # Blob Radius plus the lift and a particle's jitter within its own cell.
    reach = 4.0 + 3.0
    near_first = np.linalg.norm(first[:, :2] - [0, 0], axis=1) <= reach
    near_second = np.linalg.norm(first[:, :2] - [30, 0], axis=1) <= reach
    assert near_first.any() and near_second.any() and np.all(near_first | near_second)


def test_the_selection_fills_source_but_not_the_optional_prims(graph):
    """Reported as a crash: created with the painted surface selected, the solver took that
    surface as its Container too -- the domain grew to the whole surface."""
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    make.scene.selected = [stage.GetPrimAtPath("/floor")]
    solver, _ = make("particle_solver")

    assert solver.get_param("source_prim").value == "/floor"
    assert solver.get_param("container_prim").value == ""
    assert solver.get_param("drive_instancer").value == ""


def test_blob_radius_defaults_to_the_brush_size(graph):
    stage, make = graph
    painted = UsdGeom.Points.Define(stage, "/painted")
    painted.GetPointsAttr().Set([(0, 0, 0)])
    painted.GetWidthsAttr().Set([2.5])  # a brush of radius 10 records a quarter of it

    assert ps._auto_blob_radius(stage.GetPrimAtPath("/painted"), stage) == pytest.approx(5.0)


@needs_taichi
def test_instances_ride_the_flow_when_driven(graph):
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    _box(stage, "/tank", (-10, -10, 20), (10, 10, 40))
    inst = UsdGeom.PointInstancer.Define(stage, "/debris")
    UsdGeom.Cube.Define(stage, "/debris/proto").GetSizeAttr().Set(1.0)
    inst.GetPrototypesRel().SetTargets(["/debris/proto"])
    inst.GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 38), Gf.Vec3f(5, 5, 30)]))
    inst.GetProtoIndicesAttr().Set([0, 0])

    solver, module = _solver(make, source_prim="/tank", collision_path="/floor",
                             drive_instancer="/debris", frame_count=16)
    _press_run(solver, module)

    positions = inst.GetPositionsAttr()
    assert positions.GetNumTimeSamples() == 16
    assert positions.Get(1)[0][2] == pytest.approx(38.0, abs=0.01), "frame 1 is where it was scattered"
    assert positions.Get(16)[0][2] < 30.0, "carried down with the water"


@needs_taichi
def test_instances_follow_their_own_blob(graph):
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    inst = UsdGeom.PointInstancer.Define(stage, "/apples")
    UsdGeom.Sphere.Define(stage, "/apples/proto").GetRadiusAttr().Set(3.0)
    inst.GetPrototypesRel().SetTargets(["/apples/proto"])
    inst.GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 20), Gf.Vec3f(15, 0, 25)]))
    inst.GetProtoIndicesAttr().Set([0, 0])

    solver, module = _solver(make, source_prim="/apples", drive_instancer="/apples",
                             collision_path="/floor", material=ps.JELLY, frame_count=16)
    _press_run(solver, module)

    positions = inst.GetPositionsAttr()
    assert positions.Get(1)[1][2] == pytest.approx(25.0, abs=0.5)
    assert positions.Get(16)[1][2] < 20.0
    assert "each follows its own blob" in solver.get_param("readout").value


@needs_taichi
def test_an_rbd_bake_pushes_the_material_aside(graph):
    stage, make = graph
    _floor(stage, "/floor", 100.0, 0.0)
    _box(stage, "/pool", (-15, -15, 0.5), (15, 15, 8))
    # What the RBD Solver leaves behind: an instancer whose positions are time samples.
    rock = UsdGeom.PointInstancer.Define(stage, "/rocks")
    UsdGeom.Sphere.Define(stage, "/rocks/proto").GetRadiusAttr().Set(5.0)
    rock.GetPrototypesRel().SetTargets(["/rocks/proto"])
    rock.GetProtoIndicesAttr().Set([0])
    frames = 12
    # Falls from 20 to rest on the floor, as a rigid-body bake would leave it -- not sunk into it.
    for f in range(1, frames + 1):
        z = 20.0 - 14.5 * (f - 1) / (frames - 1)
        rock.GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, z)]), f)

    # A container tight around the pool, so a cell is a fraction of the rock rather than as
    # big as it.
    _box(stage, "/tank", (-20, -20, 0), (20, 20, 25))
    UsdGeom.Imageable(stage.GetPrimAtPath("/tank")).MakeInvisible()

    def near_rock(colliders, out):
        # Separate output paths: two solvers writing one path would read back whichever
        # chunk's layer is stronger, not the run just made.
        solver, module = _solver(make, source_prim="/pool", collision_path=colliders, sim_path=out,
                                 container_prim="/tank", frame_count=frames, grid_res=40)
        _press_run(solver, module)
        last = np.asarray(UsdGeom.Points(stage.GetPrimAtPath(f"{out}/Particles")).GetPointsAttr().Get(frames))
        return int((np.linalg.norm(last - [0.0, 0.0, 5.5], axis=1) < 4.0).sum())

    undisturbed = near_rock("/floor", "/Calm")
    assert undisturbed > 20
    assert near_rock("/floor\n/rocks", "/Splash") < undisturbed * 0.2, "the rock should have cleared a hole"
