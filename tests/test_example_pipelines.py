"""The pipeline examples in examples/scripts run as written and build what they say.

One example per job the README lists: CAD to render, a product configurator, URDF for Isaac
Sim, a batch over an asset library, a scattered forest, a breaking block and a fluid sim.
Each is run from another directory into a folder of its own, the way a user runs it, and the
result is checked as USD. None of them renders here: the usd-core wheel has no usdrecord, so
Render logs that and the rest still writes. An example whose optional library is missing
(OpenCASCADE, MuJoCo, manifold3d, Taichi, fast-simplification) is skipped, and so is one
whose downloaded assets (example_assets.py) aren't there: tests never download.
"""
from __future__ import annotations

import importlib
import importlib.util
import runpy
import sys
from pathlib import Path

import numpy as np
import pytest
from pxr import Gf, Sdr, Usd, UsdGeom, UsdPhysics, UsdShade

from usd_chunks import Scene

SCRIPTS = Path(__file__).resolve().parent.parent / "examples" / "scripts"
# Loaded from its file, not put on sys.path: examples/scripts has modules (box, biome) that
# should not shadow anything else in this process.
_spec = importlib.util.spec_from_file_location("example_assets", SCRIPTS / "example_assets.py")
assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assets)

cad = importlib.import_module("chunks.modelling.cad_import")
reduce = importlib.import_module("chunks.modelling.poly_reduce")
particles = importlib.import_module("chunks.dynamics.particle_solver")

# CAD and the configurator shade with MaterialX Standard Surface, whose definition the
# usd-core wheel does not ship: the shader chunk has no entry for it there.
_HAS_MTLX = Sdr.Registry().GetShaderNodeByIdentifier("ND_standard_surface_surfaceshader") is not None
needs_ocp = pytest.mark.skipif(not (cad._HAS_OCP and _HAS_MTLX),
                               reason="needs cadquery-ocp and USD's MaterialX definitions")


def _downloaded(*models: str, so101: bool = False, textures: tuple[str, ...] = (), hdri: bool = True):
    """Skip unless example_assets.py has already fetched these: a test never downloads."""
    files = [assets.ROOT / "polyhaven" / m / f"{m}_1k.usdc" for m in models]
    files += [assets.ROOT / "polyhaven" / t / f"{t}_diff_2k.jpg" for t in textures]
    if so101:
        files.append(assets.ROOT / "so101" / "so101_new_calib.urdf")
    if hdri:
        files.append(assets.ROOT / "polyhaven" / "hdri" / f"{assets.HDRIS[0]}_2k.exr")
    missing = [f.name for f in files if not f.exists()]
    return pytest.mark.skipif(bool(missing), reason=f"run examples/scripts/example_assets.py first: no {missing}")


def _run(name: str, tmp_path_factory) -> Path:
    """Run examples/scripts/<name>.py into a fresh folder, from another working directory."""
    out = tmp_path_factory.mktemp(name)
    elsewhere = tmp_path_factory.mktemp("cwd")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(elsewhere)
        mp.syspath_prepend(str(SCRIPTS))  # the scripts import example_assets from beside them
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


needs_so101 = _downloaded(so101=True, textures=("concrete_floor_02",))


@needs_so101
def test_urdf_robot_arrives_as_an_articulated_physics_rig(urdf) -> None:
    stage = Usd.Stage.Open(str(urdf / "so101_scene.usda"))
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
    assert stage.GetDefaultPrim().GetPath() == "/World"
    assert stage.GetPrimAtPath("/World/so101/base_link").HasAPI(UsdPhysics.ArticulationRootAPI)
    revolute = [p for p in stage.Traverse() if p.IsA(UsdPhysics.RevoluteJoint)]
    assert len(revolute) == 6, "the SO-101 has six moving joints, gripper included"
    assert stage.GetPrimAtPath("/World/physicsScene").IsA(UsdPhysics.Scene)
    assert stage.GetPrimAtPath("/World/anchor").IsA(UsdPhysics.FixedJoint)
    assert stage.GetPrimAtPath("/World/anchor").GetRelationship("physics:body1").GetTargets() == [
        "/World/so101/base_link"]
    assert stage.GetPrimAtPath("/World/floor").HasAPI(UsdPhysics.CollisionAPI)
    assert _bound_material(stage, "/World/floor") == "/World/looks/concrete"
    block = stage.GetPrimAtPath("/World/block")
    for api in (UsdPhysics.RigidBodyAPI, UsdPhysics.CollisionAPI, UsdPhysics.MeshCollisionAPI, UsdPhysics.MassAPI):
        assert block.HasAPI(api), api
    assert UsdPhysics.MeshCollisionAPI(block).GetApproximationAttr().Get() == "convexHull"
    assert UsdPhysics.MassAPI(block).GetMassAttr().Get() == pytest.approx(0.02)
    assert _bound(stage, "/World/block").GetMin()[2] == pytest.approx(0.0, abs=1e-6), "the block sits on the floor"


@needs_so101
def test_urdf_drives_hold_the_pose_they_target(urdf) -> None:
    stage = Usd.Stage.Open(str(urdf / "so101_scene.usda"))
    wrist = stage.GetPrimAtPath("/World/so101/wrist_link/joint_wrist_flex")
    assert wrist.GetAttribute("drive:angular:physics:targetPosition").Get() == pytest.approx(65.0)
    assert wrist.GetAttribute("drive:angular:physics:stiffness").Get() == pytest.approx(40.0)
    jaw = _bound(stage, "/World/so101/moving_jaw_so101_v1_link")
    assert jaw.GetMin()[2] > 0.0, "the posed arm reaches down to the block without going through the floor"
    _reopens_as_chunks(urdf / "urdf_network.usda", "Pose", "PhysicsScene", "Anchor", "Colliders", "Render")


# ---------------------------------------------------------------------------
# Scattered forest
# ---------------------------------------------------------------------------


@_downloaded("fir_sapling", "dead_tree_trunk_02", "tree_stump_01", "boulder_01", "shrub_04", "nettle_plant",
             "grass_medium_01", textures=("brown_mud_leaves_01",))
def test_forest_scatters_every_layer_from_real_assets(tmp_path_factory) -> None:
    out = _run("forest", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "forest.usda"))
    for layer in ("trees", "deadwood", "undergrowth", "grass"):
        ids = UsdGeom.PointInstancer(stage.GetPrimAtPath(f"/forest/{layer}")).GetProtoIndicesAttr().Get()
        assert ids, f"nothing was scattered in {layer}"
    # One variation per Reference, at the origin, without Poly Haven's MaterialX.
    fir = stage.GetPrimAtPath("/protos/fir_b")
    assert [c.GetName() for c in fir.GetChildren()] == ["Cylinder_069"], "only fir_sapling_b's mesh"
    assert Gf.IsClose(UsdGeom.Xformable(fir).GetLocalTransformation(), Gf.Matrix4d(1), 1e-6), "back at the origin"
    assert not any(p.IsA(UsdShade.Material) for p in Usd.PrimRange(stage.GetPrimAtPath("/protos")))
    # The mesh is bark, and its twig face subset keeps the twigs: the binding is weaker than the subset's.
    assert _bound_material(stage, "/protos/fir_b/Cylinder_069") == "/looks/fir_sapling_branches"
    assert _bound_material(stage, "/protos/fir_b/Cylinder_069/fir_sapling_twigs") == "/looks/fir_sapling_twigs"
    assert _bound_material(stage, "/ground") == "/looks/forest_floor"
    _reopens_as_chunks(out / "forest_network.usda", "FirA", "ToOrigin", "Bind_fir_sapling_twigs", "Trees", "Grass",
                       "Render")


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
# The valley biome, and houses with water
# ---------------------------------------------------------------------------


@_downloaded("fir_sapling", "shrub_04", "grass_medium_01", "boulder_01",
             textures=("aerial_grass_rock", "coast_sand_01", "rock_boulder_dry"))
def test_valley_scatters_by_height_and_keeps_away_from_the_houses(tmp_path_factory) -> None:
    out = _run("valley", tmp_path_factory)
    stage = Usd.Stage.Open(str(out / "valley.usda"))
    ground = stage.GetPrimAtPath("/World/terrain/ground")
    points = np.asarray(UsdGeom.Mesh(ground).GetPointsAttr().Get())

    def instances(layer):
        return np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath(f"/World/biome/{layer}")).GetPositionsAttr().Get())

    houses, trees, grass = instances("houses"), instances("trees"), instances("grass")
    assert len(houses) and len(trees) and len(grass)
    assert houses[:, 2].min() >= 2.9 and houses[:, 2].max() <= 6.1, "houses only on the valley floor"
    assert trees[:, 2].max() <= 36.0, "no trees above the tree line (32 m, soft over 4)"
    assert trees[:, 2].min() > 0.8, "no trees in the lake"
    gap = np.linalg.norm(trees[:, None, :2] - houses[None, :, :2], axis=-1).min()
    assert gap >= 7.0, "the trees keep away from the houses"
    # The bands' faces are subsets with their own materials; the ground keeps its own.
    for band, look in (("sand", "sand"), ("rock", "rock")):
        subset = UsdGeom.Subset(stage.GetPrimAtPath(f"/World/terrain/ground/{band}"))
        assert len(subset.GetIndicesAttr().Get()), band
        assert _bound_material(stage, f"/World/terrain/ground/{band}") == f"/World/looks/{look}"
    assert _bound_material(stage, "/World/terrain/ground") == "/World/looks/grass"
    assert points[:, 2].max() > 60.0 and (out / "terrain.usdc").stat().st_size < 1_000_000
    _reopens_as_chunks(out / "valley_network.usda", "TreeLine", "ValleyFloor", "Shore", "Houses", "Trees")


@pytest.mark.skipif(not particles._HAS_TAICHI, reason="needs taichi")
@_downloaded(textures=("grass_path_2",))
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
