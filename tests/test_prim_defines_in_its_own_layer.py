"""A Prim chunk defines its prim in its own layer, even when another layer already does.

DefinePrim writes nothing for a prim already defined with the same type. Two department
layers (geo, proxy) each defining /ball as an Xform: the second Prim chunk's layer got no
spec, so the published proxy file had `def "ball"` with no type, or nothing at all.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk


def test_both_layers_publish_the_def(tmp_path) -> None:
    with Scene() as scene:
        branches = []
        for name in ("geo", "proxy"):
            layer = scene.create.layer(layer_name=name)
            with scene.create.edit_target(layer, edit_target=name):
                branches.append(scene.create.prim(primpath_location="/ball", prim_type="Xform"))
        merged = scene.create.merge(*branches)
        exporter = scene.create.export_usd(merged, output_dir=str(tmp_path), keep_session_layer=False)
        rows = exporter.get_param("export_layers", chunk.is_inst(chunk.ListGroup))
        for i in range(rows.size):
            keep = rows.get_indexed_param(rows.get_param("layer_name"), i).value in ("geo", "proxy")
            rows.get_indexed_param(rows.get_param("export_enabled"), i).value = keep
            rows.get_indexed_param(rows.get_param("bake"), i).value = keep
        scene.commit()

    for name in ("geo", "proxy"):
        layer = Sdf.Layer.FindOrOpen(str(tmp_path / f"{name}.usda"))  # held: a spec dies with it
        spec = layer.GetPrimAtPath("/ball")
        assert spec is not None, f"{name}.usda has no /ball"
        assert (spec.specifier, spec.typeName) == (Sdf.SpecifierDef, "Xform"), name
