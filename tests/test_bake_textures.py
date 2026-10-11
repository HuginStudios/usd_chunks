"""Bake Textures: the look of one surface, carried onto another's UVs.

The colour lands in the right texels, the normal map is in the tangent frame a renderer
decodes it in (OpenGL: red along +u, green along +v), nothing outside the charts is left
empty, and the chunk binds a material that reads what it wrote.
"""

from __future__ import annotations

import importlib
import os

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk_registry, cook_cache, surface_samples, texture_bake
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

Image = pytest.importorskip("PIL.Image")
bt = importlib.import_module("chunks.modelling.bake_textures")

pytestmark = pytest.mark.skipif(not texture_bake._HAS_SCIPY, reason="needs scipy")


def _plane_target(size=2.0, z=0.0, st_scale=1.0):
    """A quad in the xy plane as a bake Target: u along +x, v along +y, normal +z."""
    points = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], dtype=np.float64) * (size / 2)
    points[:, 2] = z
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float64) * st_scale
    normals = np.tile([0.0, 0.0, 1.0], (2, 3, 1))
    return texture_bake.Target(points, tris, uv[tris], normals)


def _define(stage, path, points, counts, indices, colour=None):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points, dtype=np.float32)))
    mesh.GetFaceVertexCountsAttr().Set(list(map(int, counts)))
    mesh.GetFaceVertexIndicesAttr().Set(list(map(int, indices)))
    if colour is not None:
        mesh.CreateDisplayColorAttr([Gf.Vec3f(*colour)])
    return mesh


def _quad(stage, path, x0, x1, y0=-1.0, y1=1.0, z=0.0, colour=None, tilt=(0.0, 0.0)):
    """An axis-aligned quad; `tilt` adds (dz/dx, dz/dy) so its normal leans."""
    xs = np.array([x0, x1, x1, x0])
    ys = np.array([y0, y0, y1, y1])
    zs = z + tilt[0] * xs + tilt[1] * ys
    return _define(stage, path, np.stack([xs, ys, zs], axis=1), [4], [0, 1, 2, 3], colour)


def _bake(stage, root="/Source", target=None, res=32, maps=texture_bake.MAPS):
    sources = surface_samples.gather_sources(stage.GetPrimAtPath(root))
    request = texture_bake.BakeRequest(sources, target or _plane_target(), res, frozenset(maps))
    return texture_bake.bake(request)


# ---------------------------------------------------------------------------
# rasterizing
# ---------------------------------------------------------------------------


def test_a_quads_two_triangles_cover_every_texel_exactly_once() -> None:
    target = _plane_target()
    texels = texture_bake.rasterize(target.corner_st, 64)
    flat = texels.rows * 64 + texels.cols
    assert len(flat) == 64 * 64
    assert len(np.unique(flat)) == 64 * 64
    assert np.allclose(texels.bary.sum(axis=1), 1.0)


def test_row_zero_is_the_top_of_the_texture() -> None:
    """USD's st has v = 0 at the bottom; an image's row 0 is its top."""
    upper = np.array([[[0.0, 0.5], [1.0, 0.5], [1.0, 1.0]]])  # a triangle in the top half
    texels = texture_bake.rasterize(upper, 16)
    assert texels.rows.max() < 8


# ---------------------------------------------------------------------------
# what gets baked
# ---------------------------------------------------------------------------


def test_colour_lands_where_the_source_shows_it() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    _quad(stage, "/Source/Left", -1, 0, colour=(1, 0, 0))
    _quad(stage, "/Source/Right", 0, 1, colour=(0, 0, 1))

    image = _bake(stage, maps=[texture_bake.MAP_BASE_COLOUR])[texture_bake.MAP_BASE_COLOUR]
    left, right = image[:, :12].reshape(-1, 3).mean(axis=0), image[:, 20:].reshape(-1, 3).mean(axis=0)
    assert np.allclose(left, (1, 0, 0), atol=0.02)
    assert np.allclose(right, (0, 0, 1), atol=0.02)


def test_a_source_parallel_to_the_target_bakes_a_flat_normal_map() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    _quad(stage, "/Source/Sheet", -1, 1, z=0.05)
    image = _bake(stage, maps=[texture_bake.MAP_NORMAL])[texture_bake.MAP_NORMAL]
    assert np.allclose(image.reshape(-1, 3).mean(axis=0), (0.5, 0.5, 1.0), atol=0.01)


@pytest.mark.parametrize(
    ("tilt", "channel"),
    [((-0.5, 0.0), 0), ((0.0, -0.5), 1)],
    ids=["leans-along-u-is-red", "leans-along-v-is-green"],
)
def test_the_normal_map_is_in_the_opengl_tangent_frame(tilt, channel) -> None:
    """A source surface sloping down along +x has its normal leaning toward +x (+u): the
    red channel goes above 0.5. Sloping down along +y leans toward +v: green, OpenGL's
    "green up" -- a DirectX-convention map would invert it and light the bump upside down."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    _quad(stage, "/Source/Slope", -1, 1, tilt=tilt)
    image = _bake(stage, maps=[texture_bake.MAP_NORMAL])[texture_bake.MAP_NORMAL]
    mean = image.reshape(-1, 3).mean(axis=0)
    other = 1 - channel
    assert mean[channel] > 0.6
    assert abs(mean[other] - 0.5) < 0.02


def test_texels_outside_the_charts_take_the_nearest_chart_colour() -> None:
    """A gutter left black bleeds into the chart's edge as soon as the map is mip-mapped."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    _quad(stage, "/Source/Sheet", -1, 1, colour=(0, 1, 0))
    image = _bake(stage, target=_plane_target(st_scale=0.5), maps=[texture_bake.MAP_BASE_COLOUR])
    image = image[texture_bake.MAP_BASE_COLOUR]
    assert np.allclose(image.reshape(-1, 3), (0, 1, 0), atol=0.02)  # the chart is a quarter


def test_a_two_sided_card_does_not_cancel_its_own_normal() -> None:
    """Leaf cards are seen from both sides. Samples from a card whose normal faces away
    from the target must be flipped toward it, not averaged into nothing."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    card = _quad(stage, "/Source/Card", -1, 1, z=0.05)
    card.CreateOrientationAttr(UsdGeom.Tokens.leftHanded)  # its normal points -z
    image = _bake(stage, maps=[texture_bake.MAP_NORMAL])[texture_bake.MAP_NORMAL]
    assert image.reshape(-1, 3).mean(axis=0)[2] > 0.95


def test_a_map_written_as_srgb_reads_back_as_the_linear_colour() -> None:
    linear = np.full((4, 4, 3), 0.2, dtype=np.float32)
    encoded = texture_bake.linear_to_srgb(linear)
    back = surface_samples.srgb_to_linear(np.round(encoded * 255) / 255)
    assert np.allclose(back, 0.2, atol=0.005)


# ---------------------------------------------------------------------------
# the chunk
# ---------------------------------------------------------------------------


class _DummyScene(SceneSender):
    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cook_cache"))
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def make():
        module = reg.get_chunk_module("bake_textures")
        chunks = module.create() if module.loaded else module.load()
        for c in chunks:
            scene.add_chunk(c)
        node = chunks[0]
        node.get_param("output_dir").value = str(tmp_path / "maps")
        node.get_param("resolution").value = 32
        return node

    return stage, make


def _target_with_uvs(stage, path, z=0.0):
    mesh = _quad(stage, path, -1, 1, z=z)
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying).Set(
        Vt.Vec2fArray([(0, 0), (1, 0), (1, 1), (0, 1)])
    )
    return mesh


def test_the_chunk_writes_maps_and_binds_a_material_reading_them(wired) -> None:
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/Asset")
    _quad(stage, "/Asset/Body", -1, 1, z=0.05, colour=(0.8, 0.2, 0.1))
    _target_with_uvs(stage, "/Proxy")

    node = make()
    node.get_param("source_path").value = "/Asset"
    node.get_param("target_path").value = "/Proxy"

    colour_file = node.get_param("out_basecolor").value
    normal_file = node.get_param("out_normal").value
    assert os.path.isfile(colour_file) and os.path.isfile(normal_file)
    assert node.get_param("out_material").value == "/Proxy_baked"

    material = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/Proxy")).ComputeBoundMaterial()[0]
    assert material and material.GetPath() == Sdf.Path("/Proxy_baked")
    shader = material.ComputeSurfaceSource("")[0]
    texture = UsdShade.Shader(shader.GetInput("diffuseColor").GetConnectedSources()[0][0].source.GetPrim())
    assert os.path.normcase(texture.GetInput("file").Get().path) == os.path.normcase(colour_file.replace("\\", "/"))


def test_a_target_without_uvs_is_reported_not_baked(wired) -> None:
    stage, make = wired
    _quad(stage, "/Asset", -1, 1, colour=(1, 1, 1))
    _quad(stage, "/Proxy", -1, 1)

    node = make()
    node.get_param("source_path").value = "/Asset"
    node.get_param("target_path").value = "/Proxy"
    assert "UV Unwrap" in node.get_param("readout").value
    assert node.get_param("out_basecolor").value == ""


def test_a_target_inside_the_source_is_not_baked_from_itself(wired) -> None:
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/Asset")
    _quad(stage, "/Asset/Body", -1, 1, z=0.05, colour=(0.0, 1.0, 0.0))
    # Grey and closer than the body: sampled, it would win every texel.
    mesh = _target_with_uvs(stage, "/Asset/Proxy")
    mesh.CreateDisplayColorAttr([Gf.Vec3f(0.5, 0.5, 0.5)])

    node = make()
    node.get_param("bake_normal").value = False
    node.get_param("source_path").value = "/Asset"
    node.get_param("target_path").value = "/Asset/Proxy"

    pixels = np.asarray(Image.open(node.get_param("out_basecolor").value)).reshape(-1, 3)
    assert pixels.mean(axis=0)[1] > 250 and pixels.mean(axis=0)[0] < 5


def test_a_rebake_with_new_content_replaces_its_files(wired) -> None:
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/Asset")
    body = _quad(stage, "/Asset/Body", -1, 1, z=0.05, colour=(1.0, 0.0, 0.0))
    _target_with_uvs(stage, "/Proxy")

    node = make()
    node.get_param("source_path").value = "/Asset"
    node.get_param("target_path").value = "/Proxy"
    first = node.get_param("out_basecolor").value

    body.GetDisplayColorAttr().Set([Gf.Vec3f(0.0, 0.0, 1.0)])
    node.get_param("resolution").value = 16
    second = node.get_param("out_basecolor").value
    assert second != first  # a new name, so a renderer's texture cache can't show the old one
    assert os.path.isfile(second) and not os.path.exists(first)


# ---------------------------------------------------------------------------
# what the source's materials say
# ---------------------------------------------------------------------------


def _glass(stage, path, weight=1.0):
    """An OpenPBR-only glass, as Blender's exporter writes it: no UsdPreviewSurface."""
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/Glass")
    shader.CreateIdAttr("ND_open_pbr_surface_surfaceshader")
    shader.CreateInput("transmission_weight", Sdf.ValueTypeNames.Float).Set(weight)
    material.CreateSurfaceOutput("mtlx").ConnectToSource(shader.ConnectableAPI(), "surface")
    return material


def test_glass_is_seen_through_not_baked_as_a_surface() -> None:
    """A turntable's dust cover: baked as surface, it covers the platter in grey."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    _quad(stage, "/Source/Platter", -1, 1, z=0.0, colour=(1.0, 0.0, 0.0))
    lid = _quad(stage, "/Source/Lid", -1, 1, z=0.05, colour=(0.5, 0.5, 0.5))
    UsdShade.MaterialBindingAPI.Apply(lid.GetPrim()).Bind(_glass(stage, "/Looks/Glass"))

    image = _bake(stage, target=_plane_target(z=0.06), maps=[texture_bake.MAP_BASE_COLOUR])
    assert np.allclose(image[texture_bake.MAP_BASE_COLOUR].reshape(-1, 3).mean(axis=0), (1, 0, 0), atol=0.02)


def test_a_uv_transform_between_primvar_and_texture_is_applied(tmp_path) -> None:
    """Fabrics tile through a UsdTransform2d. Ignored, one stretched copy of the weave
    covers the whole chair; applied, a 2x scale shows the texture's stripe twice."""
    stripes = np.zeros((8, 8, 3), dtype=np.uint8)
    stripes[:, :4] = (255, 0, 0)  # left half red, right half blue
    stripes[:, 4:] = (0, 0, 255)
    png = tmp_path / "stripes.png"
    Image.fromarray(stripes, "RGB").save(png)

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Source")
    sheet = _quad(stage, "/Source/Sheet", -1, 1, z=0.05)
    UsdGeom.PrimvarsAPI(sheet).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex).Set(
        Vt.Vec2fArray([(0, 0), (1, 0), (1, 1), (0, 1)])
    )
    material = UsdShade.Material.Define(stage, "/Looks/Fabric")
    surface = UsdShade.Shader.Define(stage, "/Looks/Fabric/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(surface.ConnectableAPI(), "surface")
    reader = UsdShade.Shader.Define(stage, "/Looks/Fabric/UV")
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
    mapping = UsdShade.Shader.Define(stage, "/Looks/Fabric/Mapping")
    mapping.CreateIdAttr("UsdTransform2d")
    mapping.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(), "result")
    mapping.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(2, 2))
    texture = UsdShade.Shader.Define(stage, "/Looks/Fabric/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(str(png))
    texture.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("raw")
    texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(mapping.ConnectableAPI(), "result")
    surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(texture.ConnectableAPI(), "rgb")
    UsdShade.MaterialBindingAPI.Apply(sheet.GetPrim()).Bind(material)

    image = _bake(stage, res=64, maps=[texture_bake.MAP_BASE_COLOUR])[texture_bake.MAP_BASE_COLOUR]
    row = image[32, :, 0] > 0.5  # red along one row, left to right
    changes = np.count_nonzero(row[1:] != row[:-1])
    assert changes == 3  # red, blue, red, blue: the stripe pair twice
