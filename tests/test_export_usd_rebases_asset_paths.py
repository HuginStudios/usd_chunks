"""A layer Export USD writes into another folder still finds the files it brings in.

A relative asset path is found from the file it is written in. A Reference to
`inputs/set.usda` works in the network, whose folder holds inputs/, but Export USD wrote it
unchanged into shot/layout.usda, where USD looks for shot/inputs/set.usda and finds nothing.
An agent benchmark hit it and wrote a chunk of its own to fix the paths after export.
"""
from __future__ import annotations

from pathlib import Path

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk


def _set_asset(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/Set").GetPrim())
    UsdGeom.Cube.Define(stage, "/Set/Table")
    stage.Save()


def _export_layout(folder: Path, output_dir: str) -> None:
    with Scene.new(path=str(folder / "net.usda")) as scene:
        with chunk.change_block():
            world = scene.create.prim(primpath_location="/World", prim_type="Xform")
            layout = scene.create.layer(world, layer_name="layout")
            into = scene.create.edit_target(layout, edit_target="layout")
            ref = scene.create.reference(into, file_path="inputs/set.usda", primpath_location="/World/Set")
            back = scene.create.edit_target(ref, edit_target="Session Layer")
            export = scene.create.export_usd(back, output_dir=output_dir, bake_all=True)
        scene.set_output(export)
        scene.commit()


def _reference_in(layer_path: Path) -> str:
    layer = Sdf.Layer.FindOrOpen(str(layer_path))  # held: a spec does not keep its layer alive
    return layer.GetPrimAtPath("/World/Set").referenceList.prependedItems[0].assetPath


def test_a_reference_exported_into_a_subfolder_still_resolves(tmp_path, monkeypatch) -> None:
    _set_asset(tmp_path / "inputs" / "set.usda")
    _export_layout(tmp_path, str(tmp_path / "shot"))
    layout = tmp_path / "shot" / "layout.usda"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # found from the file, not from where it is opened
    stage = Usd.Stage.Open(str(layout))
    assert stage.GetPrimAtPath("/World/Set/Table"), _reference_in(layout)


def test_a_reference_exported_beside_the_network_is_left_as_written(tmp_path) -> None:
    _set_asset(tmp_path / "inputs" / "set.usda")
    _export_layout(tmp_path, str(tmp_path))
    assert _reference_in(tmp_path / "layout.usda") == "inputs/set.usda"


def test_a_file_under_the_export_folder_stays_a_search_path(tmp_path) -> None:
    _set_asset(tmp_path / "shot" / "inputs" / "set.usda")
    (tmp_path / "inputs").mkdir()
    # The network finds it as shot/inputs/set.usda; from shot/ that is inputs/set.usda.
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        with chunk.change_block():
            world = scene.create.prim(primpath_location="/World", prim_type="Xform")
            layout = scene.create.layer(world, layer_name="layout")
            into = scene.create.edit_target(layout, edit_target="layout")
            ref = scene.create.reference(into, file_path="shot/inputs/set.usda", primpath_location="/World/Set")
            back = scene.create.edit_target(ref, edit_target="Session Layer")
            export = scene.create.export_usd(back, output_dir=str(tmp_path / "shot"), bake_all=True)
        scene.set_output(export)
        scene.commit()
    assert _reference_in(tmp_path / "shot" / "layout.usda") == "inputs/set.usda"
