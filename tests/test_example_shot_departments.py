"""examples/scripts/shot_departments.py runs as written and makes the shot it says.

It is the example an agent copies for a shot in department layers, so it must keep
working: run from another directory, it writes shot.usda (the session layer, by Session
File Name) as plain USD sublayering the departments over the set, each department file
holds only its own edits, and the input set is not copied into the shot.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "scripts" / "shot_departments.py"


@pytest.fixture(scope="module")
def shot(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("example")
    elsewhere = tmp_path_factory.mktemp("cwd")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(elsewhere)  # paths come from the network's folder, not the working directory
        mp.setattr(sys, "argv", [str(EXAMPLE), str(out)])
        runpy.run_path(str(EXAMPLE), run_name="__main__")
    assert not any(elsewhere.iterdir()), "the example wrote into the working directory"
    return out / "shot"


def test_the_shot_file_sublayers_the_departments_over_the_set(shot) -> None:
    layer = Sdf.Layer.FindOrOpen(str(shot / "shot.usda"))
    assert list(layer.subLayerPaths) == ["camera.usda", "lighting.usda", "layout.usda", "../inputs/set.usda"]
    assert not layer.GetPrimAtPath("/chunks"), "the shot carries the network's bookkeeping"
    assert not (shot / "set.usda").exists(), "the input set was copied into the shot"
    assert not (shot / "SessionLayer.usda").exists()


def test_the_composed_shot_is_what_the_departments_made(shot) -> None:
    stage = Usd.Stage.Open(str(shot / "shot.usda"))
    box = UsdGeom.Xformable(stage.GetPrimAtPath("/World/box")).ComputeLocalToWorldTransform(0)
    assert tuple(box.ExtractTranslation()) == pytest.approx((3.0, 0.5, 0.0))
    assert stage.GetPrimAtPath("/World/key").GetAttribute("inputs:intensity").Get() == pytest.approx(3.0)
    assert stage.GetPrimAtPath("/World/cam").IsA(UsdGeom.Camera)
    assert UsdGeom.GetStageMetersPerUnit(stage) == pytest.approx(1.0)
    assert (stage.GetStartTimeCode(), stage.GetEndTimeCode()) == (1.0, 48.0)


@pytest.mark.parametrize("dept, made", [("layout", "/World/box"), ("lighting", "/World/key"), ("camera", "/World/cam")])
def test_each_department_holds_only_its_own_edits(shot, dept, made) -> None:
    layer = Sdf.Layer.FindOrOpen(str(shot / f"{dept}.usda"))
    leaves = [p for p in ("/World/box", "/World/key", "/World/cam") if layer.GetPrimAtPath(p)]
    assert leaves == [made]
    assert not layer.GetPrimAtPath("/chunks")


def test_the_network_reopens_as_chunks(shot) -> None:
    with Scene.open(str(shot / "shot_network.usda")) as scene:
        names = {c.name for c in scene.get_chunks()}
    assert {"Set", "MoveBox", "Key", "ShotCam", "Export"} <= names


def test_the_session_layer_keeps_its_name_by_default(tmp_path) -> None:
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        last = scene.create.stage_settings(meters_per_unit=1.0)
        scene.set_output(scene.create.export_usd(last, output_dir=".", keep_session_layer=True))
        scene.save()
        scene.commit()
    assert (tmp_path / "SessionLayer.usda").exists()
