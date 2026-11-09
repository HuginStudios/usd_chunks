"""An export leaves out the prims a Prune Prim pruned.

Prune Prim deactivates a prim and flags it as meant to be deleted, and said exports strip
it, but nothing did: every export kept it as an inactive `def`. Agents duplicating a column
into a ring and pruning the original found the original still in the delivered file.

A prim the export does not own -- one inside a referenced asset -- keeps its inactive over,
or removing it would bring the prim back.
"""
from __future__ import annotations

from pathlib import Path

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk


def _ring(scene):
    col = scene.create.usd_cylinder(prim_path="/World/column")
    ring = scene.create.duplicate_prim(col, source_prim="/World/column", dest_path="/World/columns",
                                       copies=4, copy_rotate=(0.0, 90.0, 0.0))
    pruned = scene.create.prune_prim(ring, prim_path="/World/column")
    scene.set_output(pruned)
    return pruned


def _spec(path: Path, prim: str):
    layer = Sdf.Layer.FindOrOpen(str(path))
    layer.Reload()
    return layer, layer.GetPrimAtPath(prim)


def test_scene_export_leaves_out_a_pruned_prim(tmp_path) -> None:
    with Scene() as scene:
        _ring(scene)
        scene.export(str(tmp_path / "ring.usda"))
    layer, column = _spec(tmp_path / "ring.usda", "/World/column")
    assert column is None, layer.ExportToString()
    assert len(layer.GetPrimAtPath("/World/columns").nameChildren) == 4


def test_export_usd_flattened_leaves_out_a_pruned_prim(tmp_path) -> None:
    with Scene() as scene:
        pruned = _ring(scene)
        exp = scene.create.export_usd(pruned, flatten=True, output_file=str(tmp_path / "flat.usda"))
        scene.set_output(exp)
        scene.commit()
    layer, column = _spec(tmp_path / "flat.usda", "/World/column")
    assert column is None, layer.ExportToString()


def test_export_usd_layers_leave_out_a_pruned_prim(tmp_path) -> None:
    with Scene() as scene:
        with chunk.change_block():
            layout = scene.create.layer(layer_name="layout")
            into = scene.create.edit_target(layout, edit_target="layout")
            pruned = _ring_in(scene, into)
            back = scene.create.edit_target(pruned, edit_target="Session Layer")
            exp = scene.create.export_usd(back, output_dir=str(tmp_path), bake_all=True)
        scene.set_output(exp)
        scene.commit()
    for written in tmp_path.glob("*.usda"):
        layer, column = _spec(written, "/World/column")
        assert column is None, (written.name, layer.ExportToString())
    stage = Usd.Stage.Open(str(tmp_path / "layout.usda"))
    assert len(stage.GetPrimAtPath("/World/columns").GetChildren()) == 4


def _ring_in(scene, above):
    col = scene.create.usd_cylinder(above, prim_path="/World/column")
    ring = scene.create.duplicate_prim(col, source_prim="/World/column", dest_path="/World/columns",
                                       copies=4, copy_rotate=(0.0, 90.0, 0.0))
    return scene.create.prune_prim(ring, prim_path="/World/column")


def test_a_pruned_prim_inside_a_referenced_asset_stays_hidden(tmp_path) -> None:
    asset = Usd.Stage.CreateNew(str(tmp_path / "set.usda"))
    asset.SetDefaultPrim(UsdGeom.Xform.Define(asset, "/Set").GetPrim())
    UsdGeom.Cube.Define(asset, "/Set/Table")
    UsdGeom.Cube.Define(asset, "/Set/Chair")
    asset.Save()
    with Scene() as scene:
        ref = scene.create.reference(file_path=str(tmp_path / "set.usda"), primpath_location="/World/Set")
        pruned = scene.create.prune_prim(ref, prim_path="/World/Set/Table")
        scene.set_output(pruned)
        scene.export(str(tmp_path / "out.usda"))
    stage = Usd.Stage.Open(str(tmp_path / "out.usda"))
    assert not stage.GetPrimAtPath("/World/Set/Table").IsActive()  # not brought back
    assert stage.GetPrimAtPath("/World/Set/Chair").IsActive()
