"""A network builds an asset whose files are joined by payload and reference, not sublayers.

Kitchen_set's Bottle: Bottle.usd payloads Bottle_payload.usd, which references
Bottle.geom.usd. A Layer chunk with Sublayer off makes such a layer; a Payload or Reference
chunk brings it in by its Layer menu; an Edit Target downstream of that authors into it
through the arc; Export USD writes each as its own file with the arcs pointing at them.
"""

from __future__ import annotations

import os

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk_registry, chunk_utils
from usd_chunks.chunk_stage import get_chunk_prim_path


def _bottle(scene):
    """Bottle -> payload Bottle_payload -> reference Bottle.geom -> a cube at /Bottle/geo."""
    asset = scene.create.layer(layer_name="Bottle")
    payload = scene.create.layer(asset, layer_name="Bottle_payload", sublayer=False, default_prim="Bottle")
    geom = scene.create.layer(payload, layer_name="Bottle.geom", sublayer=False, default_prim="Bottle")
    with scene.create.edit_target(geom, edit_target="Bottle") as target:
        arc = scene.create.payload(target, layer="Bottle_payload", primpath_location="/Bottle", name="payload")
    with scene.create.edit_target(arc, edit_target="Bottle_payload") as target:
        ref = scene.create.reference(target, layer="Bottle.geom.usda", primpath_location="/Bottle", name="geom_ref")
    with scene.create.edit_target(ref, edit_target="Bottle.geom.usda") as target:
        mesh = scene.create.cube(target, prim_path="/Bottle/geo", name="bottle_mesh")
    return mesh


def _arcs(prim):
    """The arc types under prim's root node, and under each of those."""
    root = prim.GetPrimIndex().rootNode
    top = [str(n.arcType).split(".")[-1] for n in root.children]
    nested = [str(n.arcType).split(".")[-1] for c in root.children for n in c.children]
    return top, nested


def _export(scene, after, out):
    scene.create.export_usd(after, output_dir=str(out), bake_all=True, keep_session_layer=False)
    scene.commit()


def test_the_asset_composes_through_its_arcs() -> None:
    with Scene() as scene:
        mesh = _bottle(scene)
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"
        assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], ["ArcTypeReference"]), (
            "the reference is inside the payload, not beside it"
        )
        lm = chunk_registry.ChunkLayerManager()
        assert lm.get_chunk_target(mesh.get_hash()).GetDisplayName() == "Bottle.geom.usda"
        # The arc layers are in no layer stack -- they come in by arc alone.
        stack = {layer.identifier for layer in stage.GetLayerStack()}
        assert all(layer.identifier not in stack for _name, layer in lm.arc_layers())


def test_chunks_behind_an_arc_keep_their_prims_on_the_stage() -> None:
    """/chunks is outside every asset's arc: bookkeeping routed into an arc layer vanished."""
    with Scene() as scene:
        _bottle(scene)
        stage = scene.get_stage()
        for c in scene.get_chunks():
            assert stage.GetPrimAtPath(get_chunk_prim_path(c)).IsValid(), c.name


def test_export_writes_the_kitchen_set_layout(tmp_path) -> None:
    with Scene() as scene:
        _export(scene, _bottle(scene), tmp_path)

    asset = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle.usda"))
    payload = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle_payload.usda"))
    geom = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle.geom.usda"))
    assert [p.assetPath for p in asset.GetPrimAtPath("/Bottle").payloadList.GetAddedOrExplicitItems()] == ["Bottle_payload.usda"]
    assert [r.assetPath for r in payload.GetPrimAtPath("/Bottle").referenceList.GetAddedOrExplicitItems()] == ["Bottle.geom.usda"]
    assert geom.GetPrimAtPath("/Bottle/geo").typeName == "Mesh"
    for layer in (asset, payload, geom):
        assert layer.defaultPrim == "Bottle"
        assert "anon:" not in layer.ExportToString()
        assert not layer.subLayerPaths

    stage = Usd.Stage.Open(str(tmp_path / "Bottle.usda"))
    assert len(UsdGeom.Mesh(stage.GetPrimAtPath("/Bottle/geo")).GetPointsAttr().Get()) == 8
    assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], ["ArcTypeReference"])


def test_the_network_saves_and_reopens(tmp_path) -> None:
    with Scene() as scene:
        _bottle(scene)
        scene.save(str(tmp_path / "network.usda"))

    with Scene.open(str(tmp_path / "network.usda")) as scene:
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"
        assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], ["ArcTypeReference"])
        mesh = next(c for c in scene.get_chunks() if c.name == "bottle_mesh")
        _export(scene, mesh, tmp_path / "out")
    assert Sdf.Layer.FindOrOpen(str(tmp_path / "out" / "Bottle.geom.usda")).GetPrimAtPath("/Bottle/geo")


def test_a_dotted_name_is_tagged_so_an_arc_can_open_it() -> None:
    with Scene() as scene:
        scene.create.layer(layer_name="Bottle.geom", sublayer=False, default_prim="Bottle")
        names = [name for name, _layer in chunk_registry.ChunkLayerManager().arc_layers()]
        assert names == ["Bottle.geom.usda"]


def test_sublayer_toggles_the_layer_in_and_out_of_the_stack() -> None:
    with Scene() as scene:
        layer_chunk = scene.create.layer(layer_name="dept")
        stage = scene.get_stage()
        named = lambda: [name for name, _l in chunk_utils.user_layers(stage)]  # noqa: E731
        assert "dept" in named()
        layer_chunk.get_param("sublayer").value = False
        assert "dept" not in named()
        assert [n for n, _l in chunk_registry.ChunkLayerManager().arc_layers()] == ["dept"]
        layer_chunk.get_param("sublayer").value = True
        assert "dept" in named()
        assert not chunk_registry.ChunkLayerManager().arc_layers()


def test_a_local_layer_is_handed_back_unchanged() -> None:
    """Every layer before this feature is local, and must be authored exactly as before."""
    stage = Usd.Stage.CreateInMemory()
    assert chunk_utils.edit_target(stage, stage.GetRootLayer()) is stage.GetRootLayer()
    assert chunk_utils.edit_target(stage, None) is None
    stray = Sdf.Layer.CreateAnonymous("stray")
    assert chunk_utils.edit_target(stage, stray) is stray, "nothing reaches it: unchanged, fails as before"
    assert not chunk_utils.is_editable(stage, stray)


def test_layers_of_a_new_scene_still_export_as_before(tmp_path) -> None:
    """No arc layer: the per-layer export writes what it always did."""
    with Scene() as scene:
        layer = scene.create.layer(layer_name="model")
        with scene.create.edit_target(layer, edit_target="model") as target:
            box = scene.create.cube(target, prim_path="/world/box", name="box")
        _export(scene, box, tmp_path)
    assert sorted(os.listdir(tmp_path)) == ["model.usda", "tmp.usda"]
    assert Sdf.Layer.FindOrOpen(str(tmp_path / "model.usda")).GetPrimAtPath("/world/box")
