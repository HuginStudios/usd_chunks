"""Bind Material's Strength: a mesh binding that leaves its face subsets' bindings alone.

A mesh with a bark and a leaf face subset (a tree, as most assets come) needs the mesh
bound to bark and the leaf subset to leaves. Bind Material always bound as
strongerThanDescendants, so the mesh's binding beat the subset's and the leaves rendered
as bark.
"""
from __future__ import annotations

import pytest
from pxr import UsdGeom, UsdShade

from usd_chunks import Scene


def _tree(stage):
    """/tree/trunk, a two-face mesh with a "leaves" subset on face 1, and two materials."""
    mesh = UsdGeom.Mesh.Define(stage, "/tree/trunk")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([4, 4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3, 1, 4, 5, 2])
    UsdGeom.Subset.CreateGeomSubset(mesh, "leaves", UsdGeom.Tokens.face, [1], "materialBind")
    for name in ("bark", "leaves"):
        UsdShade.Material.Define(stage, f"/looks/{name}")


def _bound(stage, path):
    return UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()[0].GetPath()


def test_weaker_than_subsets_leaves_the_leaf_subset_its_own_material():
    with Scene() as scene:
        stage = scene.get_stage()
        _tree(stage)
        bark = scene.create.bind_material(material_path="/looks/bark", primpath_location="/tree/trunk",
                                          strength="Weaker than Descendants")
        scene.create.bind_material(bark, material_path="/looks/leaves", primpath_location="/tree/trunk/leaves")

        assert _bound(stage, "/tree/trunk") == "/looks/bark"
        assert _bound(stage, "/tree/trunk/leaves") == "/looks/leaves"


def test_stronger_is_still_the_default():
    """Binding a group still beats what its children had bound: a referenced asset's own
    materials, say."""
    with Scene() as scene:
        stage = scene.get_stage()
        _tree(stage)
        UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath("/tree/trunk/leaves")).Bind(
            UsdShade.Material(stage.GetPrimAtPath("/looks/leaves")))
        node = scene.create.bind_material(material_path="/looks/bark", primpath_location="/tree")

        assert node.get_param("strength").value == "Stronger than Descendants"
        assert _bound(stage, "/tree/trunk/leaves") == "/looks/bark"


@pytest.mark.parametrize("strength", ["Weaker than Descendants", "Stronger than Descendants"])
def test_the_strength_is_saved(tmp_path, strength):
    path = tmp_path / "net.usda"
    with Scene.new(path=str(path)) as scene:
        last = scene.create.mesh(prim_path="/tree/trunk", mesh_type="cube")
        last = scene.create.preview_surface(last, mat_path="/looks/bark")
        scene.create.bind_material(last, name="Bark", material_path="/looks/bark", primpath_location="/tree/trunk",
                                   strength=strength)
        scene.save()

    with Scene.open(str(path)) as scene:
        assert scene.get_chunk("Bark").get_param("strength").value == strength
        rel = UsdShade.MaterialBindingAPI(scene.get_stage().GetPrimAtPath("/tree/trunk")).GetDirectBindingRel()
        expected = "weakerThanDescendants" if strength.startswith("Weaker") else "strongerThanDescendants"
        assert UsdShade.MaterialBindingAPI.GetMaterialBindingStrength(rel) == expected
