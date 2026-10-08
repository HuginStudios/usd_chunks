"""A cook of a chunk an arc brings in lands inside the asset, and goes where the asset goes.

house.usda holds a cube and a network moving it, and lists house.cook.usda. The shot brings
the house in at /World/house/chunks by a reference, payload, inherit or variant. The cook
runs at stage paths; its result is mapped through the arc into house.cook.usda's namespace,
so it is as weak as the arc -- a shot opinion on the cube beats it -- and travels with every
arc that brings the house's layers in.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from network_files import save_network
from usd_chunks import chunk_layers, chunk_stage
from usd_chunks.chunk_script import Scene

_GEO = "/World/house/chunks/geo"


def _shot(tmp_path, arc: str, target: str = _GEO) -> tuple:
    paths = save_network(tmp_path / "house.usda", {"H1": (target, 1.0)}, ends=("output",))
    house = Sdf.Layer.FindOrOpen(str(tmp_path / "house.usda"))
    UsdGeom.Cube.Define(Usd.Stage.Open(house), "/chunks/geo")
    chunk_layers.add_cook_sublayer(house)
    house.Save()
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    stage = Usd.Stage.Open(shot)
    UsdGeom.Cube.Define(stage, "/a")
    prim = stage.DefinePrim("/World/house/chunks")
    if arc == "reference":
        prim.GetReferences().AddReference("./house.usda", "/chunks")
    elif arc == "payload":
        prim.GetPayloads().AddPayload("./house.usda", "/chunks")
    elif arc == "inherit":
        cls = stage.CreateClassPrim("/_house")
        cls.GetReferences().AddReference("./house.usda", "/chunks")
        prim.GetInherits().AddInherit("/_house")
    else:
        variant_set = prim.GetVariantSets().AddVariantSet("build")
        variant_set.AddVariant("house")
        variant_set.SetVariantSelection("house")
        with variant_set.GetVariantEditContext():
            prim.GetReferences().AddReference("./house.usda", "/chunks")
    shot.Save()
    return tmp_path / "shot.usda", "/World/house" + paths["H1"]


def _cook(scene, chunk_path):
    chunk = next(c for c in scene.get_chunks() if chunk_stage.get_chunk_prim_path(c) == chunk_path)
    chunk.execute()


def _z(stage, path):
    xform = UsdGeom.Xformable(stage.GetPrimAtPath(path))
    return xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()[2]


@pytest.mark.parametrize("arc", ["reference", "payload", "inherit", "variant"])
def test_the_result_lands_in_the_assets_cook_layer_at_its_own_paths(tmp_path, arc):
    shot, h1 = _shot(tmp_path, arc)
    scene = Scene.open(str(shot), "pytest")
    try:
        _cook(scene, h1)
        stage = scene.get_stage()
        assert _z(stage, _GEO) == pytest.approx(1.0)
        cook = Sdf.Layer.Find(str(tmp_path / "house.cook.usda"))
        mirrors = [Sdf.Layer.Find(p) for p in cook.subLayerPaths]
        assert any(m.GetPrimAtPath("/chunks/geo") for m in mirrors), "not mapped into the house"
        order = stage.GetPrimAtPath(_GEO).GetAttribute("xformOpOrder")
        assert order.GetPropertyStack()[0].layer in mirrors
    finally:
        scene.close()


def test_a_shot_opinion_beats_the_assets_cook(tmp_path):
    """The cook is as strong as the reference: a local opinion in the shot wins."""
    shot, h1 = _shot(tmp_path, "reference")
    layer = Sdf.Layer.FindOrOpen(str(shot))
    stage = Usd.Stage.Open(layer)
    over = stage.OverridePrim(_GEO)
    UsdGeom.Xformable(over).CreateXformOpOrderAttr().Set(["xformOp:translate"])
    over.CreateAttribute("xformOp:translate", Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(0, 0, 7))
    layer.Save()
    scene = Scene.open(str(shot), "pytest")
    try:
        _cook(scene, h1)
        assert _z(scene.get_stage(), _GEO) == pytest.approx(7.0)
    finally:
        scene.close()


def test_a_result_outside_the_asset_stays_in_the_session_layer(tmp_path):
    """The house moving the shot's /a: the reference does not carry /a, so the result
    cannot travel with the house, and cooks as it always has."""
    shot, h1 = _shot(tmp_path, "reference", target="/a")
    scene = Scene.open(str(shot), "pytest")
    try:
        _cook(scene, h1)
        stage = scene.get_stage()
        assert _z(stage, "/a") == pytest.approx(1.0)
        cook = Sdf.Layer.Find(str(tmp_path / "house.cook.usda"))
        assert not any(Sdf.Layer.Find(p).rootPrims for p in cook.subLayerPaths)
    finally:
        scene.close()
