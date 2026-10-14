"""Every export carries the stage's up-axis and units, as a save already did.

A flatten carried them only when the scene authored them, so a new scene -- running on the
fallback -- was exported with no opinion at all and opened elsewhere as whatever that
machine's default was. Export USD's per-layer files never carried them, so a layer opened
on its own lost even an authored Z-up and lay on its side.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene


def _z_up_in_centimetres(scene):
    """Authored on the session layer, as a host's own up-axis setting would be."""
    stage = scene.get_stage()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    scene.create.cube(prim_path="/world/box", name="box")


def _metadata(path):
    """What the file itself authors -- a fallback answer would pass without an opinion."""
    stage = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(path)))
    assert stage.HasAuthoredMetadata(UsdGeom.Tokens.upAxis), "no up-axis written"
    assert stage.HasAuthoredMetadata(UsdGeom.Tokens.metersPerUnit), "no units written"
    return UsdGeom.GetStageUpAxis(stage), UsdGeom.GetStageMetersPerUnit(stage)


def test_scene_export(tmp_path) -> None:
    with Scene() as scene:
        _z_up_in_centimetres(scene)
        scene.export(str(tmp_path / "out.usda"))
    assert _metadata(tmp_path / "out.usda") == ("Z", 0.01)


@pytest.mark.parametrize("mode", ["Baked", "Procedural", "Based on Settings"])
def test_export_usd_flattened(tmp_path, mode) -> None:
    with Scene() as scene:
        _z_up_in_centimetres(scene)
        scene.create.export_usd(flatten=True, export_mode=mode, output_file=str(tmp_path / "out.usda"))
        scene.commit()
    assert _metadata(tmp_path / "out.usda") == ("Z", 0.01)


@pytest.mark.parametrize("bake", [True, False])
def test_export_usd_every_layer_file(tmp_path, bake) -> None:
    """Any one of them may be the file somebody opens on its own."""
    with Scene() as scene:
        _z_up_in_centimetres(scene)
        layer = scene.create.layer(layer_name="model")
        with scene.create.edit_target(layer, edit_target="model"):
            scene.create.cube(prim_path="/world/crate", name="crate")
        scene.create.export_usd(output_dir=str(tmp_path), bake_all=bake)
        scene.commit()
    files = sorted(tmp_path.glob("*.usda"))
    assert {p.name for p in files} >= {"model.usda", "SessionLayer.usda"}
    for path in files:
        assert _metadata(path) == ("Z", 0.01), path.name


def test_an_unauthored_up_axis_is_written_out(tmp_path) -> None:
    """The fallback the scene was made under is recorded, not left to the next machine's."""
    with Scene() as scene:
        scene.create.cube(prim_path="/world/box", name="box")
        stage = scene.get_stage()
        expected = (UsdGeom.GetStageUpAxis(stage), UsdGeom.GetStageMetersPerUnit(stage))
        scene.export(str(tmp_path / "out.usda"))
    assert _metadata(tmp_path / "out.usda") == expected
