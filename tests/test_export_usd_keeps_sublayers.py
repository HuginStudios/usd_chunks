"""A baked layer export keeps the files a Sublayer chunk added as sublayers.

The Sublayer chunk keeps its paths in a private layer. Baking inlined that layer by
flattening it, which pulled the whole sublayered file into the exported layer and turned
its relative paths absolute. The export should name the file as a sublayer instead.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk


def test_baked_export_keeps_sublayer_arc(tmp_path) -> None:
    dept = Sdf.Layer.CreateNew(str(tmp_path / "dept.usda"))
    Sdf.CreatePrimInLayer(dept, "/world/thing").specifier = Sdf.SpecifierDef
    dept.Save()
    dept_path = str(tmp_path / "dept.usda").replace("\\", "/")

    with Scene() as scene:
        layer = scene.create.layer(layer_name="shot")
        target = scene.create.edit_target(layer, edit_target="shot")
        with target:
            subs = scene.create.sublayer()
            rows = subs.get_param("imports", chunk.is_inst(chunk.ListGroup))
            rows.size = 1
            rows.get_indexed_param("sublayer_path", 0, chunk.is_param_t(str)).value = dept_path
        exporter = scene.create.export_usd(subs, output_dir=str(tmp_path), keep_session_layer=False)
        rows = exporter.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
        for i in range(rows.size):
            keep = rows.get_indexed_param(rows.get_param("layer_name"), i).value == "shot"
            rows.get_indexed_param(rows.get_param("export_enabled"), i).value = keep
            rows.get_indexed_param(rows.get_param("bake"), i).value = keep
        scene.commit()

    out = Sdf.Layer.FindOrOpen(str(tmp_path / "shot.usda"))
    # Written beside the shot, so relative to it (Export USD's Relative Paths).
    assert list(out.subLayerPaths) == ["./dept.usda"], out.ExportToString()
    assert not out.GetPrimAtPath("/world"), "the sublayered file's content is not copied in"
