"""Export USD writes each layer to the File Name on its row, or its own name when blank.

The file was always named after the layer, so a versioned publish needed the version in
the layer's name (materials_v002), with no way to name the file separately.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk


def _rows(exporter):
    rows = exporter.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
    by_name = {rows.get_indexed_param(rows.get_param("layer_name"), i).value: i for i in range(rows.size)}
    return rows, by_name


def test_rows_write_to_their_file_names(tmp_path) -> None:
    with Scene() as scene:
        last = None
        for name in ("look", "fx", "light"):
            layer = scene.create.layer(*([last] if last else []), layer_name=name)
            last = scene.create.edit_target(layer, edit_target=name)
        exporter = scene.create.export_usd(last, output_dir=str(tmp_path), keep_session_layer=False)
        rows, by_name = _rows(exporter)
        for name, i in by_name.items():  # only the three layers made here
            rows.get_indexed_param(rows.get_param("export_enabled"), i).value = name in {"look", "fx", "light"}
        rows.get_indexed_param(rows.get_param("file_name"), by_name["look"]).value = "materials_v002"
        rows.get_indexed_param(rows.get_param("file_name"), by_name["fx"]).value = "fx_cache.usdc"
        scene.commit()

    written = {p.name for p in tmp_path.iterdir()}
    assert {"materials_v002.usda", "fx_cache.usdc", "light.usda"} <= written
    assert not {"look.usda", "fx.usda"} & written
    assert Sdf.Layer.FindOrOpen(str(tmp_path / "fx_cache.usdc")).GetFileFormat().formatId == "usdc"


def test_a_file_name_survives_a_new_layer_appearing(tmp_path) -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="look")
        target = scene.create.edit_target(layer, edit_target="look")
        exporter = scene.create.export_usd(target, output_dir=str(tmp_path))
        rows, by_name = _rows(exporter)
        rows.get_indexed_param(rows.get_param("file_name"), by_name["look"]).value = "materials_v003"

        scene.create.layer(target, layer_name="extra")  # rebuilds the rows
        rows, by_name = _rows(exporter)
        assert rows.get_indexed_param(rows.get_param("file_name"), by_name["look"]).value == "materials_v003"
