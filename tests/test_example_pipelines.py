"""The pipeline examples in examples/scripts run as written and build what they say.

One example per job the README lists: CAD to render, a product configurator, URDF for Isaac
Sim, a batch over an asset library, a scattered biome, a breaking block and a fluid sim. Each
is run from another directory into a folder of its own, the way a user runs it, and the
result is checked as USD. None of them renders here: the usd-core wheel has no usdrecord, so
Render logs that and the rest still writes. An example whose optional library is missing
(OpenCASCADE, MuJoCo, manifold3d, Taichi, fast-simplification) is skipped.
"""
from __future__ import annotations

import importlib
import runpy
import sys
from pathlib import Path

import pytest
from pxr import Sdr, Usd, UsdGeom, UsdPhysics, UsdShade

from usd_chunks import Scene

SCRIPTS = Path(__file__).resolve().parent.parent / "examples" / "scripts"

cad = importlib.import_module("chunks.modelling.cad_import")
reduce = importlib.import_module("chunks.modelling.poly_reduce")
fracture = importlib.import_module("chunks.dynamics.fracture")
rbd = importlib.import_module("chunks.dynamics.rbd_solver")
particles = importlib.import_module("chunks.dynamics.particle_solver")

# CAD and the configurator shade with MaterialX Standard Surface, whose definition the
# usd-core wheel does not ship: the shader chunk has no entry for it there.
_HAS_MTLX = Sdr.Registry().GetShaderNodeByIdentifier("ND_standard_surface_surfaceshader") is not None
needs_ocp = pytest.mark.skipif(not (cad._HAS_OCP and _HAS_MTLX),
                               reason="needs cadquery-ocp and USD's MaterialX definitions")


def _run(name: str, tmp_path_factory) -> Path:
    """Run examples/scripts/<name>.py into a fresh folder, from another working directory."""
    out = tmp_path_factory.mktemp(name)
    elsewhere = tmp_path_factory.mktemp("cwd")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(elsewhere)
        mp.syspath_prepend(str(SCRIPTS))  # biome_render imports biome from beside it
        mp.setattr(sys, "argv", [str(SCRIPTS / f"{name}.py"), str(out)])
        runpy.run_path(str(SCRIPTS / f"{name}.py"), run_name="__main__")
    assert not any(elsewhere.iterdir()), f"{name} wrote into the working directory"
    return out


def _triangles(stage: Usd.Stage, root: str = "/") -> int:
    total = 0
    for prim in Usd.PrimRange(stage.GetPrimAtPath(root)):
        if prim.IsA(UsdGeom.Mesh):
            total += sum(c - 2 for c in UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get() or [])
    return total


def _bound(stage: Usd.Stage, path: str, time=Usd.TimeCode.Default()):
    cache = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    return cache.ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()


def _bound_material(stage: Usd.Stage, path: str) -> str:
    material, _ = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()
    return str(material.GetPath()) if material else ""


def _reopens_as_chunks(path: Path, *names: str) -> None:
    """The saved network opens again with the named nodes in it."""
    with Scene.open(str(path), lazy=True) as scene:
        found = {c.name for c in scene.chunks_in_order()}
    assert set(names) <= found, f"missing {set(names) - found} in {sorted(found)}"


# ---------------------------------------------------------------------------
# URDF to Isaac Sim
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def urdf(tmp_path_factory) -> Path:
    return _run("urdf_to_isaac", tmp_path_factory)


def test_urdf_robot_arrives_as_an_articulated_physics_rig(urdf) -> None:
    stage = Usd.Stage.Open(str(urdf / "desk_arm_scene.usda"))
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
    assert stage.GetDefaultPrim().GetPath() == "/World"
    base = stage.GetPrimAtPath("/World/desk_arm/base")
    assert base.HasAPI(UsdPhysics.ArticulationRootAPI)
    bodies = [p for p in Usd.PrimRange(stage.GetPrimAtPath("/World/desk_arm")) if p.HasAPI(UsdPhysics.RigidBodyAPI)]
    assert len(bodies) == 5
    revolute = [p for p in stage.Traverse() if p.IsA(UsdPhysics.RevoluteJoint)]
    assert len(revolute) == 3
    assert stage.GetPrimAtPath("/World/physicsScene").IsA(UsdPhysics.Scene)
    assert stage.GetPrimAtPath("/World/anchor").IsA(UsdPhysics.FixedJoint)


def test_urdf_drives_hold_the_pose_they_target(urdf) -> None:
    stage = Usd.Stage.Open(str(urdf / "desk_arm_scene.usda"))
    elbow = stage.GetPrimAtPath("/World/desk_arm/forearm/joint_elbow")
    assert elbow.GetAttribute("drive:angular:physics:targetPosition").Get() == pytest.approx(75.0)
    assert elbow.GetAttribute("drive:angular:physics:stiffness").Get() == pytest.approx(400.0)
    tool = _bound(stage, "/World/desk_arm/tool")
    assert tool.GetMin()[2] > 0.2, "the posed arm reaches up, off the floor"
    _reopens_as_chunks(urdf / "urdf_network.usda", "Pose", "Render")


# ---------------------------------------------------------------------------
# Scattered biome
# ---------------------------------------------------------------------------


def test_biome_render_lights_and_frames_the_forest(tmp_path_factory) -> None:
    out = _run("biome_render", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "biome_render.usda"))
    for layer in ("trees", "bushes", "ferns", "grass"):
        ids = UsdGeom.PointInstancer(stage.GetPrimAtPath(f"/biome/{layer}")).GetProtoIndicesAttr().Get()
        assert ids, f"no {layer} were scattered"
    assert stage.GetPrimAtPath("/cam").IsA(UsdGeom.Camera)
    assert _bound_material(stage, "/ground") == "/looks/soil"
    _reopens_as_chunks(out / "biome_render_network.usda", "Trees", "Grass", "Render")


# ---------------------------------------------------------------------------
# CAD to render, and the configurator built on it
# ---------------------------------------------------------------------------


@needs_ocp
def test_cad_parts_are_upright_instanced_and_shaded(tmp_path_factory) -> None:
    out = _run("cad_to_render", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "cad_scene.usda"))
    motor = _bound(stage, "/World/Motor")
    assert motor.GetMin()[1] == pytest.approx(0.0, abs=0.005), "stands on the ground (bolt tips 4 mm below)"
    assert motor.GetMax()[1] > 0.1, "turned Z-up to Y-up: the motor is taller than the plate"
    bolts = [stage.GetPrimAtPath(f"/World/Motor/Motor/bolt_{i}") for i in range(4)]
    shared = {tuple(b.GetMetadata("references").GetAddedOrExplicitItems()) for b in bolts}
    assert len(shared) == 1, "the bolt is one part, tessellated once and placed four times"
    assert _bound_material(stage, "/World/Motor/Prototypes/Body/mesh") == "/World/Looks/painted_red"
    assert _bound_material(stage, "/World/Motor/Prototypes/Bolt/mesh") == "/World/Looks/brushed_steel"
    _reopens_as_chunks(out / "cad_network.usda", "CAD", "Render")


@needs_ocp
def test_configurator_options_are_variants(tmp_path_factory) -> None:
    out = _run("configurator", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "configurator.usda"))
    motor = stage.GetPrimAtPath("/World/Motor")
    sets = motor.GetVariantSets()
    assert set(sets.GetNames()) == {"paint", "base"}
    body = "/World/Motor/Prototypes/Body/mesh"
    for colour in ("red", "blue", "yellow"):
        sets.GetVariantSet("paint").SetVariantSelection(colour)
        assert _bound_material(stage, body) == f"/World/Looks/paint_{colour}"
    plate = UsdGeom.Imageable(stage.GetPrimAtPath("/World/Motor/Motor/plate"))
    assert plate.ComputeVisibility() == UsdGeom.Tokens.inherited
    sets.GetVariantSet("base").SetVariantSelection("bare")
    assert plate.ComputeVisibility() == UsdGeom.Tokens.invisible
    _reopens_as_chunks(out / "configurator_network.usda", "Paint", "Base")


# ---------------------------------------------------------------------------
# Batch over a library
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not reduce._HAS_SIMPLIFY, reason="needs fast-simplification")
def test_batch_makes_a_lighter_proxy_of_every_asset(tmp_path_factory) -> None:
    out = _run("batch_library", tmp_path_factory)
    for name in ("sphere", "torus"):
        before = _triangles(Usd.Stage.Open(str(out / "library" / f"{name}.usda")))
        after = _triangles(Usd.Stage.Open(str(out / "proxies" / f"{name}_proxy.usda")))
        assert 0 < after <= before * 0.15, f"{name}: {before} -> {after}"
    assert (out / "proxies" / "cone_proxy.usda").exists()


# ---------------------------------------------------------------------------
# Boom, and houses with water
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (fracture._HAS_MANIFOLD and rbd._HAS_MUJOCO), reason="needs manifold3d and mujoco")
def test_boom_block_falls_and_breaks(tmp_path_factory) -> None:
    out = _run("boom", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "boom.usda"))
    pieces = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/pieces"))

    def visible(frame):
        hidden = set(pieces.GetInvisibleIdsAttr().Get(frame) or [])
        return [i for i in range(len(pieces.GetPositionsAttr().Get(frame))) if i not in hidden]

    assert len(visible(1)) == 1, "it starts whole"
    assert len(visible(60)) > 5, "it has broken by the end"
    positions = pieces.GetPositionsAttr().Get(60)
    heights = sorted(positions[i][1] for i in visible(60))
    assert heights[len(heights) // 2] < 1.0, "most pieces came down (a few may still be flying)"


@pytest.mark.skipif(not particles._HAS_TAICHI, reason="needs taichi")
def test_houses_stand_and_the_water_lands_on_the_ground(tmp_path_factory) -> None:
    out = _run("houses_and_water", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "houses_and_water.usda"))
    for i in (1, 2, 3):
        house = _bound(stage, f"/World/houses/cottage{i}")
        assert house.GetMin()[1] == pytest.approx(0.0, abs=0.1)
    assert _bound(stage, "/World/houses/cottage2").GetMax()[1] > _bound(stage, "/World/houses/cottage1").GetMax()[1]
    points = UsdGeom.Points(stage.GetPrimAtPath("/World/fx/sim/Particles")).GetPointsAttr().Get(48)
    assert len(points) > 1000
    assert min(p[1] for p in points) > -0.05, "no water sank through the ground"
