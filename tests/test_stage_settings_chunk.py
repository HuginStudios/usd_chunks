"""The Stage Settings chunk writes units, up axis and frame range onto the edit layer.

No chunk authored stage metadata, and USD reads it only from the root layer -- for a shot,
the container file. The test_shot3 shots came out in centimetres with no frame range, and
the build script stamped the settings on afterwards, outside the chunks.
"""

from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk

SETTINGS = dict(meters_per_unit=1.0, up_axis="Z", start_frame=1001, end_frame=1100, fps=25.0)


def test_without_an_edit_target_it_applies_to_the_open_stage() -> None:
    with Scene() as scene:
        scene.create.stage_settings(**SETTINGS)
        stage = scene.get_stage()
        assert UsdGeom.GetStageMetersPerUnit(stage) == 1.0
        assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
        assert (stage.GetStartTimeCode(), stage.GetEndTimeCode()) == (1001, 1100)
        assert stage.GetTimeCodesPerSecond() == 25.0


def test_behind_an_edit_target_its_export_carries_them(tmp_path) -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="sh010")
        with scene.create.edit_target(layer, edit_target="sh010"):
            settings = scene.create.stage_settings(**SETTINGS)
        exporter = scene.create.export_usd(settings, output_dir=str(tmp_path), keep_session_layer=False)
        rows = exporter.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
        for i in range(rows.size):
            keep = rows.get_indexed_param(rows.get_param("layer_name"), i).value == "sh010"
            rows.get_indexed_param(rows.get_param("export_enabled"), i).value = keep
            rows.get_indexed_param(rows.get_param("bake"), i).value = keep
        scene.commit()

    opened = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(tmp_path / "sh010.usda")))
    assert UsdGeom.GetStageMetersPerUnit(opened) == 1.0
    assert UsdGeom.GetStageUpAxis(opened) == UsdGeom.Tokens.z
    assert (opened.GetStartTimeCode(), opened.GetEndTimeCode()) == (1001, 1100)
    assert opened.GetFramesPerSecond() == 25.0


def test_removing_it_puts_the_stage_back() -> None:
    """It writes on the session layer, outside its own layer: deleting the chunk used to
    leave its settings behind."""
    with Scene() as scene:
        stage = scene.get_stage()
        session = stage.GetSessionLayer()
        session.pseudoRoot.SetInfo(UsdGeom.Tokens.upAxis, UsdGeom.Tokens.y)  # set before: kept
        settings = scene.create.stage_settings(**SETTINGS)
        assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z

        scene.remove_chunk(settings.get_hash())

        assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
        assert not session.pseudoRoot.HasInfo(UsdGeom.Tokens.metersPerUnit)
        assert not session.HasStartTimeCode() and not session.HasEndTimeCode()
        assert not session.HasTimeCodesPerSecond() and not session.HasFramesPerSecond()
