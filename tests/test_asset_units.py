"""A file brought in with other units than the stage warns, once per pair of units.

USD does not rescale a reference made in metres onto a stage in centimetres. The shapes
still look right next to each other, but whatever reads the stage's units -- Particle
Solver gravity, Finish Frame haze distance, a camera lens -- is 100x off, silently.
"""
from __future__ import annotations

import logging

from pxr import Usd, UsdGeom

from usd_chunks import Scene


def _asset(path, meters_per_unit):
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/barrel").GetPrim())
    UsdGeom.Cube.Define(stage, "/barrel/body")
    stage.Save()
    return str(path)


def _unit_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "metersPerUnit" in r.getMessage()]


def test_a_metre_asset_on_a_centimetre_stage_warns_once(tmp_path, caplog):
    barrel = _asset(tmp_path / "barrel.usda", 1.0)
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        UsdGeom.SetStageMetersPerUnit(scene.get_stage(), 0.01)
        ref = scene.create.reference(file_path=barrel, primpath_location="/set/barrel", name="Barrel")
        scene.set(ref, primpath_location="/set/barrel2")
        # A second file in the same units is not listed again.
        scene.create.reference(file_path=_asset(tmp_path / "crate.usda", 1.0), primpath_location="/set/crate")
    warnings = _unit_warnings(caplog)
    assert len(warnings) == 1
    assert "Barrel brings in barrel.usda" in warnings[0].getMessage()
    assert "100x" in warnings[0].getMessage()


def test_matching_units_say_nothing(tmp_path, caplog):
    barrel = _asset(tmp_path / "barrel_m.usda", 1.0)
    with Scene(host_name="pytest") as scene, caplog.at_level(logging.WARNING):
        UsdGeom.SetStageMetersPerUnit(scene.get_stage(), 1.0)
        scene.create.reference(file_path=barrel, primpath_location="/set/barrel")
    assert not _unit_warnings(caplog)
