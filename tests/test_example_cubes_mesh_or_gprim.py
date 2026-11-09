"""examples/scripts/cubes_mesh_or_gprim.py makes the prim types and placements it says.

`scene.create.cube` and `sphere` are the Mesh chunk's entries and make Mesh prims; a small
model asked for "a USD Cube" took them in 2 of 3 benchmark runs. The example sets each beside
its gprim counterpart, so it must keep showing what it claims.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
from pxr import Usd, UsdGeom

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "scripts" / "cubes_mesh_or_gprim.py"


@pytest.fixture(scope="module")
def stage(tmp_path_factory):
    out = tmp_path_factory.mktemp("cubes") / "cubes.usda"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", [str(EXAMPLE), str(out)])
        runpy.run_path(str(EXAMPLE), run_name="__main__")
    return Usd.Stage.Open(str(out))


@pytest.mark.parametrize("path, type_name", [
    ("/World/mesh_box", "Mesh"), ("/World/mesh_ball", "Mesh"),
    ("/World/usd_box", "Cube"), ("/World/usd_ball", "Sphere"),
])
def test_each_prim_is_the_type_the_example_says(stage, path, type_name) -> None:
    assert stage.GetPrimAtPath(path).GetTypeName() == type_name


@pytest.mark.parametrize("path", ["/World/mesh_box", "/World/usd_box", "/World/usd_ball"])
def test_the_boxes_and_the_usd_ball_rest_on_y0(stage, path) -> None:
    bound = UsdGeom.BBoxCache(0, ["default"]).ComputeWorldBound(stage.GetPrimAtPath(path))
    assert bound.ComputeAlignedRange().GetMin()[1] == pytest.approx(0.0, abs=1e-6)
