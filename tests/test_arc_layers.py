"""A network builds an asset whose files are joined by payload and reference, not sublayers.

Kitchen_set's Bottle: Bottle.usd payloads Bottle_payload.usd, which references
Bottle.geom.usd. A Layer chunk with Sublayer off makes such a layer; a Payload or Reference
chunk brings it in by its Layer menu; an Edit Target downstream of that authors into it
through the arc; Export USD writes each as its own file with the arcs pointing at them.
"""

from __future__ import annotations

import os

import pytest

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_stage import get_chunk_prim_path


def _bottle(scene):
    """Bottle -> payload Bottle_payload -> reference Bottle.geom -> a cube at /Bottle/geo."""
    asset = scene.create.layer(layer_name="Bottle")
    payload = scene.create.layer(asset, layer_name="Bottle_payload", sublayer=False, default_prim="Bottle")
    geom = scene.create.layer(payload, layer_name="Bottle.geom", sublayer=False, default_prim="Bottle")
    with scene.create.edit_target(geom, edit_target="Bottle") as target:
        arc = scene.create.payload(target, from_layer=True, layer="Bottle_payload", primpath_location="/Bottle", name="payload")
    with scene.create.edit_target(arc, edit_target="Bottle_payload") as target:
        ref = scene.create.reference(target, from_layer=True, layer="Bottle.geom.usda", primpath_location="/Bottle", name="geom_ref")
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
        # Kept though the Layer menus list nothing until they are opened.
        layers = {c.name: c.get_param("layer").value for c in scene.get_chunks() if c.name in ("payload", "geom_ref")}
        assert layers == {"payload": "Bottle_payload", "geom_ref": "Bottle.geom.usda"}
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


def test_flipping_a_referenced_layer_to_a_sublayer_and_back() -> None:
    """Back on, the Reference has nothing to bring in and the mesh composes as a sublayer;
    off again, the asset is as it was. Off again once crashed: a masked cook ran on a second
    stage of the same layers, which heard an edit before the change that caused it."""
    with Scene() as scene:
        mesh = _bottle(scene)
        stage = scene.get_stage()
        geom = next(c for c in scene.get_chunks() if c.get_param("layer_name") is not None
                    and c.get_param("layer_name").value == "Bottle.geom")
        geom.get_param("sublayer").value = True
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"
        assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], [])
        geom.get_param("sublayer").value = False
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"
        assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], ["ArcTypeReference"])
        lm = chunk_registry.ChunkLayerManager()
        assert lm.get_chunk_target(mesh.get_hash()).GetDisplayName() == "Bottle.geom.usda"


def test_a_disabled_chunk_behind_an_arc_drops_out_and_comes_back() -> None:
    """Its cook stays in its own layer under the arc layer until export, like any chunk's."""


    with Scene() as scene:
        mesh = _bottle(scene)
        stage = scene.get_stage()
        mesh.set_state(chunk.ChunkFlags.State.DISABLED)
        assert not stage.GetPrimAtPath("/Bottle/geo")
        mesh.unset_state(chunk.ChunkFlags.State.DISABLED)
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"


def test_component_kind_and_variant_set_land_on_the_asset_file(tmp_path) -> None:
    """As Kitchen_set's Bottle.usd: kind and modelingVariant beside the payload, not inside it."""
    with Scene() as scene:
        asset = scene.create.layer(layer_name="Bottle")
        payload = scene.create.layer(asset, layer_name="Bottle_payload", sublayer=False, default_prim="Bottle")
        geom = scene.create.layer(payload, layer_name="Bottle.geom", sublayer=False, default_prim="Bottle")
        with scene.create.edit_target(geom, edit_target="Bottle") as target:
            arc = scene.create.payload(target, from_layer=True, layer="Bottle_payload", primpath_location="/Bottle", name="payload")
            kind = scene.create.set_metadata(arc, prim_path="/Bottle", metadata_key="kind",
                                             metadata_value="component", name="kind")
            variants = scene.create.variant(kind, primpath_location="/Bottle", name="variants")
            entries = variants.get_param("variant_entries")
            for name, value in (("vs_name", "modelingVariant"), ("v_names", "Short,Tall"), ("v_default", "Tall")):
                entries.get_indexed_param(entries.get_param(name), 0).value = value
        with scene.create.edit_target(variants, edit_target="Bottle_payload") as target:
            ref = scene.create.reference(target, from_layer=True, layer="Bottle.geom.usda", primpath_location="/Bottle", name="geom_ref")
        with scene.create.edit_target(ref, edit_target="Bottle.geom.usda") as target:
            mesh = scene.create.cube(target, prim_path="/Bottle/Geom/Bottle", name="bottle_mesh")
        _export(scene, mesh, tmp_path)

    asset_layer = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle.usda"))
    payload_layer = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle_payload.usda"))
    asset_spec = asset_layer.GetPrimAtPath("/Bottle")
    assert asset_spec.kind == "component"
    assert list(asset_spec.variantSets.keys()) == ["modelingVariant"]
    assert asset_spec.variantSelections["modelingVariant"] == "Tall"
    payload_spec = payload_layer.GetPrimAtPath("/Bottle")
    assert not payload_spec.kind and not payload_spec.variantSets

    stage = Usd.Stage.Open(str(tmp_path / "Bottle.usda"))
    prim = stage.GetPrimAtPath("/Bottle")
    assert Usd.ModelAPI(prim).GetKind() == "component"
    assert prim.GetVariantSets().GetVariantSet("modelingVariant").GetVariantNames() == ["Short", "Tall"]
    assert stage.GetPrimAtPath("/Bottle/Geom/Bottle").GetTypeName() == "Mesh"


def test_existing_layer_shows_the_layer_menu_in_place_of_file_path(tmp_path) -> None:
    hidden = lambda c, name: c.get_param(name).has_state(chunk.ChunkFlags.State.HIDDEN)  # noqa: E731
    with Scene() as scene:
        ref = scene.create.reference(name="ref")
        assert not hidden(ref, "file_path") and hidden(ref, "layer")
        ref.get_param("from_layer").value = True
        assert hidden(ref, "file_path") and not hidden(ref, "layer")
        _bottle(scene)
        scene.save(str(tmp_path / "network.usda"))

    with Scene.open(str(tmp_path / "network.usda")) as scene:
        for c in scene.get_chunks():
            if c.name in ("ref", "payload", "geom_ref"):
                assert hidden(c, "file_path") and not hidden(c, "layer"), c.name


def _wired_bottle(scene, driven: bool = False):
    """The Bottle with its arc layers wired in: the Layer chunks off the chain, each Layer Name
    wired into the Payload's or Reference's Layer."""
    scene.create.layer(layer_name="Bottle_payload", sublayer=False, default_prim="Bottle", name="pay_layer")
    scene.create.layer(layer_name="Bottle.geom", sublayer=False, default_prim="Bottle", name="geom_layer")
    asset = scene.create.layer(layer_name="Bottle")
    with scene.create.edit_target(asset, edit_target="Bottle") as target:
        arc = scene.create.payload(target, from_layer=True, layer="@pay_layer.layer_name@",
                                   primpath_location="/Bottle", name="payload")
    with scene.create.edit_target(arc, edit_target="Bottle_payload") as target:
        ref = scene.create.reference(target, from_layer=True, layer="@geom_layer.layer_name@",
                                     primpath_location="/Bottle", name="geom_ref")
    with scene.create.edit_target(ref, edit_target="Bottle.geom.usda") as target:
        mesh = scene.create.cube(target, prim_path="/Bottle/geo", name="bottle_mesh")
    if driven:
        scene.set_output(mesh)
    return mesh


@pytest.mark.parametrize("driven", [False, True], ids=["headless", "output"])
def test_a_layer_name_wired_into_the_arc_brings_that_layer_in(tmp_path, driven) -> None:
    """Bottle.geom wired in names the layer listed as Bottle.geom.usda: either is its name."""
    with Scene() as scene:
        _wired_bottle(scene, driven)
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"
        assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], ["ArcTypeReference"])
        scene.save(str(tmp_path / "network.usda"))
    with Scene.open(str(tmp_path / "network.usda")) as scene:
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"
        assert _arcs(stage.GetPrimAtPath("/Bottle")) == (["ArcTypePayload"], ["ArcTypeReference"])


def test_a_wired_bottle_exports_the_kitchen_set_layout(tmp_path) -> None:
    with Scene() as scene:
        _export(scene, _wired_bottle(scene, driven=True), tmp_path)
    asset = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle.usda"))
    payload = Sdf.Layer.FindOrOpen(str(tmp_path / "Bottle_payload.usda"))
    assert [p.assetPath for p in asset.GetPrimAtPath("/Bottle").payloadList.GetAddedOrExplicitItems()] == ["Bottle_payload.usda"]
    assert [r.assetPath for r in payload.GetPrimAtPath("/Bottle").referenceList.GetAddedOrExplicitItems()] == ["Bottle.geom.usda"]
    stage = Usd.Stage.Open(str(tmp_path / "Bottle.usda"))
    assert stage.GetPrimAtPath("/Bottle/geo").GetTypeName() == "Mesh"


def test_the_layer_menu_lists_the_arc_layers_as_it_opens() -> None:
    """Filled when a frontend opens it (Menu.refresh_items), not kept current on every change."""
    with Scene() as scene:
        _bottle(scene)
        for c in scene.get_chunks():
            if c.name in ("payload", "geom_ref"):
                menu = c.get_param("layer")
                menu.refresh_items()
                assert {"Bottle_payload", "Bottle.geom.usda"} <= set(menu.menu_items), c.name


def test_references_and_payloads_do_not_each_hear_every_stage_change() -> None:
    """Each one listened to every USD change to keep its Layer menu current: on a network with
    106 References that was 397k calls, 12 s of opening it."""
    from usd_chunks import chunk_notice

    def listeners():
        dispatch = chunk_notice._global_dispatches.get(chunk.ChunkNoticeAlias.STAGE_CONTENTS_CHANGED)
        return len(dispatch.subscribers) if dispatch else 0

    with Scene() as scene:
        before = listeners()
        for i in range(3):
            scene.create.reference(name=f"ref{i}")
            scene.create.payload(name=f"pay{i}")
        assert listeners() == before


def test_the_layer_menu_opens_on_an_arc_layer_renamed_sublayered_or_deleted() -> None:
    """None of these is a change to the stage: the arc layer is in no layer stack. A menu
    kept current by stage changes went on listing the old name; one filled as it opens can't."""
    with Scene() as scene:
        ref = scene.create.reference(name="ref", from_layer=True)
        menu = ref.get_param("layer")

        def items():
            menu.refresh_items()
            return list(menu.menu_items)

        dept = scene.create.layer(layer_name="dept", sublayer=False, name="dept")
        assert items() == ["dept"]
        dept.get_param("layer_name").value = "look"
        assert items() == ["look"]
        dept.get_param("sublayer").value = True
        assert items() == []
        dept.get_param("sublayer").value = False
        assert items() == ["look"]
        dept.request_delete()
        assert items() == []
