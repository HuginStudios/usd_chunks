"""The ChunkGroups in examples/groups build, and work as examples/scripts/use_groups.py uses them.

They are ready-made networks an agent references instead of building: rocks on a terrain
(Scatter Points, its prototype a lofted list row) and a spiral stair (Duplicate Prim). The
committed .usda files are what agents use, so the example runs against those.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene

REPO = Path(__file__).resolve().parent.parent
GROUPS = REPO / "examples" / "groups"


def _run(script: Path, *args) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", [str(script), *map(str, args)])
        runpy.run_path(str(script), run_name="__main__")


def _published(path: Path, group: str) -> set[str]:
    layer = Sdf.Layer.FindOrOpen(str(path))   # held: a spec lives only as long as its layer
    spec = layer.GetPrimAtPath(f"/chunks/{group}")
    return {a.name.split(":", 1)[1] for a in spec.attributes if a.name.startswith("inputs:")}


@pytest.mark.parametrize("name, group, params", [
    ("scatter_on_terrain", "ScatterOnTerrain", {
        "Scatter_surface_prim", "Scatter_proto_path", "Scatter_inst_path", "Scatter_density",
        "Scatter_seed", "Scatter_max_slope", "Scatter_sit_on_surface"}),
    ("spiral_stairs", "SpiralStairs", {
        "Stairs_copies", "Place_tx", "Place_ty", "Place_sx", "Place_sy", "Place_sz",
        "Stairs_copy_translate", "Stairs_copy_rotate", "Stairs_dest_path"}),
])
def test_the_build_script_makes_each_group_with_its_params(tmp_path, name, group, params) -> None:
    _run(GROUPS / "build_groups.py", tmp_path)
    assert params <= _published(tmp_path / f"{name}.usda", group)
    # The committed file has the same interface: rebuilt after any change to the script.
    assert params <= _published(GROUPS / f"{name}.usda", group)


@pytest.fixture(scope="module")
def used(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("use_groups")
    _run(REPO / "examples" / "scripts" / "use_groups.py", out)
    return out


def test_the_scatter_group_places_its_density_of_rocks(used) -> None:
    stage = Usd.Stage.Open(str(used / "groups.usda"))
    rocks = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/rocks"))
    assert len(rocks.GetPositionsAttr().Get()) == 200   # 2 per m² of the flat 10 x 10 m
    proto = stage.GetPrimAtPath(rocks.GetPrototypesRel().GetTargets()[0])
    assert proto.GetTypeName() == "Sphere", "the lofted prototype row was not used"


def test_the_stairs_group_places_its_steps(used) -> None:
    stage = Usd.Stage.Open(str(used / "groups.usda"))
    steps = [p for p in stage.GetPrimAtPath("/World/stairs").GetChildren() if p.IsActive()]
    assert len(steps) == 12
    assert all(p.GetTypeName() == "Cube" for p in steps)
    centre = UsdGeom.Xformable(steps[3]).ComputeLocalToWorldTransform(0).ExtractTranslation()
    assert tuple(centre) == pytest.approx((0.0, 0.7, 2.0), abs=1e-5)   # 90° on, 3 steps up


def test_group_params_override_on_the_saved_network(used) -> None:
    with Scene.open(str(used / "groups_network.usda")) as scene:
        rocks = scene.get_chunk("Rocks")
        stairs = scene.get_chunk("Stairs")
        before = list(UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/World/rocks")).GetPositionsAttr().Get())
        scene.set(rocks, Scatter_seed=3)
        scene.set(stairs, Stairs_copies=5)
        stage = scene.get_stage()
        after = list(UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/rocks")).GetPositionsAttr().Get())
        assert len(after) == 200 and after != before
        assert len([p for p in stage.GetPrimAtPath("/World/stairs").GetChildren() if p.IsActive()]) == 5
