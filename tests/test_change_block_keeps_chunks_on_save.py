"""A network built in a change_block saves every chunk it made.

Two department layers side by side, built in a `chunk.change_block()`: the Edit Target back
to the Session Layer cooks at the block's end, on a cook stage (skip_cook), and resolved
"Session Layer" there -- the cook stage's own throwaway session layer. Re-homing the chunks
below it moved the second Layer's and Edit Target's chunk prims into that layer, which was
then thrown away. The saved network had neither, and opened again it cooked without the set
and the camera. Two benchmark agents building a shot hit it.
"""
from __future__ import annotations

import contextlib

import pytest
from pxr import Sdf, Usd, UsdGeom, UsdLux

from usd_chunks import Scene, chunk


def _set_asset(path) -> None:
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/Set").GetPrim())
    UsdGeom.Cube.Define(stage, "/Set/Table")
    stage.Save()


def _chunk_prims(path) -> set[str]:
    layer = Sdf.Layer.FindOrOpen(str(path))
    found = set()

    def visit(spec):
        if spec.typeName == "Chunk":
            found.add(spec.name)
        for child in spec.nameChildren:
            visit(child)

    for root in layer.rootPrims:
        visit(root)
    return found


@pytest.mark.parametrize("block", [False, True], ids=["one-by-one", "change_block"])
def test_department_layers_built_in_a_block_save_every_chunk(tmp_path, block) -> None:
    (tmp_path / "inputs").mkdir()
    _set_asset(tmp_path / "inputs" / "set.usda")
    net = tmp_path / "net.usda"
    with Scene.new(path=str(net)) as scene:
        with chunk.change_block() if block else contextlib.nullcontext():
            layout = scene.create.layer(layer_name="layout")
            into = scene.create.edit_target(layout, edit_target="layout")
            world = scene.create.prim(into, primpath_location="/World", prim_type="Xform")
            ref = scene.create.reference(world, file_path="inputs/set.usda", primpath_location="/World/Set")
            cam = scene.create.camera(ref, primpath_location="/World/Cam", tx=5.0)
            back = scene.create.edit_target(cam, edit_target="Session Layer")
            lighting = scene.create.layer(back, layer_name="lighting")
            into = scene.create.edit_target(lighting, edit_target="lighting")
            key = scene.create.rect_light(into, primpath_location="/World/Key")
            back = scene.create.edit_target(key, edit_target="Session Layer")
        scene.set_output(back)
        made = {c.name for c in scene.chunks_in_order()}
        scene.save()

    assert made <= _chunk_prims(net), sorted(made - _chunk_prims(net))
    with Scene.open(str(net)) as scene:
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/World/Set/Table").IsA(UsdGeom.Gprim)
        assert stage.GetPrimAtPath("/World/Cam").IsA(UsdGeom.Camera)
        assert stage.GetPrimAtPath("/World/Key").IsA(UsdLux.RectLight)
