"""Two Payload chunks on one prim leave that prim defined, not an `over`.

Each chunk skipped defining the prim when it was already valid, but a payload from the
other chunk makes it valid by composition. Neither defined it, so an asset container with
a geo payload and a materials payload exported `over "asset"` instead of `def "asset"`.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene


def _make_asset_file(path, name: str) -> str:
    layer = Sdf.Layer.CreateNew(str(path / f"{name}.usda"))
    Sdf.CreatePrimInLayer(layer, "/asset").specifier = Sdf.SpecifierDef
    layer.Save()
    return str(path / f"{name}.usda").replace("\\", "/")


def test_two_payloads_on_one_prim_define_it(tmp_path) -> None:
    geo = _make_asset_file(tmp_path, "geo")
    mat = _make_asset_file(tmp_path, "mat")

    with Scene() as scene:
        layer = scene.create.layer(layer_name="container")
        target = scene.create.edit_target(layer, edit_target="container")
        with target:
            first = scene.create.payload(file_path=geo, primpath_location="/asset")
            scene.create.payload(first, file_path=mat, primpath_location="/asset")
        stage = scene.get_stage()

        specs = [
            spec
            for spec in stage.GetPrimAtPath("/asset").GetPrimStack()
            if spec.layer in stage.GetLayerStack(includeSessionLayers=True)
        ]
        assert any(spec.specifier == Sdf.SpecifierDef for spec in specs)
        assert sum(1 for spec in specs if spec.hasPayloads) == 2, "both payloads are still there"
