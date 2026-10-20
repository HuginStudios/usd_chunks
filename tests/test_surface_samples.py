"""Surface samples: the cheap signature of an asset, and the normals read from it.

A signature stands in for reading the asset when nothing changed, so it must move with
everything the read depends on. The normals were made cheaper to read, and must come out
the same as before to the bit.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import surface_samples
from usd_chunks.mesh_math import fan_triangulate

Image = pytest.importorskip("PIL.Image")

_BOX_FACES = [0, 2, 3, 1, 4, 5, 7, 6, 0, 1, 5, 4, 2, 6, 7, 3, 0, 4, 6, 2, 1, 3, 7, 5]
_BOX_POINTS = np.array([[x, y, z] for z in (-1, 1) for y in (-1, 1) for x in (-1, 1)], dtype=np.float32)


def _box(stage, path):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(_BOX_POINTS))
    mesh.GetFaceVertexCountsAttr().Set([4] * 6)
    mesh.GetFaceVertexIndicesAttr().Set(_BOX_FACES)
    return mesh


def _preview(stage, path, colour, texture=None):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    diffuse = shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)
    if texture is None:
        diffuse.Set(Gf.Vec3f(*colour))
    else:
        tex = UsdShade.Shader.Define(stage, f"{path}/Texture")
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(texture)
        diffuse.ConnectToSource(tex.ConnectableAPI(), "rgb")
    return material


@pytest.fixture
def asset(tmp_path):
    """A box with a colour, normals and a textured subset, under a parent; beside it a
    PointInstancer scattering a second box."""
    png = tmp_path / "wood.png"
    Image.fromarray(np.full((2, 2, 3), 90, dtype=np.uint8), "RGB").save(png)
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Asset")
    body = _box(stage, "/Asset/Body")
    body.CreateDisplayColorPrimvar(UsdGeom.Tokens.constant).Set([Gf.Vec3f(0.5, 0.5, 0.5)])
    body.CreateNormalsAttr([Gf.Vec3f(0, 0, 1)] * 24)
    body.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    paint = _preview(stage, "/Looks/Paint", (0.8, 0.1, 0.1))
    wood = _preview(stage, "/Looks/Wood", None, texture=str(png))
    _preview(stage, "/Looks/Other", (0.1, 0.1, 0.8))
    binding = UsdShade.MaterialBindingAPI.Apply(body.GetPrim())
    binding.Bind(paint)
    subset = binding.CreateMaterialBindSubset("Top", Vt.IntArray([1]))
    UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(wood)

    instancer = UsdGeom.PointInstancer.Define(stage, "/Asset/Scatter")
    _box(stage, "/Asset/Scatter/Proto")
    instancer.CreatePrototypesRel().SetTargets(["/Asset/Scatter/Proto"])
    instancer.CreateProtoIndicesAttr([0, 0])
    instancer.CreatePositionsAttr([Gf.Vec3f(4, 0, 0), Gf.Vec3f(8, 0, 0)])
    return stage, png


def _signature(stage):
    return surface_samples.signature(stage.GetPrimAtPath("/Asset"))


def _rebind(stage):
    UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/Asset/Body")).Bind(
        UsdShade.Material(stage.GetPrimAtPath("/Looks/Other"))
    )


def _rewrite(png):
    Image.fromarray(np.full((2, 2, 3), 200, dtype=np.uint8), "RGB").save(png)
    later = os.path.getmtime(png) + 10
    os.utime(png, (later, later))


EDITS = {
    "a point moves": lambda stage, png: UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Body")).GetPointsAttr().Set(
        Vt.Vec3fArray.FromNumpy(_BOX_POINTS * 1.5)
    ),
    "the faces change": lambda stage, png: UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Body"))
    .GetFaceVertexIndicesAttr()
    .Set(_BOX_FACES[4:] + _BOX_FACES[:4]),
    "a primvar changes": lambda stage, png: UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Body"))
    .GetDisplayColorPrimvar()
    .Set([Gf.Vec3f(0.1, 0.9, 0.1)]),
    "the normals change": lambda stage, png: UsdGeom.Mesh(stage.GetPrimAtPath("/Asset/Body"))
    .GetNormalsAttr()
    .Set([Gf.Vec3f(0, 1, 0)] * 24),
    "a subset changes": lambda stage, png: UsdGeom.Subset(stage.GetPrimAtPath("/Asset/Body/Top"))
    .GetIndicesAttr()
    .Set(Vt.IntArray([2])),
    "the material changes": lambda stage, png: UsdShade.Shader(stage.GetPrimAtPath("/Looks/Paint/Surface"))
    .GetInput("diffuseColor")
    .Set(Gf.Vec3f(0.1, 0.8, 0.1)),
    "the texture file is rewritten": lambda stage, png: _rewrite(png),
    "another material is bound": lambda stage, png: _rebind(stage),
    "the mesh is hidden": lambda stage, png: UsdGeom.Imageable(stage.GetPrimAtPath("/Asset/Body")).MakeInvisible(),
    "the parent moves": lambda stage, png: UsdGeom.Xformable(stage.GetPrimAtPath("/Asset"))
    .AddTranslateOp()
    .Set(Gf.Vec3d(0, 0, 3)),
    "an instance moves": lambda stage, png: UsdGeom.PointInstancer(stage.GetPrimAtPath("/Asset/Scatter"))
    .GetPositionsAttr()
    .Set([Gf.Vec3f(4, 0, 0), Gf.Vec3f(9, 0, 0)]),
}


@pytest.mark.parametrize("edit", EDITS)
def test_the_signature_moves_with_what_the_read_reads(asset, edit) -> None:
    stage, png = asset
    before = _signature(stage)
    assert _signature(stage) == before

    EDITS[edit](stage, png)
    assert _signature(stage) != before


def test_an_unchanged_asset_is_signed_without_checksumming_it_again(asset, monkeypatch) -> None:
    """The second signature recognises USD's arrays by address and computes no CRC."""
    stage, _png = asset
    first: dict = {}
    before = surface_samples.signature(stage.GetPrimAtPath("/Asset"), current=first)
    crcs = []
    real = surface_samples.zlib.crc32
    monkeypatch.setattr(surface_samples.zlib, "crc32", lambda *a: crcs.append(1) or real(*a))
    second: dict = {}

    assert surface_samples.signature(stage.GetPrimAtPath("/Asset"), previous=first, current=second) == before
    assert second.keys() == first.keys()
    assert len(crcs) == 2, "only the placements of the two meshes, worked out afresh"


# ---------------------------------------------------------------------------
# normals
# ---------------------------------------------------------------------------


def _normals_as_they_were(prim, tris, corners, face_of_tri):
    """read_corner_normals' authored-normals path before it was made cheaper."""
    mesh = UsdGeom.Mesh(prim)
    normals = surface_samples._primvar_corners(prim, "normals", tris, corners, face_of_tri)
    if normals is None and (values := mesh.GetNormalsAttr().Get()):
        normals = surface_samples.per_corner(values, mesh.GetNormalsInterpolation(), tris, corners, face_of_tri)
    normals = np.asarray(normals, dtype=np.float64)
    length = np.linalg.norm(normals, axis=-1, keepdims=True)
    normals = np.divide(normals, length, out=np.zeros_like(normals), where=length > 0.0)
    return normals.astype(np.float32)


@pytest.mark.parametrize("as_primvar", [True, False], ids=["primvar", "attribute"])
@pytest.mark.parametrize("interpolation, count", [("constant", 1), ("uniform", 6), ("vertex", 8), ("faceVarying", 24)])
def test_authored_normals_read_the_same_to_the_bit(as_primvar, interpolation, count) -> None:
    stage = Usd.Stage.CreateInMemory()
    mesh = _box(stage, "/Box")
    values = np.random.default_rng(3).normal(size=(count, 3)).astype(np.float32) * 7.0
    if count > 1:
        values[-1] = 0.0  # a zero normal stays zero
    if as_primvar:
        primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("normals", Sdf.ValueTypeNames.Normal3fArray, interpolation)
        primvar.Set(Vt.Vec3fArray.FromNumpy(values))
    else:
        mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(values))
        mesh.SetNormalsInterpolation(interpolation)
    tris, corners, face_of_tri = fan_triangulate(mesh.GetFaceVertexCountsAttr().Get(), _BOX_FACES)
    points = _BOX_POINTS.astype(np.float64)

    normals = surface_samples.read_corner_normals(mesh.GetPrim(), points, tris, corners, face_of_tri)
    expected = _normals_as_they_were(mesh.GetPrim(), tris, corners, face_of_tri)
    assert normals.dtype == np.float32 and normals.shape == (12, 3, 3)
    assert np.array_equal(normals.view(np.uint32), expected.view(np.uint32))
    assert normals.flags.writeable
