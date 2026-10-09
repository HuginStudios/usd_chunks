"""A cook of a chunk an arc brings in belongs to the layer that imports it, never the asset.

house.usda holds a cube and a network moving it, and lists house.cook.usda as its asset
department published it. The shot brings the house in at /World/house/chunks by a
reference, payload, inherit or variant, and lists shot.cook.usda. An asset is shared by every
shot that uses it, so the shot's cook of the house goes to shot.cook.usda, at shot paths:
stronger than the house (local beats the arc), weaker than shot.usda's own opinions, and
house.usda and house.cook.usda are never written.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from network_files import run, save_network
from usd_chunks import chunk_layers, chunk_registry, chunk_stage
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
    chunk_layers.add_cook_sublayer(shot)
    shot.Save()
    return tmp_path / "shot.usda", "/World/house" + paths["H1"]


def _cook(scene, chunk_path):
    chunk = next(c for c in scene.get_chunks() if chunk_stage.get_chunk_prim_path(c) == chunk_path)
    chunk.execute()
    return chunk


def _z(stage, path):
    xform = UsdGeom.Xformable(stage.GetPrimAtPath(path))
    return xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()[2]


def _shot_opinion(shot):
    layer = Sdf.Layer.FindOrOpen(str(shot))
    stage = Usd.Stage.Open(layer)
    over = stage.OverridePrim(_GEO)
    UsdGeom.Xformable(over).CreateXformOpOrderAttr().Set(["xformOp:translate"])
    over.CreateAttribute("xformOp:translate", Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(0, 0, 7))
    layer.Save()


@pytest.mark.parametrize("arc", ["reference", "payload", "inherit", "variant"])
def test_the_result_lands_in_the_importing_layers_cook_layer(tmp_path, arc):
    shot, h1 = _shot(tmp_path, arc)
    house_files = {p: p.read_bytes() for p in (tmp_path / "house.usda", tmp_path / "house.cook.usda")}
    scene = Scene.open(str(shot), "pytest")
    try:
        chunk = _cook(scene, h1)
        stage = scene.get_stage()
        assert _z(stage, _GEO) == pytest.approx(1.0)
        target = chunk_registry.ChunkLayerManager().get_chunk_target(chunk.get_hash())
        assert target == Sdf.Layer.Find(str(tmp_path / "shot.cook.usda"))
        assert not Sdf.Layer.Find(str(tmp_path / "house.cook.usda")).subLayerPaths
    finally:
        scene.close()
    assert {p: p.read_bytes() for p in house_files} == house_files


def test_a_shot_opinion_beats_the_shots_cook_of_the_asset(tmp_path):
    """The cook is a sublayer of shot.usda: what shot.usda says itself wins."""
    shot, h1 = _shot(tmp_path, "reference")
    _shot_opinion(shot)
    scene = Scene.open(str(shot), "pytest")
    try:
        _cook(scene, h1)
        assert _z(scene.get_stage(), _GEO) == pytest.approx(7.0)
    finally:
        scene.close()


def test_a_shot_opinion_beats_the_cook_in_a_headless_run(tmp_path):
    shot, _ = _shot(tmp_path, "reference")
    _shot_opinion(shot)
    out = tmp_path / "out.usda"
    run(shot, "--all", "--export", str(out))
    assert _z(Usd.Stage.Open(str(out)), _GEO) == pytest.approx(7.0)


def test_a_bake_goes_beside_the_shot_and_never_into_the_asset(tmp_path):
    """`run --bake` writes the shot's cook of the house to shot.cook.usda; house.usda and the
    asset department's house.cook.usda stay as published."""
    shot, h1 = _shot(tmp_path, "reference")
    house_files = {p: p.read_bytes() for p in (tmp_path / "house.usda", tmp_path / "house.cook.usda")}
    out = run(shot, "--bake", "--all")
    assert "shot.cook.usda" in out and "house.cook.usda" not in out
    assert {p: p.read_bytes() for p in house_files} == house_files
    bake = Sdf.Layer.OpenAsAnonymous(str(tmp_path / "shot.cook.usda"))
    assert bake.GetPrimAtPath(_GEO)
    assert set(bake.customLayerData["usdchunks:cooks"]) == {h1}


def test_the_shots_bake_travels_with_the_shot(tmp_path):
    """The bake is part of shot.usda's layer stack, so a sequence referencing the shot --
    opened in plain USD, nothing cooking -- sees the moved cube."""
    shot, _ = _shot(tmp_path, "reference")
    run(shot, "--bake", "--all")
    seq = Sdf.Layer.CreateNew(str(tmp_path / "seq.usda"))
    spec = Sdf.CreatePrimInLayer(seq, "/sh010")
    spec.specifier = Sdf.SpecifierDef
    spec.referenceList.Prepend(Sdf.Reference("./shot.usda", "/World"))
    seq.Save()
    stage = Usd.Stage.Open(str(tmp_path / "seq.usda"))
    for layer in stage.GetUsedLayers():
        if not layer.anonymous:
            layer.Reload()
    assert _z(stage, "/sh010/house/chunks/geo") == pytest.approx(1.0)


def test_a_result_outside_the_asset_lands_in_the_shots_cook_too(tmp_path):
    """The house moving the shot's /a is shot content like any other."""
    shot, h1 = _shot(tmp_path, "reference", target="/a")
    scene = Scene.open(str(shot), "pytest")
    try:
        chunk = _cook(scene, h1)
        assert _z(scene.get_stage(), "/a") == pytest.approx(1.0)
        target = chunk_registry.ChunkLayerManager().get_chunk_target(chunk.get_hash())
        assert target == Sdf.Layer.Find(str(tmp_path / "shot.cook.usda"))
    finally:
        scene.close()
