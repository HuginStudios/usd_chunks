"""Rock crushing: Rock Library -> Crusher Rig -> Crusher Solver.

What has to hold for the crusher to mean anything:

* material is conserved -- a rock's two pieces tile it, so at every frame the rock alive in
  the simulation adds up to exactly the rock that has been dropped;
* breakage is load-driven -- the same drop with Strength 0 breaks nothing;
* the bake is what the simulation did -- a parked body is invisible, and nowhere near the
  parking lot it was actually sitting in;
* the rollers visibly turn, which needs the bake's layer to win over the rig's.
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

rl = importlib.import_module("scripts.dynamics.rock_library")
rig = importlib.import_module("scripts.dynamics.crusher_rig")
cs = importlib.import_module("scripts.dynamics.crusher_solver")

needs_mujoco = pytest.mark.skipif(not cs._HAS_MUJOCO, reason="needs mujoco")
needs_scipy = pytest.mark.skipif(not rl._HAS_SCIPY, reason="needs scipy")


# ---------------------------------------------------------------------------
# Rock Library
# ---------------------------------------------------------------------------


@needs_scipy
def test_every_break_conserves_volume_and_stays_inside_its_parent():
    library = rl.build_library(count=3, generations=3, seed=4)
    for tree in library:
        assert len(tree) == rl.nodes_per_rock(3)
        for n, piece in enumerate(tree):
            if piece is None or 2 * n + 2 >= len(tree) or tree[2 * n + 1] is None:
                continue
            a, b = tree[2 * n + 1], tree[2 * n + 2]
            assert a.volume + b.volume == pytest.approx(piece.volume, rel=1e-6)
            # Each child, placed by its offset, lies inside its parent's hull.
            from scipy.spatial import Delaunay

            inside = Delaunay(piece.points)
            for child in (a, b):
                assert (inside.find_simplex(child.points + child.offset, tol=1e-6) >= 0).all()


@needs_scipy
def test_library_round_trips_through_usd():
    stage = Usd.Stage.CreateInMemory()
    library = rl.build_library(count=2, generations=2, seed=1)
    rl.author_library(stage, Sdf.Path("/Lib"), library, visible=False)
    read = cs.read_library(stage, "/Lib")
    assert sorted(read) == [0, 1]
    for tree in library:
        for piece in tree:
            got = read[piece.rock][piece.node]
            assert got["generation"] == piece.generation
            assert got["volume"] == pytest.approx(piece.volume, rel=1e-5)
            assert np.allclose(got["offset"], piece.offset, atol=1e-4)
            assert np.allclose(got["points"].mean(axis=0), piece.points.mean(axis=0), atol=1e-4)


# ---------------------------------------------------------------------------
# Crusher Rig
# ---------------------------------------------------------------------------


def test_rig_parts_are_closed_outward_and_tagged():
    parts = rig.build_parts()
    for part in parts:
        p = part.verts[part.tris]
        signed = np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0
        assert signed > 0.0, f"{part.name} is wound inside out"
    roles = [part.role for part in parts]
    assert roles.count("rotor") == 4  # two rollers, two belt drums
    assert roles.count("conveyor") == 1
    assert roles.count("emitter") == 1


def test_rollers_feed_the_nip_and_drums_run_with_the_belt():
    parts = {p.name: p for p in rig.build_parts()}

    def top_velocity(part):
        axis = np.asarray(part.attrs["axis"], dtype=float) * part.attrs["rpm"]
        top = np.array([0.0, 1.0, 0.0])
        return np.cross(axis, top)

    # Roller A sits at -Z, so its top must move +Z (into the nip); B the opposite.
    assert top_velocity(parts["RollerA"])[2] > 0.0
    assert top_velocity(parts["RollerB"])[2] < 0.0
    belt = np.asarray(parts["Belt"].attrs["beltVelocity"])
    for drum in ("DrumHead", "DrumTail"):
        assert top_velocity(parts[drum]) @ belt > 0.0


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


def _small_scene(generations=2):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Y")
    rig.author_rig(stage, Sdf.Path("/Rig"), rig.build_parts(), False)
    rl.author_library(stage, Sdf.Path("/Lib"), rl.build_library(count=3, generations=generations, seed=3), visible=False)
    return stage


def _job(stage, frames=60, count=3, strength=40.0):
    pieces = cs.read_library(stage, "/Lib")
    radius = min(float(np.linalg.norm(p["points"], axis=1).max()) for t in pieces.values() for p in t.values())
    machine = cs.gather_machine(stage, "/Rig", radius)
    job = dict(
        up_idx=1, gravity=-981.0, fps=24.0, frames=frames, substeps=8, friction=0.6, restitution=0.1,
        density=2.6, statics=machine["statics"], rotors=machine["rotors"], belts=machine["belts"],
        pieces=pieces,
        emitter=dict(lo=machine["emitter"][0], hi=machine["emitter"][1], count=count, rate=6.0,
                     velocity=np.array([0.0, -150.0, 0.0]), spin=1.0),
        breakage=dict(strength=strength, variation=0.2, cooldown=0.04, kick=10.0, max_generation=8),
        seed=1,
    )
    return job, pieces, machine


@pytest.fixture(scope="module")
def crushed():
    if not (cs._HAS_MUJOCO and rl._HAS_SCIPY):
        pytest.skip("needs mujoco and scipy")
    stage = _small_scene()
    job, pieces, machine = _job(stage)
    return stage, pieces, machine, cs.simulate(job)


def test_rock_breaks_between_the_rollers(crushed):
    _, _, _, result = crushed
    assert result["stats"]["dropped"] == 3
    assert result["stats"]["breaks"] > 0


@needs_mujoco
@needs_scipy
def test_strength_zero_breaks_nothing():
    stage = _small_scene()
    job, _, _ = _job(stage, frames=40, strength=0.0)
    assert cs.simulate(job)["stats"]["breaks"] == 0


def test_material_is_conserved_at_every_frame(crushed):
    _, pieces, _, result = crushed
    vol = np.array([pieces[int(r)][int(n)]["volume"] for r, n in zip(result["slot_rock"], result["slot_node"])])
    roots = result["slot_node"] == 0
    for f in range(result["alive"].shape[0]):
        alive = result["alive"][f]
        # Every dropped rock is somewhere: either whole, or as pieces that add up to it.
        dropped = result["alive"][: f + 1][:, roots].any(axis=0)
        expected = vol[roots][dropped].sum()
        assert vol[alive].sum() == pytest.approx(expected, rel=1e-5)


def test_bake_hides_parked_bodies_and_keeps_them_near_the_machine(crushed):
    stage, pieces, machine, result = crushed
    cs.author_result(
        stage, "/Sim/Rocks", result, pieces, 1,
        [stage.GetPrimAtPath(p) for p in machine["rotor_prims"]], machine["rotor_frames"],
    )
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/Sim/Rocks"))
    n = len(result["slot_rock"])
    assert len(inst.GetProtoIndicesAttr().Get()) == n
    for f in (0, 30, 59):
        hidden = set(inst.GetInvisibleIdsAttr().Get(1 + f))
        assert hidden == {i for i in range(n) if not result["alive"][f][i]}
        pos = np.asarray(inst.GetPositionsAttr().Get(1 + f))
        assert np.abs(pos).max() < 1e3, "a parked body's parking-lot position leaked into the bake"

    roller = stage.GetPrimAtPath("/Rig/RollerA")
    order = list(UsdGeom.Xformable(roller).GetXformOpOrderAttr().Get())
    assert order[-3:] == [
        "xformOp:translate:crusherPivot", "xformOp:orient:crusherSpin", "!invert!xformOp:translate:crusherPivot",
    ]
    # The spin is real: the roller's pose differs frame to frame.
    a = UsdGeom.Xformable(roller).ComputeLocalToWorldTransform(1)
    b = UsdGeom.Xformable(roller).ComputeLocalToWorldTransform(12)
    assert a != b


# ---------------------------------------------------------------------------
# The three chunks in a scene
# ---------------------------------------------------------------------------


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@needs_mujoco
@needs_scipy
def test_three_chunks_bake_a_crusher(qapp):
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

    library = mk("rock_library")
    library.get_param("generations").value = 2
    library.get_param("count").value = 3
    machine = mk("crusher_rig")
    solver = mk("crusher_solver")
    library.execute()
    machine.execute()
    qapp.processEvents()
    assert stage.GetPrimAtPath("/RockLibrary/Rock_00/n0").IsValid()
    assert stage.GetPrimAtPath("/CrusherRig/RollerA").IsValid()

    solver.get_param("rock_count").value = 2
    solver.get_param("frame_count").value = 36
    solver.get_param("substeps").value = 6
    cs._run(solver, None, None)
    qapp.processEvents()

    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/CrusherSim/Rocks"))
    assert inst and inst.GetPositionsAttr().GetNumTimeSamples() == 36
    assert "dropped" in solver.get_param("readout").value
    # The bake's layer must win over the rig's, or the rollers' spin is hidden under the
    # rig's own xformOpOrder.
    order = UsdGeom.Xformable(stage.GetPrimAtPath("/CrusherRig/RollerA")).GetXformOpOrderAttr().Get()
    assert "xformOp:orient:crusherSpin" in list(order)
