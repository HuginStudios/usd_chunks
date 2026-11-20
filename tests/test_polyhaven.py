"""Poly Haven Model and Poly Haven Material, against a cache that is already filled: no network.

* a model is referenced at Location, one part or all, at the origin, with UsdPreviewSurface
  looks from its own textures bound where the file bound its MaterialX ones;
* a texture becomes a material with colour, roughness and normal maps, bound to Bind To;
* nothing downloaded and no network: the node says so instead of raising.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import Scene, polyhaven


def _fake_model(cache, name="rock_set", res="1k"):
    """A model file shaped like Poly Haven's: a default prim with two parts side by side, each
    a mesh bound to a material whose texture is <name>_diff_<res>.jpg."""
    folder = cache / name
    folder.mkdir(parents=True)
    (folder / "textures").mkdir()
    diffuse = folder / "textures" / f"{name}_diff_{res}.jpg"
    diffuse.write_bytes(b"jpg")
    stage = Usd.Stage.CreateNew(str(folder / f"{name}_{res}.usdc"))
    root = UsdGeom.Xform.Define(stage, f"/{name}")
    stage.SetDefaultPrim(root.GetPrim())
    mat = UsdShade.Material.Define(stage, f"/{name}/_materials/{name}")
    tex = UsdShade.Shader.Define(stage, f"/{name}/_materials/{name}/diff")
    tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(str(diffuse))
    for i, part in enumerate(("a", "b")):
        xf = UsdGeom.Xform.Define(stage, f"/{name}/{name}_{part}")
        xf.AddTranslateOp().Set(Gf.Vec3d(3.0 * (i + 1), 0, 0))
        mesh = UsdGeom.Mesh.Define(stage, f"/{name}/{name}_{part}/geo")
        mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
        mesh.CreateFaceVertexCountsAttr([3])
        mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
    stage.Save()
    return folder / f"{name}_{res}.usdc"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv(polyhaven.ENV, str(tmp_path))
    return tmp_path


def test_a_part_is_referenced_at_the_origin_with_its_look(cache):
    _fake_model(cache)
    with Scene() as scene:
        stage = scene.get_stage()
        node = scene.create.polyhaven_model(model="rock_set", part="rock_set_b", primpath_location="/kit/rock")

        prim = stage.GetPrimAtPath("/kit/rock")
        assert prim.GetChild("geo").IsA(UsdGeom.Mesh)
        assert UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation() == Gf.Vec3d(0, 0, 0)
        look = UsdShade.MaterialBindingAPI(prim.GetChild("geo")).ComputeBoundMaterial()[0]
        assert look.GetPath() == "/kit/rock/Looks/rock_set"
        surface = UsdShade.Shader(stage.GetPrimAtPath("/kit/rock/Looks/rock_set/surface"))
        assert surface.GetIdAttr().Get() == "UsdPreviewSurface"
        assert stage.GetPrimAtPath("/kit/rock/Looks/rock_set/diffuse").GetAttribute("inputs:file").Get().path \
            .endswith("rock_set_diff_1k.jpg")
        assert "rock_set_b of 2 parts" in node.get_param("readout").value


def test_all_parts_each_at_the_origin_for_a_scatter_row(cache):
    _fake_model(cache)
    with Scene() as scene:
        stage = scene.get_stage()
        scene.create.polyhaven_model(model="rock_set", primpath_location="/kit/rocks")

        for part in ("rock_set_a", "rock_set_b"):
            xf = UsdGeom.Xformable(stage.GetPrimAtPath(f"/kit/rocks/{part}"))
            assert xf.ComputeLocalToWorldTransform(0).ExtractTranslation() == Gf.Vec3d(0, 0, 0)


def test_with_nothing_downloaded_and_no_network_it_says_so(cache, monkeypatch):
    def offline(*_args):
        raise OSError("no network")

    monkeypatch.setattr(polyhaven, "download_model", offline)
    with Scene() as scene:
        node = scene.create.polyhaven_model(model="not_here")

        assert node.get_param("readout").value.startswith("download failed")
        assert not scene.get_stage().GetPrimAtPath("/not_here")


def test_a_texture_becomes_a_material_on_the_prims(cache):
    for key, _ in polyhaven.TEXTURE_MAPS:
        path = polyhaven.texture_files("bricks")[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
    with Scene() as scene:
        stage = scene.get_stage()
        UsdGeom.Mesh.Define(stage, "/wall")
        scene.create.polyhaven_material(texture="bricks", primpath_location="/wall", tiling=3.0)

        material = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/wall")).ComputeBoundMaterial()[0]
        assert material.GetPath() == "/materials/bricks"
        surface = UsdShade.Shader(stage.GetPrimAtPath("/materials/bricks/surface"))
        for name in ("diffuseColor", "roughness", "normal"):
            assert surface.GetInput(name).HasConnectedSource(), name
        tiling = UsdShade.Shader(stage.GetPrimAtPath("/materials/bricks/tiling"))
        assert tuple(tiling.GetInput("scale").Get()) == (3.0, 3.0)
