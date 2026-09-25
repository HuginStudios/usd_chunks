"""Breakage: Fracture -> RBD Solver (Breakable), driven by UsdPhysics.

What has to hold for breakage to mean anything:

* a fracture conserves material -- every node's children tile it exactly, at any depth,
  for procedural rock and for an arbitrary closed (even concave, UV-mapped) model;
* the surface survives the cuts -- the model's own UVs outside, a projection on the cuts,
  and inside/outside groups that partition every face;
* breakage is load-driven, and material is conserved frame by frame in the simulation;
* the world moves the way a UsdPhysics scene says: a revolute joint's velocity drive spins
  its body, an animated collider drags what it touches, a surface velocity carries it;
* the bake is what the simulation did, parked bodies hidden and never at the parking lot.
"""

from __future__ import annotations

import importlib
import math

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

fr = importlib.import_module("scripts.dynamics.fracture")
rig = importlib.import_module("scripts.dynamics.crusher_rig")
rbd = importlib.import_module("scripts.dynamics.rbd_solver")
brk = rbd._breakage()

needs_manifold = pytest.mark.skipif(not fr._HAS_MANIFOLD, reason="needs manifold3d")
needs_mujoco = pytest.mark.skipif(not brk._HAS_MUJOCO, reason="needs mujoco")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _torus(stage, path="/Torus", R=20.0, r=6.0, nu=24, nv=12, at=(0.0, 50.0, 0.0), closed=True):
    """A quad torus with faceVarying UVs -- concave, seamed, and not at the origin."""
    pts, faces, uvs = [], [], []
    for i in range(nu):
        for j in range(nv):
            u, v = 2 * math.pi * i / nu, 2 * math.pi * j / nv
            pts.append(Gf.Vec3f((R + r * math.cos(v)) * math.cos(u), r * math.sin(v), (R + r * math.cos(v)) * math.sin(u)))
    for i in range(nu if closed else nu - 1):
        for j in range(nv):
            a, b = i * nv + j, ((i + 1) % nu) * nv + j
            c, d = ((i + 1) % nu) * nv + (j + 1) % nv, i * nv + (j + 1) % nv
            faces += [a, b, c, d]
            uvs += [((i + di) / nu, (j + dj) / nv) for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1))]
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexCountsAttr([4] * (len(faces) // 4))
    mesh.CreateFaceVertexIndicesAttr(faces)
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray,
                                            UsdGeom.Tokens.faceVarying).Set(uvs)
    UsdGeom.Xformable(mesh).AddTranslateOp().Set(Gf.Vec3d(*at))
    return mesh.GetPrim()


def _rock_trees(count=3, generations=2, seed=3, at=(300.0, 0.0, 0.0)):
    rng = np.random.default_rng(seed)
    trees = []
    for r in range(count):
        solid = fr._mf.Manifold.hull_points(fr.random_rock_points(rng, 15.0) + np.asarray(at) + (40.0 * r, 0, 0))
        solid = solid.as_original()
        trees.append(fr.Tree(r, f"Rock{r:02d}", fr.fracture(r, solid, rng, generations, 0.35),
                             solid.original_id(), False, colour=fr._PALETTE[r]))
    return trees


# ---------------------------------------------------------------------------
# Fracture
# ---------------------------------------------------------------------------


@needs_manifold
def test_rock_fracture_conserves_volume_at_every_node():
    for tree in _rock_trees(count=2, generations=4):
        by_node = {p.node: p for p in tree.pieces}
        assert len(tree.pieces) > 8
        for p in tree.pieces:
            if p.children:
                assert sum(by_node[c].volume for c in p.children) == pytest.approx(p.volume, rel=1e-5)
                for c in p.children:
                    assert np.allclose(by_node[c].centre - p.centre, by_node[c].offset)


@needs_manifold
def test_a_uv_mapped_concave_model_fractures_with_its_surface():
    stage = Usd.Stage.CreateInMemory()
    solid, has_uv, problem = fr._source_solid(_torus(stage))
    assert problem == "" and has_uv
    solid = solid.as_original()
    pieces = fr.fracture(0, solid, np.random.default_rng(1), 3, 0.35)
    by_node = {p.node: p for p in pieces}
    for p in pieces:
        if p.children:
            assert sum(by_node[c].volume for c in p.children) == pytest.approx(p.volume, rel=1e-5)

    tree = fr.Tree(0, "Torus", pieces, solid.original_id(), has_uv)
    # Materials are bound by path: they need not exist on the stage being written.
    fr.author(stage, Sdf.Path("/Fractured"), [tree], uv_scale=0.1,
              outside_material="/Looks/Paint", inside_material="/Looks/Broken")
    leaf = next(p for p in pieces if p.generation == 3)
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath(f"/Fractured/Prototypes/Torus_n{leaf.node}"))
    n_faces = len(mesh.GetFaceVertexCountsAttr().Get())
    inside = set(UsdGeom.Subset(stage.GetPrimAtPath(mesh.GetPath().AppendChild("inside"))).GetIndicesAttr().Get())
    outside = set(UsdGeom.Subset(stage.GetPrimAtPath(mesh.GetPath().AppendChild("outside"))).GetIndicesAttr().Get())
    assert inside and outside and not (inside & outside) and len(inside | outside) == n_faces
    for name, target in (("outside", "/Looks/Paint"), ("inside", "/Looks/Broken")):
        rel = stage.GetPrimAtPath(mesh.GetPath().AppendChild(name)).GetRelationship("material:binding")
        assert rel.GetTargets() == [Sdf.Path(target)]
    # The outside keeps the model's own UVs, which live in [0, 1] on this torus.
    st = np.asarray(UsdGeom.PrimvarsAPI(mesh).GetPrimvar("st").Get()).reshape(-1, 3, 2)
    out_uv = st[sorted(outside)]
    assert out_uv.min() >= -1e-4 and out_uv.max() <= 1 + 1e-4


@needs_manifold
def test_an_open_mesh_falls_back_to_its_hull_and_says_so():
    stage = Usd.Stage.CreateInMemory()
    solid, has_uv, problem = fr._source_solid(_torus(stage, closed=False))
    assert solid is not None and not has_uv
    assert "convex hull" in problem


# ---------------------------------------------------------------------------
# The world, read the UsdPhysics way
# ---------------------------------------------------------------------------


def test_rig_is_a_usdphysics_scene_with_the_right_spin_directions():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")
    rig.author_rig(stage, Sdf.Path("/Rig"), rig.build_parts(), False)
    spins = brk._joint_spins(stage)
    assert len(spins) == 4
    belt = np.asarray(stage.GetPrimAtPath("/Rig/Belt").GetAttribute(brk.SURFACE_VELOCITY).Get())

    def top_velocity(name):
        _, _pivot, axis, deg_s = spins[Sdf.Path(f"/Rig/{name}")]
        return np.cross(np.asarray(axis) * deg_s, (0.0, 1.0, 0.0))

    assert top_velocity("RollerA")[2] > 0.0  # at -Z: its top feeds the nip
    assert top_velocity("RollerB")[2] < 0.0
    for drum in ("DrumHead", "DrumTail"):
        assert top_velocity(drum) @ belt > 0.0
    for p in stage.Traverse():
        if p.IsA(UsdGeom.Mesh) and p.GetName() != "Emitter":
            assert p.HasAPI(UsdPhysics.CollisionAPI), p.GetPath()


def _box(stage, path, centre, size):
    v, t = rig.box(centre, size)
    m = UsdGeom.Mesh.Define(stage, path)
    m.CreatePointsAttr([Gf.Vec3f(*map(float, p)) for p in v])
    m.CreateFaceVertexCountsAttr([3] * len(t))
    m.CreateFaceVertexIndicesAttr([int(i) for i in t.reshape(-1)])
    return m.GetPrim()


def _one_rock_job(stage, colliders, frames=48, strength=0.0, start=(0.0, 12.0, 0.0)):
    trees = _rock_trees(count=1, generations=1, at=(0.0, 0.0, 0.0))
    fr.author(stage, Sdf.Path("/Fractured"), trees, uv_scale=0.05)
    frac = brk.read_fracture(stage, stage.GetPrimAtPath("/Fractured"))
    world = brk.gather_world(stage, [stage.GetPrimAtPath(c) for c in colliders], frames, 1, 24.0, 0.0, 3.0)
    job = dict(
        up_idx=1, gravity=-981.0, fps=24.0, frames=frames, substeps=8, friction=0.8, restitution=0.0,
        density=1.0, statics=world["statics"], belts=world["belts"], drivers=world["drivers"],
        pieces=frac["pieces"], starts=[dict(key=(0, 0), pos=np.asarray(start), quat=np.array([1.0, 0, 0, 0]))],
        breakage=dict(strength=strength), seed=0,
    )
    return job, world


@needs_mujoco
@needs_manifold
def test_a_surface_velocity_carries_what_rests_on_it():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")
    belt = _box(stage, "/World/Belt", (0.0, -2.0, 0.0), (400.0, 4.0, 100.0))
    belt.CreateAttribute(brk.SURFACE_VELOCITY, Sdf.ValueTypeNames.Vector3f).Set(Gf.Vec3f(60.0, 0.0, 0.0))
    job, world = _one_rock_job(stage, ["/World"])
    assert len(world["belts"]) == 1
    res = brk.simulate(job)
    x = res["positions"][:, 0, 0]
    assert x[-1] - x[12] > 30.0  # ~60/s for 1.5 s, minus the time to grip


@needs_mujoco
@needs_manifold
def test_an_animated_collider_drags_rather_than_teleports():
    """A keyed pusher sweeping through a resting body moves it along -- with the pusher's
    velocity, not by being popped out sideways."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")
    _box(stage, "/World/Floor", (0.0, -2.0, 0.0), (600.0, 4.0, 200.0))
    pusher = _box(stage, "/World/Pusher", (0.0, 10.0, 0.0), (4.0, 20.0, 60.0))
    op = UsdGeom.Xformable(pusher).AddTranslateOp()
    op.Set(Gf.Vec3d(-40.0, 0, 0), 1)
    op.Set(Gf.Vec3d(80.0, 0, 0), 49)
    job, world = _one_rock_job(stage, ["/World"], frames=48)
    assert len(world["drivers"]) == 1 and not world["spins"]
    res = brk.simulate(job)
    assert res["positions"][-1, 0, 0] > 40.0


@needs_mujoco
@needs_manifold
def test_a_joint_drive_spins_its_body_and_the_spin_is_baked():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")
    wheel = _box(stage, "/World/Wheel", (0.0, 0.0, 0.0), (60.0, 4.0, 60.0))
    UsdPhysics.RigidBodyAPI.Apply(wheel).CreateKinematicEnabledAttr(True)
    joint = UsdPhysics.RevoluteJoint.Define(stage, "/World/Joint")
    joint.CreateBody1Rel().SetTargets([wheel.GetPath()])
    joint.CreateAxisAttr("Y")
    UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "angular").CreateTargetVelocityAttr(90.0)
    job, world = _one_rock_job(stage, ["/World/Wheel"], frames=24, start=(20.0, 8.0, 0.0))
    assert len(world["spins"]) == 1
    res = brk.simulate(job)
    # A body riding a turntable goes round with it: 90 deg/s for ~1 s.
    p0, p1 = res["positions"][6, 0], res["positions"][-1, 0]
    turned = math.degrees(math.atan2(p1[2], p1[0]) - math.atan2(p0[2], p0[0]))
    assert abs(turned) > 45.0

    frac = brk.read_fracture(stage, stage.GetPrimAtPath("/Fractured"))
    brk.author_result(stage, stage.GetPrimAtPath("/Fractured"), res, frac["proto"], 1, world["spins"])
    order = list(UsdGeom.Xformable(wheel).GetXformOpOrderAttr().Get())
    assert order[-2] == "xformOp:orient:rbdSpin"
    assert UsdGeom.Xformable(wheel).ComputeLocalToWorldTransform(1) != UsdGeom.Xformable(wheel).ComputeLocalToWorldTransform(12)


# ---------------------------------------------------------------------------
# Crusher: breakage under load
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def crushed():
    if not (brk._HAS_MUJOCO and fr._HAS_MANIFOLD):
        pytest.skip("needs mujoco and manifold3d")
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")
    rig.author_rig(stage, Sdf.Path("/Rig"), rig.build_parts(), False)
    fr.author(stage, Sdf.Path("/Fractured"), _rock_trees(), uv_scale=0.05)
    frac = brk.read_fracture(stage, stage.GetPrimAtPath("/Fractured"))
    world = brk.gather_world(stage, [stage.GetPrimAtPath("/Rig")], 60, 1, 24.0, 0.0, 3.0,
                             exclude=["/Rig/Emitter"])
    assert not any(name == "Emitter" for _, name, _ in world["found"])
    ev = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Rig/Emitter")).GetPointsAttr().Get())
    box = (ev.min(axis=0), ev.max(axis=0))

    def job(strength):
        return dict(
            up_idx=1, gravity=-981.0, fps=24.0, frames=60, substeps=8, friction=0.6, restitution=0.1,
            density=1.0, statics=world["statics"], belts=world["belts"], drivers=world["drivers"],
            pieces=frac["pieces"],
            emitter=dict(lo=box[0], hi=box[1], count=3, rate=6.0,
                         velocity=np.array([0.0, -150.0, 0.0]), spin=1.0,
                         roots=[k for k, p in frac["pieces"].items() if p["generation"] == 0]),
            breakage=dict(strength=strength, variation=0.2, cooldown=0.04, kick=10.0), seed=1,
        )

    return stage, frac, world, brk.simulate(job(40.0)), job


def test_rock_breaks_between_the_rollers(crushed):
    _, _, _, result, _ = crushed
    assert result["stats"]["bodies"] == 3
    assert result["stats"]["breaks"] > 0


def test_strength_zero_breaks_nothing(crushed):
    *_, job = crushed
    j = job(0.0)
    j["frames"] = 40
    assert brk.simulate(j)["stats"]["breaks"] == 0


def test_material_is_conserved_at_every_frame(crushed):
    _, frac, _, result, _ = crushed
    vol = np.array([frac["pieces"][k]["volume"] for k in result["slot_key"]])
    roots = np.array([frac["pieces"][k]["generation"] == 0 for k in result["slot_key"]])
    for f in range(result["alive"].shape[0]):
        started = result["alive"][: f + 1][:, roots].any(axis=0)
        assert vol[result["alive"][f]].sum() == pytest.approx(vol[roots][started].sum(), rel=1e-5)


def test_bake_hides_parked_bodies_and_keeps_them_near_the_scene(crushed):
    stage, frac, world, result, _ = crushed
    brk.author_result(stage, stage.GetPrimAtPath("/Fractured"), result, frac["proto"], 1, world["spins"])
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/Fractured"))
    n = len(result["slot_key"])
    assert len(inst.GetProtoIndicesAttr().Get()) == n
    for f in (0, 30, 59):
        assert set(inst.GetInvisibleIdsAttr().Get(1 + f)) == {i for i in range(n) if not result["alive"][f][i]}
        assert np.abs(np.asarray(inst.GetPositionsAttr().Get(1 + f))).max() < 1e3


# ---------------------------------------------------------------------------
# The chunks in a scene
# ---------------------------------------------------------------------------


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
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


@needs_manifold
def test_fracture_chunk_replaces_a_model_with_its_instancer(scene):
    qapp, stage, mk = scene
    torus = _torus(stage)
    frac = mk("fracture")
    frac.get_param("source_prims").value = str(torus.GetPath())
    frac.get_param("generations").value = 2
    frac.execute()
    qapp.processEvents()
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/Fractured"))
    assert inst and len(inst.GetProtoIndicesAttr().Get()) == 1
    assert UsdGeom.Imageable(torus).ComputeVisibility() == UsdGeom.Tokens.invisible
    assert "pieces" in frac.get_param("readout").value


@needs_mujoco
@needs_manifold
def test_fracture_rig_and_rbd_solver_crush_rock(scene):
    qapp, stage, mk = scene
    frac = mk("fracture")
    frac.get_param("source").value = fr.SOURCE_ROCKS
    frac.get_param("source_prims").value = ""
    frac.get_param("count").value = 3
    frac.get_param("generations").value = 2
    frac.get_param("rock_origin").value = (300.0, 0.0, 0.0)
    machine = mk("crusher_rig")
    solver = mk("rbd_solver")
    frac.execute()
    machine.execute()
    qapp.processEvents()

    solver.get_param("prim_path").value = "/Fractured"
    solver.get_param("collision_path").value = "/CrusherRig"
    solver.get_param("emitter_prim").value = "/CrusherRig/Emitter"
    solver.get_param("drop_count").value = 2
    solver.get_param("frame_count").value = 30
    solver.get_param("substeps").value = 6
    rbd._run(solver, None, None)
    qapp.processEvents()

    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/Fractured"))
    assert inst.GetPositionsAttr().GetNumTimeSamples() == 30
    # Dropped from the emitter (a guide prim), not started where the 3 laid-out rocks are:
    # 2 drops of a 2-generation tree is 2 x 7 slots.
    assert len(inst.GetProtoIndicesAttr().Get()) == 14
    assert "bodies" in solver.get_param("readout").value
    # The bake's layer must win over the rig's, or the rollers' spin hides under its order.
    order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/CrusherRig/RollerA")).GetXformOpOrderAttr().Get())
    assert "xformOp:orient:rbdSpin" in order


@needs_manifold
def test_fracture_does_not_recut_when_nothing_changed(scene, monkeypatch):
    """A cook with the same params and the same source is free; a real change re-cuts."""
    qapp, stage, mk = scene
    _torus(stage)
    frac = mk("fracture")
    frac.get_param("source_prims").value = "/Torus"
    frac.get_param("generations").value = 1
    frac.execute()
    # Chunk modules load twice under different names; patch the copy execute() runs in.
    mod = frac._module
    mod._CACHE.clear()  # an earlier test may have cut this very torus
    real = mod.build_trees
    calls = []
    monkeypatch.setattr(mod, "build_trees", lambda *a, **k: calls.append(1) or real(*a, **k))
    frac.execute()
    frac.execute()
    assert calls == []
    frac.get_param("generations").value = 2
    frac.execute()
    assert calls
    assert UsdGeom.PointInstancer(stage.GetPrimAtPath("/Fractured"))


@needs_manifold
def test_fracture_shows_rock_settings_only_for_rocks(scene):
    qapp, stage, mk = scene
    frac = mk("fracture")
    from usd_chunks.chunk_flags import ChunkFlags

    def hidden(name):
        return frac.get_param(name).has_state(ChunkFlags.State.HIDDEN)

    assert hidden("Rocks") and not hidden("source_prims")
    frac.get_param("source").value = fr.SOURCE_ROCKS
    assert not hidden("Rocks") and hidden("source_prims")


def test_physics_drive_makes_any_prim_spin_or_carry(scene):
    """The generic way in: a plain mesh plus a Physics Drive chunk, no rig involved."""
    qapp, stage, mk = scene
    _box(stage, "/World/Gear", (10.0, 5.0, 0.0), (40.0, 10.0, 40.0))
    _box(stage, "/World/Belt", (0.0, -20.0, 0.0), (200.0, 4.0, 40.0))
    spin = mk("physics_drive")
    spin.get_param("prim_path").value = "/World/Gear"
    spin.get_param("mode").value = "Spin"
    spin.get_param("axis").value = "Y"
    spin.get_param("rpm").value = 20.0
    spin.execute()
    belt = mk("physics_drive")
    belt.get_param("prim_path").value = "/World/Belt"
    belt.get_param("mode").value = "Conveyor"
    belt.get_param("belt_direction").value = (1.0, 0.0, 0.0)
    belt.get_param("belt_speed").value = 50.0
    belt.execute()
    qapp.processEvents()

    world = brk.gather_world(stage, [stage.GetPrimAtPath("/World")], 12, 1, 24.0, 0.0, 3.0)
    found = {name: (kind, note) for kind, name, note in world["found"]}
    assert found["Gear"][0] == "spinning" and "20 rpm" in found["Gear"][1]
    assert found["Belt"] == ("conveyor", "50 units/s")
    # Spinning about the gear's own bounds centre, not the world origin.
    assert np.allclose(world["drivers"][0]["pos"][0], (10.0, 5.0, 0.0))
    assert "spins 20 rpm" in spin.get_param("readout").value


def test_convex_parts_collide_as_one_hull_and_concave_ones_as_prisms():
    """A box or a cone is one geom (MuJoCo's hull is the shape); a bowl, an open plate or a
    toothed roller keeps the per-triangle prisms that make concave shapes collide right."""
    parts = {p.name: p for p in rig.build_parts()}
    assert brk.is_convex_solid(parts["Floor"].verts, parts["Floor"].tris)
    assert brk.is_convex_solid(parts["DrumHead"].verts, parts["DrumHead"].tris)  # lathe: welded first
    assert not brk.is_convex_solid(parts["RollerA"].verts, parts["RollerA"].tris)  # teeth
    assert not brk.is_convex_solid(parts["Bucket"].verts, parts["Bucket"].tris)  # a cup
    v, t = rig.box((0, 0, 0), (10, 10, 10))
    assert not brk.is_convex_solid(v, t[:10])  # open: two triangles missing


@needs_manifold
def test_bevel_rounds_pieces_and_keeps_inside_and_outside():
    stage = Usd.Stage.CreateInMemory()
    trees = _rock_trees(count=1, generations=2)
    fr.author(stage, Sdf.Path("/Sharp"), trees, uv_scale=0.1)
    fr.author(stage, Sdf.Path("/Round"), trees, uv_scale=0.1, bevel=0.6, bevel_detail=3)
    sharp = UsdGeom.Mesh(stage.GetPrimAtPath("/Sharp/Prototypes/Rock00_n3"))
    round_ = UsdGeom.Mesh(stage.GetPrimAtPath("/Round/Prototypes/Rock00_n3"))
    n_faces = len(round_.GetFaceVertexCountsAttr().Get())
    assert n_faces > 4 * len(sharp.GetFaceVertexCountsAttr().Get())
    inside = set(UsdGeom.Subset(stage.GetPrimAtPath("/Round/Prototypes/Rock00_n3/inside")).GetIndicesAttr().Get())
    outside = set(UsdGeom.Subset(stage.GetPrimAtPath("/Round/Prototypes/Rock00_n3/outside")).GetIndicesAttr().Get())
    assert inside and outside and not (inside & outside) and len(inside | outside) == n_faces


@needs_manifold
def test_chunky_cuts_leave_chunkier_pieces():
    def mean_chunkiness(candidates):
        rng = np.random.default_rng(3)
        solid = fr._mf.Manifold.hull_points(fr.random_rock_points(rng, 15.0)).as_original()
        pieces = fr.fracture(0, solid, rng, 4, 0.35, candidates=candidates)
        return np.mean([fr.chunkiness(p.solid) for p in pieces if p.generation == 4])

    assert mean_chunkiness(8) > mean_chunkiness(1) + 0.05
