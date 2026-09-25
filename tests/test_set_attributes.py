"""Set Attributes: typed values and API schemas on prims, surviving a save and reload.

The case that asked for it: V-Ray only shows a dome light to camera when the dome carries
VRayLightAPI's ``vray:env:bg:tex:use`` / ``from_dome`` -- attributes no light chunk knows
about. They used to go in through an Inline USD chunk, which hides the edit in a block of
text; a chunk with one visible row per attribute does not.
"""

from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import export_chunks, initialize as _chunk_usd_init

sa = importlib.import_module("scripts.usd.set_attributes")


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene(stage):
    class _S(_DummyScene):
        def get_stage(self):
            return stage

    s = _S()
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return s


def _make(scene):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("set_attributes")
    c = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(c)
    return c


def _fill(node, rows):
    lg = node.get_param("attributes", chunk.is_inst(chunk.ListGroup))
    lg.size = len(rows)
    for i, (name, type_name, value) in enumerate(rows):
        lg.get_indexed_param(lg.get_param("attr_name"), i).value = name
        lg.get_indexed_param(lg.get_param("attr_type"), i).value = type_name
        lg.get_indexed_param(lg.get_param("attr_value"), i).value = value


@pytest.mark.parametrize("type_name, text, expected", [
    ("bool", "1", True), ("bool", "false", False), ("int", "3", 3), ("float", "0.25", 0.25),
    ("float3", "(1, 2, 3)", Gf.Vec3f(1, 2, 3)), ("color3f", "0.5 0.4 0.3", Gf.Vec3f(0.5, 0.4, 0.3)),
    ("string", "hi", "hi"),
])
def test_values_parse_by_type(type_name, text, expected):
    assert sa.parse(type_name, text) == expected


def test_a_bad_value_is_reported_not_written(qapp):
    stage = Usd.Stage.CreateInMemory()
    s = _scene(stage)
    UsdGeom.Xform.Define(stage, "/World/Thing")
    node = _make(s)
    node.get_param("prim_path").value = "/World/Thing"
    _fill(node, [("custom:ok", "int", "4"), ("custom:bad", "float3", "1 2")])
    node.execute()
    prim = stage.GetPrimAtPath("/World/Thing")
    assert prim.GetAttribute("custom:ok").Get() == 4
    assert not prim.HasAttribute("custom:bad")
    assert "custom:bad" in node.get_param("readout").value


def test_schemas_and_values_come_back_after_a_save(qapp, tmp_path):
    stage = Usd.Stage.CreateInMemory()
    s = _scene(stage)
    UsdLux.DomeLight.Define(stage, "/World/lights/Sky")
    node = _make(s)
    node.get_param("prim_path").value = "/World/lights/Sky"
    node.get_param("schemas").value = "VRayLightAPI"
    _fill(node, [("vray:env:bg:tex:use", "bool", "1"), ("vray:env:bg:tex:from_dome", "bool", "true")])
    node.execute()

    path = str(tmp_path / "scene.usda")
    export_chunks(s, path)
    stage2 = Usd.Stage.Open(path)
    UsdLux.DomeLight.Define(stage2, "/World/lights/Sky")  # the light itself comes from elsewhere
    _scene(stage2)
    sky = stage2.GetPrimAtPath("/World/lights/Sky")
    # Added to what the light already has -- never replacing it: prepended, not explicit.
    authored = [spec.GetInfo("apiSchemas") for spec in sky.GetPrimStack() if spec.HasInfo("apiSchemas")]
    assert any("VRayLightAPI" in op.prependedItems for op in authored)
    assert not any(op.isExplicit for op in authored)
    assert "LightAPI" in sky.GetAppliedSchemas()
    assert sky.GetAttribute("vray:env:bg:tex:use").Get() is True
    assert sky.GetAttribute("vray:env:bg:tex:from_dome").Get() is True
    assert sky.GetAttribute("vray:env:bg:tex:use").GetTypeName() == Sdf.ValueTypeNames.Bool
