"""A texture whose image cannot be loaded must not be authored with a UV chain behind it.

Reported live as usdview crashing when switching the render delegate from Cycles back to
Storm:

    ERROR: Failed to load 1 image files
    Runtime Error: in BuildShaderGraph at line 526 of .../cycles/src/hydra/material.cpp
        -- Could not find node '/Mapping' to connect

The two lines are one story. hdCycles translates our UsdTransform2d into a Cycles `mapping`
node, so when the UsdUVTexture it feeds has no loadable image and gets dropped, the
connection into `/Mapping` is left pointing at a node the graph never built, and the
delegate throws mid-rebuild.

The throw itself is hdCycles' (a dangling connection should not be fatal), and is not
fixable from this repo. What is ours is the trigger: both UV-checker chunks authored the
whole primvar-reader -> UsdTransform2d -> texture.st chain unconditionally, including when
the file was a stale absolute path from another machine or a texture that had since been
deleted. A material with no image to sample is better bound as a plain surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pxr import Usd, UsdGeom, UsdShade

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "modelling"))
import add_mesh  # noqa: E402


@pytest.fixture
def prim():
    # The stage has to stay referenced for the test's lifetime -- returning only the prim
    # lets the stage be collected and every prim handle into it expire.
    stage = Usd.Stage.CreateInMemory()
    mesh = UsdGeom.Mesh.Define(stage, "/mesh").GetPrim()
    yield mesh
    del stage


def _texture_prims(stage):
    return [p for p in stage.Traverse()
            if p.GetAttribute("info:id") and p.GetAttribute("info:id").Get() == "UsdUVTexture"]


class TestUsableCheck:
    def test_a_real_file_is_usable(self):
        real = str(Path(add_mesh.__file__).parent / "checker.png")
        assert add_mesh.texture_file_is_usable(real) is True

    def test_a_missing_file_is_not(self):
        assert add_mesh.texture_file_is_usable("C:/definitely/not/here.png") is False

    def test_an_empty_path_is_not(self):
        assert add_mesh.texture_file_is_usable("") is False

    def test_a_udim_pattern_is_left_alone(self):
        """UDIM sets never resolve as a single file and are meant not to -- treating one as
        missing would refuse to author a perfectly valid texture."""
        assert add_mesh.texture_file_is_usable("C:/tex/diffuse.<UDIM>.exr") is True


class TestAuthoring:
    def test_a_missing_texture_authors_no_texture_network(self, prim):
        """The regression: no UsdUVTexture, and so no UsdTransform2d connection into one."""
        add_mesh.add_texture(prim, texture_file="C:/definitely/not/here.png")

        stage = prim.GetStage()
        assert _texture_prims(stage) == []
        transform2d = stage.GetPrimAtPath("/materials/UVCheckerMaterial/transform2d")
        assert not transform2d.IsValid(), "no UV chain should be left dangling"

    def test_a_missing_texture_still_binds_a_usable_material(self, prim):
        """Falling back must leave the prim shaded, not unbound."""
        add_mesh.add_texture(prim, texture_file="C:/definitely/not/here.png")

        bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        assert bound, "the prim must still get a material"
        surface = bound.GetPrim().GetStage().GetPrimAtPath(
            "/materials/UVCheckerMaterial/previewSurface"
        )
        assert surface.IsValid()
        assert surface.GetAttribute("inputs:diffuseColor").Get() is not None

    def test_a_real_texture_still_authors_the_full_chain(self, prim):
        """The normal path is unchanged -- reader -> transform2d -> texture.st."""
        real = str(Path(add_mesh.__file__).parent / "checker.png")
        add_mesh.add_texture(prim, texture_file=real)

        stage = prim.GetStage()
        assert len(_texture_prims(stage)) == 1
        texture = UsdShade.Shader(stage.GetPrimAtPath("/materials/UVCheckerMaterial/texture"))
        st = texture.GetInput("st")
        assert st and st.HasConnectedSource(), "the UV chain must still be wired"
