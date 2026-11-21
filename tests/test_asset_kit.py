"""Asset Kit: your own assets laid out for a scatter.

* a folder gives every USD file in it, one detail level per asset (Detail), each standing on
  the ground at the origin, in the scene's units and up axis, and fitted to Fit Size;
* prims in the scene come in without the place they have there, a deactivated library entry
  switched on, and the prims themselves hidden;
* with nothing given, the Fallback is laid out, at its own size.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import Scene


def _asset(path, size_cm=50.0, up="Y", offset=(0.0, 0.0, 0.0)):
    """A box asset file in centimetres: size_cm across, its pivot off its middle by offset."""
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    UsdGeom.SetStageUpAxis(stage, up)
    root = UsdGeom.Xform.Define(stage, "/asset")
    stage.SetDefaultPrim(root.GetPrim())
    cube = UsdGeom.Cube.Define(stage, "/asset/geo")
    cube.CreateSizeAttr(size_cm)
    UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(*offset))
    stage.Save()


def _range(stage, path):
    return UsdGeom.BBoxCache(0, ["default"]).ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()


def test_a_folder_gives_one_file_per_asset_standing_on_the_ground_in_scene_units(tmp_path):
    folder = tmp_path / "rocks"
    (folder / "rock").mkdir(parents=True)
    for level, size in (("low", 10.0), ("mid", 50.0), ("high", 90.0)):
        _asset(folder / "rock" / f"rock_{level}.usda", size_cm=size, offset=(30.0, 40.0, 0.0))
    _asset(folder / "pebble.usda", size_cm=20.0, up="Z")

    with Scene() as scene:
        stage = scene.get_stage()
        kit = scene.create.asset_kit(files=str(folder), primpath_location="/kit")

        assert sorted(p.GetName() for p in stage.GetPrimAtPath("/kit").GetChildren()) == ["pebble", "rock"]
        rock = _range(stage, "/kit/rock")
        assert rock.GetSize()[0] == pytest.approx(0.5), "the mid file, in metres"
        assert rock.GetMin()[1] == pytest.approx(0.0, abs=1e-6), "standing on the ground"
        assert rock.GetMidpoint()[0] == pytest.approx(0.0, abs=1e-6), "centred over the origin"
        assert _range(stage, "/kit/pebble").GetMin()[1] == pytest.approx(0.0, abs=1e-6), "turned Y up"
        assert kit.get_param("readout").value.startswith("2 assets from files")

        kit.get_param("fit").value = 0.25
        kit.get_param("detail").value = "High"
        assert max(_range(stage, "/kit/rock").GetSize()) == pytest.approx(0.25)


def test_prims_come_in_without_their_place_and_are_hidden():
    with Scene() as scene:
        stage = scene.get_stage()
        lib = UsdGeom.Cube.Define(stage, "/Library/crate")
        lib.CreateSizeAttr(1.0)
        UsdGeom.Xformable(lib).AddTranslateOp().Set(Gf.Vec3d(20.0, 5.0, 0.0))
        lib.GetPrim().SetActive(False)
        scene.create.asset_kit(prims="/Library/*", primpath_location="/kit")

        crate = _range(stage, "/kit/crate")
        assert crate.GetMidpoint()[0] == pytest.approx(0.0) and crate.GetMin()[1] == pytest.approx(0.0)
        assert stage.GetPrimAtPath("/kit/crate/place/asset").IsActive(), "a deactivated entry is switched on"
        assert UsdGeom.Imageable(stage.GetPrimAtPath("/kit/crate/place/asset")).ComputeVisibility() == "inherited"


def test_with_nothing_given_the_fallback_is_laid_out_at_its_own_size():
    with Scene() as scene:
        stage = scene.get_stage()
        builtin = UsdGeom.Cube.Define(stage, "/builtin/stone")
        builtin.CreateSizeAttr(0.2)
        kit = scene.create.asset_kit(fallback="/builtin/*", fit=2.0, primpath_location="/kit")

        assert max(_range(stage, "/kit/stone").GetSize()) == pytest.approx(0.2), "Fit Size is for your assets"
        assert UsdGeom.Imageable(stage.GetPrimAtPath("/builtin/stone")).ComputeVisibility() == "invisible"
        assert "built-in" in kit.get_param("readout").value
