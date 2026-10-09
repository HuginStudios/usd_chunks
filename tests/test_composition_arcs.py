"""A chunk network composed in by any arc runs where USD composed it.

The house network (house.usda, moving /a and /b) is brought into a shot by a reference, a
payload or an inherit, under /World/house/chunks, beside a chain of the shot's own (moving
/c). Whatever the arc: the house loads in place, cooks before the shot's chain, and nothing
is copied into the shot. A payload can also be unloaded and loaded again while open.
"""

from __future__ import annotations

import pytest
from pxr import Sdf

from network_files import run_and_read, save_cubes, save_network
from usd_chunks import chunk_stage
from usd_chunks.chunk_script import Scene

ARCS = ("reference", "payload", "inherit")
H1 = "/World/house/chunks/ChunkGroup/H1"


def _shot(tmp_path, arc: str):
    save_cubes(tmp_path / "set.usda", "abc")
    save_network(tmp_path / "house.usda", {"H1": ("/a", 1.0), "H2": ("/b", 2.0)}, ends=("output",))
    shot = tmp_path / "shot.usda"
    save_network(shot, {"S1": ("/c", 3.0)}, ends=("output",))
    layer = Sdf.Layer.FindOrOpen(str(shot))
    layer.subLayerPaths.append("./set.usda")
    spec = Sdf.CreatePrimInLayer(layer, "/World/house/chunks")
    spec.specifier = Sdf.SpecifierDef
    if arc == "reference":
        spec.referenceList.Prepend(Sdf.Reference("./house.usda", "/chunks"))
    elif arc == "payload":
        spec.payloadList.Prepend(Sdf.Payload("./house.usda", "/chunks"))
    else:
        # A class holding the network, inherited onto the house: the class itself is
        # abstract and never runs, the house that inherits it does.
        cls = Sdf.CreatePrimInLayer(layer, "/_house")
        cls.specifier = Sdf.SpecifierClass
        cls.referenceList.Prepend(Sdf.Reference("./house.usda", "/chunks"))
        spec.inheritPathList.Prepend(Sdf.Path("/_house"))
    for path in ("/World", "/World/house"):
        layer.GetPrimAtPath(path).specifier = Sdf.SpecifierDef
    layer.Save()
    return shot


@pytest.mark.parametrize("arc", ARCS)
def test_the_network_cooks_with_the_shots_own_chain(tmp_path, arc):
    z = run_and_read(_shot(tmp_path, arc), "abc")
    assert z == pytest.approx({"a": 1.0, "b": 2.0, "c": 3.0})


@pytest.mark.parametrize("arc", ARCS)
def test_the_network_loads_in_place_before_the_shots_chain(tmp_path, arc):
    scene = Scene.open(str(_shot(tmp_path, arc)), "pytest")
    try:
        stage = scene.get_stage()
        paths = {chunk_stage.get_chunk_prim_path(c) for c in scene.get_chunks()}
        assert H1 in paths
        assert not any(p.startswith(("/house", "/_house")) for p in paths), sorted(paths)
        assert not stage.GetRootLayer().GetPrimAtPath(H1), "nothing written for the house"
        s1 = stage.GetPrimAtPath("/chunks/ChunkGroup/S1")
        assert "/World/house/chunks/ChunkGroup/H2" in chunk_stage.implied_sources(s1)
    finally:
        scene.close()


def test_an_unloaded_payload_has_no_chunks_and_loading_it_brings_them(tmp_path):
    scene = Scene.open(str(_shot(tmp_path, "payload")), "pytest")
    try:
        stage = scene.get_stage()

        def house_chunks():
            return {chunk_stage.get_chunk_prim_path(c) for c in scene.get_chunks()
                    if chunk_stage.get_chunk_prim_path(c).startswith("/World/house")}

        assert H1 in house_chunks()
        stage.Unload("/World/house")
        assert house_chunks() == set(), "unloading the payload leaves its chunks behind"
        s1 = stage.GetPrimAtPath("/chunks/ChunkGroup/S1")
        assert chunk_stage.implied_sources(s1) == []
        stage.Load("/World/house")
        assert H1 in house_chunks(), "loading the payload again does not bring them back"
    finally:
        scene.close()


# -- variants ----------------------------------------------------------------------------------


def _variant_shot(tmp_path, selection: str, inline: bool = False):
    """shot.usda with a `version` variant set on /World/house/chunks.

    v1: house_v1 (H1 moves /a to 1). v2: house_v2 (H1 moves /a to 5, H3 moves /b to 7), so
    switching adds a chunk. inline=True puts v2's network in the variant itself instead of
    behind a reference: the chunk prims are defined inside the variant.
    """
    save_cubes(tmp_path / "set.usda", "abc")
    save_network(tmp_path / "house_v1.usda", {"H1": ("/a", 1.0)}, ends=("output",))
    save_network(tmp_path / "house_v2.usda", {"H1": ("/a", 5.0), "H3": ("/b", 7.0)}, ends=("output",))
    shot = tmp_path / "shot.usda"
    save_network(shot, {"S1": ("/c", 3.0)}, ends=("output",))
    layer = Sdf.Layer.FindOrOpen(str(shot))
    layer.subLayerPaths.append("./set.usda")
    house = Sdf.CreatePrimInLayer(layer, "/World/house/chunks")
    house.specifier = Sdf.SpecifierDef
    for path in ("/World", "/World/house"):
        layer.GetPrimAtPath(path).specifier = Sdf.SpecifierDef
    vset = Sdf.VariantSetSpec(house, "version")
    v1 = Sdf.VariantSpec(vset, "v1")
    v1.primSpec.referenceList.Prepend(Sdf.Reference("./house_v1.usda", "/chunks"))
    v2 = Sdf.VariantSpec(vset, "v2")
    if inline:
        v2_layer = Sdf.Layer.FindOrOpen(str(tmp_path / "house_v2.usda"))
        Sdf.CopySpec(v2_layer, "/chunks/ChunkGroup", layer,
                     "/World/house/chunks{version=v2}ChunkGroup")
    else:
        v2.primSpec.referenceList.Prepend(Sdf.Reference("./house_v2.usda", "/chunks"))
    house.variantSetNameList.Prepend("version")
    house.variantSelections["version"] = selection
    layer.Save()
    return shot


@pytest.mark.parametrize("selection, expected", [
    ("v1", {"a": 1.0, "b": 0.0, "c": 3.0}),
    ("v2", {"a": 5.0, "b": 7.0, "c": 3.0}),
])
def test_the_selected_variants_network_cooks(tmp_path, selection, expected):
    assert run_and_read(_variant_shot(tmp_path, selection), "abc") == pytest.approx(expected)


def test_a_network_defined_inside_a_variant_cooks(tmp_path):
    z = run_and_read(_variant_shot(tmp_path, "v2", inline=True), "abc")
    assert z == pytest.approx({"a": 5.0, "b": 7.0, "c": 3.0})


def test_switching_the_variant_live_swaps_the_network(tmp_path):
    scene = Scene.open(str(_variant_shot(tmp_path, "v1")), "pytest")
    try:
        stage = scene.get_stage()

        def house_chunks():
            return {chunk_stage.get_chunk_prim_path(c).rsplit("/", 1)[-1]
                    for c in scene.get_chunks()
                    if chunk_stage.get_chunk_prim_path(c).startswith("/World/house")}

        assert {"H1"} <= house_chunks() and "H3" not in house_chunks()
        stage.GetPrimAtPath("/World/house/chunks").GetVariantSet("version").SetVariantSelection("v2")
        assert "H3" in house_chunks(), "the chunk v2 adds did not load"
        h1 = next(c for c in scene.get_chunks()
                  if chunk_stage.get_chunk_prim_path(c) == "/World/house/chunks/ChunkGroup/H1")
        assert h1.get_param("tz").value == pytest.approx(5.0), "H1 kept v1's value"
        stage.GetPrimAtPath("/World/house/chunks").GetVariantSet("version").SetVariantSelection("v1")
        assert "H3" not in house_chunks(), "the chunk v2 added outlived its variant"
    finally:
        scene.close()


# -- an asset inside an asset ------------------------------------------------------------------


def _nested_shot(tmp_path):
    """shot.usda references house.usda's network; house.usda references chair.usda's network
    inside its own, at /chunks/furniture. The chair moves /c; the house /a; the shot /b."""
    save_cubes(tmp_path / "set.usda", "abc")
    save_network(tmp_path / "chair.usda", {"C1": ("/c", 4.0)}, ends=("output",))
    save_network(tmp_path / "house.usda", {"H1": ("/a", 1.0)}, ends=("output",))
    house = Sdf.Layer.FindOrOpen(str(tmp_path / "house.usda"))
    furniture = Sdf.CreatePrimInLayer(house, "/chunks/furniture")
    furniture.specifier = Sdf.SpecifierDef
    furniture.referenceList.Prepend(Sdf.Reference("./chair.usda", "/chunks"))
    house.Save()
    shot = tmp_path / "shot.usda"
    save_network(shot, {"S1": ("/b", 2.0)}, ends=("output",))
    layer = Sdf.Layer.FindOrOpen(str(shot))
    layer.subLayerPaths.append("./set.usda")
    spec = Sdf.CreatePrimInLayer(layer, "/World/house/chunks")
    spec.specifier = Sdf.SpecifierDef
    spec.referenceList.Prepend(Sdf.Reference("./house.usda", "/chunks"))
    for path in ("/World", "/World/house"):
        layer.GetPrimAtPath(path).specifier = Sdf.SpecifierDef
    layer.Save()
    return shot


def test_an_asset_inside_an_asset_cooks(tmp_path):
    z = run_and_read(_nested_shot(tmp_path), "abc")
    assert z == pytest.approx({"a": 1.0, "b": 2.0, "c": 4.0})


def test_an_asset_inside_an_asset_loads_in_place(tmp_path):
    scene = Scene.open(str(_nested_shot(tmp_path)), "pytest")
    try:
        paths = {chunk_stage.get_chunk_prim_path(c) for c in scene.get_chunks()}
        assert "/World/house/chunks/ChunkGroup/H1" in paths
        assert "/World/house/chunks/furniture/ChunkGroup/C1" in paths, sorted(paths)
        assert not scene.get_stage().GetRootLayer().GetPrimAtPath("/World/house/chunks/ChunkGroup/H1")
    finally:
        scene.close()
