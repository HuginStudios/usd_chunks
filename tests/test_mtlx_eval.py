"""mtlx_eval: a MaterialX network in USD, evaluated per sample.

The networks here are the shapes Blender's exporter writes -- image -> convert -> hsvadjust
-> mix, texcoord -> multiply -> rotate3d -> add -- because those are what a proxy bake has
to get right. A node outside the supported set must refuse to compile, not half-evaluate.
"""

from __future__ import annotations

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdShade

from usd_chunks import mtlx_eval, surface_samples

Image = pytest.importorskip("PIL.Image")


def _surface(stage, path="/Mat"):
    material = UsdShade.Material.Define(stage, path)
    surface = UsdShade.Shader.Define(stage, f"{path}/Surface")
    surface.CreateIdAttr("ND_open_pbr_surface_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(surface.ConnectableAPI(), "surface")
    return material, surface


def _node(stage, path, shader_id, output_type, **inputs):
    node = UsdShade.Shader.Define(stage, path)
    node.CreateIdAttr(shader_id)
    for name, (type_name, value) in inputs.items():
        node.CreateInput(name, type_name).Set(value)
    node.CreateOutput("out", output_type)
    return node


def _connect(node, name, type_name, source):
    node.CreateInput(name, type_name).ConnectToSource(source.ConnectableAPI(), "out")


def _run(program, n=4, **primvars):
    return program.run({k: np.asarray(v, dtype=np.float64) for k, v in primvars.items()}, surface_samples.Images(), n)


def test_a_constant_hsv_adjust_matches_a_reference_conversion() -> None:
    """Blender's darkening: value x 0.3 on a pure-ish green, with a small hue nudge."""
    stage = Usd.Stage.CreateInMemory()
    _material, surface = _surface(stage)
    hsv = _node(
        stage,
        "/Mat/Hsv",
        "ND_hsvadjust_color3",
        Sdf.ValueTypeNames.Color3f,
        **{"in": (Sdf.ValueTypeNames.Color3f, Gf.Vec3f(0.2, 0.6, 0.1)), "amount": (Sdf.ValueTypeNames.Vector3f, Gf.Vec3f(0.0, 1.0, 0.3))},
    )
    _connect(surface, "base_color", Sdf.ValueTypeNames.Color3f, hsv)

    program = mtlx_eval.compile_input(surface.GetInput("base_color"))
    assert np.allclose(_run(program)[0], np.array([0.2, 0.6, 0.1]) * 0.3, atol=1e-6)


def test_texture_coordinates_are_scaled_before_the_image_reads_them(tmp_path) -> None:
    """The sofa's tiling: texcoord -> convert -> multiply(4) -> convert -> image."""
    stripes = np.zeros((4, 8, 3), dtype=np.uint8)
    stripes[:, :4] = 255  # white left half, black right half
    png = tmp_path / "stripes.png"
    Image.fromarray(stripes, "RGB").save(png)

    stage = Usd.Stage.CreateInMemory()
    _material, surface = _surface(stage)
    coords = _node(stage, "/Mat/UV", "ND_texcoord_vector2", Sdf.ValueTypeNames.Float2)
    to3 = _node(stage, "/Mat/To3", "ND_convert_vector2_vector3", Sdf.ValueTypeNames.Vector3f)
    _connect(to3, "in", Sdf.ValueTypeNames.Float2, coords)
    scale = _node(
        stage, "/Mat/Scale", "ND_multiply_vector3", Sdf.ValueTypeNames.Vector3f,
        in2=(Sdf.ValueTypeNames.Vector3f, Gf.Vec3f(4, 4, 4)),
    )
    _connect(scale, "in1", Sdf.ValueTypeNames.Vector3f, to3)
    to2 = _node(stage, "/Mat/To2", "ND_convert_vector3_vector2", Sdf.ValueTypeNames.Float2)
    _connect(to2, "in", Sdf.ValueTypeNames.Vector3f, scale)
    image = _node(
        stage, "/Mat/Image", "ND_image_color3", Sdf.ValueTypeNames.Color3f,
        file=(Sdf.ValueTypeNames.Asset, str(png)),
    )
    _connect(image, "texcoord", Sdf.ValueTypeNames.Float2, to2)
    _connect(surface, "base_color", Sdf.ValueTypeNames.Color3f, image)

    program = mtlx_eval.compile_input(surface.GetInput("base_color"))
    assert program.primvars == {"st"}
    # x4: u 0.05 -> 0.2 (white), 0.1 -> 0.4 (white), 0.2 -> 0.8 (black), 0.3 -> 1.2 wraps
    # to 0.2 (white). Unscaled, all four would read the white half.
    values = _run(program, st=[[0.05, 0.5], [0.1, 0.5], [0.2, 0.5], [0.3, 0.5]])
    assert values[:, 0].round().tolist() == [1.0, 1.0, 0.0, 1.0]


def test_an_srgb_tagged_image_is_linearized_and_an_untagged_one_is_not(tmp_path) -> None:
    grey = np.full((2, 2, 3), 128, dtype=np.uint8)
    png = tmp_path / "grey.png"
    Image.fromarray(grey, "RGB").save(png)

    stage = Usd.Stage.CreateInMemory()
    _material, surface = _surface(stage)
    tagged = _node(stage, "/Mat/A", "ND_image_color3", Sdf.ValueTypeNames.Color3f, file=(Sdf.ValueTypeNames.Asset, str(png)))
    tagged.GetInput("file").GetAttr().SetColorSpace("srgb_texture")
    plain = _node(stage, "/Mat/B", "ND_image_float", Sdf.ValueTypeNames.Float, file=(Sdf.ValueTypeNames.Asset, str(png)))
    _connect(surface, "base_color", Sdf.ValueTypeNames.Color3f, tagged)
    _connect(surface, "specular_roughness", Sdf.ValueTypeNames.Float, plain)

    colour = _run(mtlx_eval.compile_input(surface.GetInput("base_color")), st=np.full((4, 2), 0.5))
    rough = _run(mtlx_eval.compile_input(surface.GetInput("specular_roughness")), st=np.full((4, 2), 0.5))
    assert np.allclose(colour[:, 0], 0.2158, atol=0.002)  # sRGB 128 -> linear
    assert np.allclose(rough[:, 0], 128 / 255, atol=0.002)


def test_a_node_it_cannot_evaluate_refuses_to_compile() -> None:
    stage = Usd.Stage.CreateInMemory()
    _material, surface = _surface(stage)
    noise = _node(stage, "/Mat/Noise", "ND_fractal3d_color3", Sdf.ValueTypeNames.Color3f)
    _connect(surface, "base_color", Sdf.ValueTypeNames.Color3f, noise)
    with pytest.raises(mtlx_eval.Unsupported):
        mtlx_eval.compile_input(surface.GetInput("base_color"))


def test_the_materialx_graph_outranks_the_preview_surface_beside_it() -> None:
    """Exporters write both; the preview is the approximation, the graph is the look."""
    stage = Usd.Stage.CreateInMemory()
    material, surface = _surface(stage)
    preview = UsdShade.Shader.Define(stage, "/Mat/Preview")
    preview.CreateIdAttr("UsdPreviewSurface")
    preview.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0, 0, 1))
    material.CreateSurfaceOutput().ConnectToSource(preview.ConnectableAPI(), "surface")
    hsv = _node(
        stage, "/Mat/Hsv", "ND_hsvadjust_color3", Sdf.ValueTypeNames.Color3f,
        **{"in": (Sdf.ValueTypeNames.Color3f, Gf.Vec3f(0.8, 0.8, 0.8)), "amount": (Sdf.ValueTypeNames.Vector3f, Gf.Vec3f(0, 1, 1))},
    )
    _connect(surface, "base_color", Sdf.ValueTypeNames.Color3f, hsv)

    look = surface_samples._look(material)
    assert "colour" in look.programs
    assert np.allclose(_run(look.programs["colour"])[0], 0.8)
