"""AMD MaterialX Library: Tiling scales the constant the material multiplies its UVs by."""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from tests.test_amd_materialx_reapply import _Scene
from usd_chunks import chunk_registry
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

# The shape usdMtlx gives a library material: the UVs times a float constant, and another
# constant (roughness) that is not the tiling.
_MATERIAL = """#usda 1.0
def Scope "MaterialX" {
  def Scope "Materials" {
    def Material "Chrome" {
      def NodeGraph "NG_Chrome" {
        def Shader "UVScale" {
          uniform token info:id = "ND_constant_float"
          float inputs:value = 2
          float outputs:out
        }
        def Shader "RoughnessMin" {
          uniform token info:id = "ND_constant_float"
          float inputs:value = 0.25
          float outputs:out
        }
        def Shader "node_texcoord" {
          uniform token info:id = "ND_texcoord_vector2"
          int inputs:index = 0
          float2 outputs:out
        }
        def Shader "node_multiply" {
          uniform token info:id = "ND_multiply_vector2FA"
          float2 inputs:in1.connect = </MaterialX/Materials/Chrome/NG_Chrome/node_texcoord.outputs:out>
          float inputs:in2.connect = </MaterialX/Materials/Chrome/NG_Chrome/UVScale.outputs:out>
          float2 outputs:out
        }
      }
    }
  }
}
"""

_UVSCALE = "/Chrome/NG_Chrome/UVScale.inputs:value"
_ROUGHNESS = "/Chrome/NG_Chrome/RoughnessMin.inputs:value"


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A library node with the stand-in material applied to /cube."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/cube")
    scene = _Scene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    module = reg.get_chunk_module("AMDMaterialXLibrary")
    made = module.create() if module.loaded else module.load()
    live = module.module  # the copy the registry cooks with
    monkeypatch.setattr(live, "_load_catalog", lambda node: None)
    standin = tmp_path / "chrome.usda"
    standin.write_text(_MATERIAL)
    monkeypatch.setattr(
        live, "_fetch_package",
        lambda title, resolution, cache_dir: (str(standin).replace("\\", "/"), {"mtlx_material_name": "Chrome"}, None),
    )
    for c in made:
        scene.add_chunk(c)
    node = made[0]
    node.get_param("target_primpath").value = "/cube"
    node.get_param("picked_title").value = "Chrome"
    live.execute(node)
    assert stage.GetAttributeAtPath(_UVSCALE).Get() == 2.0, "the stand-in material is on the stage"
    return live, node, stage


def test_tiling_scales_the_uv_constant_only(library):
    live, node, stage = library

    node.get_param("tiling").value = 3.0
    live.execute(node)

    assert stage.GetAttributeAtPath(_UVSCALE).Get() == pytest.approx(6.0)
    assert stage.GetAttributeAtPath(_ROUGHNESS).Get() == pytest.approx(0.25)


def test_tiling_back_to_one_is_the_material_as_it_comes(library):
    live, node, stage = library
    node.get_param("tiling").value = 3.0
    live.execute(node)

    node.get_param("tiling").value = 1.0
    live.execute(node)

    assert stage.GetAttributeAtPath(_UVSCALE).Get() == pytest.approx(2.0)


def test_a_cook_with_the_same_tiling_leaves_the_material_alone(library, monkeypatch):
    live, node, stage = library
    node.get_param("tiling").value = 3.0
    live.execute(node)
    remakes = []
    monkeypatch.setattr(live, "_remake_spec", lambda layer, path: remakes.append(path))

    live.execute(node)  # what a cook from upstream runs, e.g. after the cube moved

    assert remakes == []
    assert stage.GetAttributeAtPath(_UVSCALE).Get() == pytest.approx(6.0)
