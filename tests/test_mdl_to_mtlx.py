"""MDL-shaded materials get a portable MaterialX surface alongside their MDL one.

Assets authored for Omniverse bind their looks through MDL (`OmniPBR.mdl`, `OmniGlass.mdl`).
MDL is NVIDIA's, so every other renderer finds no surface it can build and falls back to
default grey -- the asset opens anywhere and shades nowhere.

The chunk adds a second surface output rather than replacing anything, so the MDL output
and every existing binding survive untouched. These tests pin that, and the two mistakes
that made the first version render black.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chunks"))
import mdl_to_mtlx  # noqa: E402


class _Node:
    """The slice of a chunk the module's execute() actually touches."""

    def __init__(self, stage, path):
        self._stage = stage
        self._params = {"primpath_location": path}

    def get_stage(self):
        return self._stage

    def get_param(self, name):
        value = self._params.get(name)
        return None if value is None else _Param(value)


class _Param:
    def __init__(self, value):
        self.value = value


def _mdl_material(stage, path, sub_id="OmniPBR", **inputs):
    """A material shaded the way these assets shade: MDL, via info:mdl:sourceAsset."""
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/Shader")
    prim = shader.GetPrim()
    prim.CreateAttribute("info:mdl:sourceAsset", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath(f"{sub_id}.mdl")
    )
    prim.CreateAttribute(
        "info:mdl:sourceAsset:subIdentifier", Sdf.ValueTypeNames.Token
    ).Set(sub_id)
    for name, value in inputs.items():
        if isinstance(value, Sdf.AssetPath):
            shader.CreateInput(name, Sdf.ValueTypeNames.Asset).Set(value)
        elif isinstance(value, tuple):
            shader.CreateInput(name, Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*value))
        else:
            shader.CreateInput(name, Sdf.ValueTypeNames.Float).Set(float(value))
    material.CreateSurfaceOutput("mdl").ConnectToSource(shader.ConnectableAPI(), "out")
    return material


def _run(stage, root="/World"):
    mdl_to_mtlx.execute(_Node(stage, root))


def _surface(stage, path):
    """The mtlx surface shader the chunk authored for a material, or None."""
    output = UsdShade.Material(stage.GetPrimAtPath(path)).GetSurfaceOutput("mtlx")
    if not output or not output.HasConnectedSource():
        return None
    return UsdShade.Shader(output.GetConnectedSource()[0].GetPrim())


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    return stage


def test_an_mtlx_surface_is_added():
    stage = _stage()
    _mdl_material(stage, "/World/Look", diffuse_color_constant=(0.8, 0.2, 0.1))

    _run(stage)

    surface = _surface(stage, "/World/Look")
    assert surface is not None
    assert surface.GetShaderId() == "ND_standard_surface_surfaceshader"


def test_the_mdl_output_is_left_alone():
    """The point of adding an output instead of replacing the material: nothing that
    already binds to it has to change, and Omniverse still gets its own surface."""
    stage = _stage()
    _mdl_material(stage, "/World/Look", diffuse_color_constant=(0.8, 0.2, 0.1))

    _run(stage)

    mdl = UsdShade.Material(stage.GetPrimAtPath("/World/Look")).GetSurfaceOutput("mdl")
    assert mdl and mdl.HasConnectedSource()


def test_a_non_mdl_material_is_skipped():
    stage = _stage()
    material = UsdShade.Material.Define(stage, "/World/Plain")
    shader = UsdShade.Shader.Define(stage, "/World/Plain/Shader")
    shader.CreateIdAttr("ND_standard_surface_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(shader.ConnectableAPI(), "out")

    _run(stage)

    assert stage.GetPrimAtPath("/World/Plain/mtlx_surface").IsValid() is False


def test_scalars_are_carried_across():
    stage = _stage()
    _mdl_material(
        stage, "/World/Look",
        metallic_constant=0.9,
        reflection_roughness_constant=0.25,
        specular_level=0.7,
    )

    surface = (_run(stage), _surface(stage, "/World/Look"))[1]

    assert surface.GetInput("metalness").Get() == pytest.approx(0.9)
    assert surface.GetInput("specular_roughness").Get() == pytest.approx(0.25)
    assert surface.GetInput("specular").Get() == pytest.approx(0.7)


class TestBaseColor:
    def test_a_constant_is_used_when_there_is_no_texture(self):
        stage = _stage()
        _mdl_material(stage, "/World/Look", diffuse_color_constant=(0.8, 0.2, 0.1))

        _run(stage)

        value = _surface(stage, "/World/Look").GetInput("base_color").Get()
        assert tuple(value) == pytest.approx((0.8, 0.2, 0.1), abs=1e-6)

    def test_a_texture_beats_the_constant(self, tmp_path):
        """These assets author diffuse_color_constant as flat white and put the whole look
        in the map, so honouring only the constant yields a correctly-lit white appliance."""
        stage = _stage()
        texture = tmp_path / "albedo.png"
        texture.write_bytes(b"")
        _mdl_material(
            stage, "/World/Look",
            diffuse_color_constant=(1.0, 1.0, 1.0),
            diffuse_texture=Sdf.AssetPath(str(texture)),
        )

        _run(stage)

        base = _surface(stage, "/World/Look").GetInput("base_color")
        assert base.HasConnectedSource(), "the map should drive base_color, not the constant"
        image = UsdShade.Shader(base.GetConnectedSource()[0].GetPrim())
        assert image.GetShaderId() == "ND_image_color3"

    def test_the_texture_path_is_absolute(self, tmp_path):
        """An asset path is anchored to the layer that authored it, and this surface is
        authored into a different layer. Carrying the authored relative path across means
        the texture silently fails to resolve and every material renders black -- which
        reads as a shading bug rather than a path one."""
        stage = _stage()
        texture = tmp_path / "albedo.png"
        texture.write_bytes(b"")
        _mdl_material(
            stage, "/World/Look", diffuse_texture=Sdf.AssetPath(str(texture)),
        )

        _run(stage)

        base = _surface(stage, "/World/Look").GetInput("base_color")
        image = UsdShade.Shader(base.GetConnectedSource()[0].GetPrim())
        authored = image.GetInput("file").Get().path
        assert Path(authored).is_absolute()
        assert Path(authored) == texture


def test_glass_becomes_transmissive():
    """OmniGlass has no standard_surface equivalent to copy across, so this is the one
    place a look is asserted rather than translated."""
    stage = _stage()
    _mdl_material(stage, "/World/Glazing", sub_id="OmniGlass")

    _run(stage)

    surface = _surface(stage, "/World/Glazing")
    assert surface.GetInput("transmission").Get() == pytest.approx(1.0)
    assert surface.GetInput("specular_roughness").Get() == pytest.approx(0.02)


def test_running_twice_is_idempotent():
    """A chunk's execute is re-run constantly during a solve."""
    stage = _stage()
    _mdl_material(stage, "/World/Look", diffuse_color_constant=(0.5, 0.5, 0.5))

    _run(stage)
    _run(stage)

    material = UsdShade.Material(stage.GetPrimAtPath("/World/Look"))
    assert material.GetSurfaceOutput("mtlx").HasConnectedSource()
    assert len([c for c in material.GetPrim().GetChildren()
                if c.GetName() == "mtlx_surface"]) == 1


def test_nothing_happens_without_a_prim_path():
    stage = _stage()
    _mdl_material(stage, "/World/Look", diffuse_color_constant=(0.5, 0.5, 0.5))

    mdl_to_mtlx.execute(_Node(stage, ""))

    assert stage.GetPrimAtPath("/World/Look/mtlx_surface").IsValid() is False
