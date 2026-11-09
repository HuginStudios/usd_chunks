"""A new script Scene is in metres.

USD's fallback for a stage that says nothing is centimetres, and every export stamped it on.
A script's numbers read as metres, so every benchmark agent began with a Stage Settings
chunk to say so, and one first wrote centimetres into its department files.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene


def test_a_new_scene_and_its_export_are_in_metres(tmp_path) -> None:
    with Scene() as scene:
        scene.set_output(scene.create.usd_cube(prim_path="/World/box"))
        assert UsdGeom.GetStageMetersPerUnit(scene.get_stage()) == 1.0
        scene.export(str(tmp_path / "box.usda"))
    assert Sdf.Layer.FindOrOpen(str(tmp_path / "box.usda")).pseudoRoot.GetInfo("metersPerUnit") == 1.0


def test_scene_new_with_a_path_is_in_metres(tmp_path) -> None:
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        assert UsdGeom.GetStageMetersPerUnit(scene.get_stage()) == 1.0


def test_stage_settings_still_sets_other_units() -> None:
    with Scene() as scene:
        scene.set_output(scene.create.stage_settings(meters_per_unit=0.01))
        assert UsdGeom.GetStageMetersPerUnit(scene.get_stage()) == pytest.approx(0.01)


def test_a_stage_passed_in_or_opened_keeps_its_own(tmp_path) -> None:
    stage = Usd.Stage.CreateInMemory()
    with Scene(stage) as scene:
        assert not scene.get_stage().GetRootLayer().pseudoRoot.HasInfo("metersPerUnit")
    path = tmp_path / "cm.usda"
    with Scene() as scene:
        scene.set_output(scene.create.stage_settings(meters_per_unit=0.01))
        scene.save(str(path))
    with Scene.open(str(path)) as scene:
        assert UsdGeom.GetStageMetersPerUnit(scene.get_stage()) == pytest.approx(0.01)
