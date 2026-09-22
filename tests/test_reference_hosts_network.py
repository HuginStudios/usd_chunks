"""A Reference, Sublayer or Payload chunk pointed at a saved chunk network becomes its node.

Point any of them at a file whose target prim is a chunkgroup and it does not put that prim
into the scene: it puts a reference arc on its own chunk prim, so the group's chunks
compose in as its children and the node is the group -- dive in and the network is there,
as if it had been expanded in place, exactly as a chunkgroup is. Edits inside are
overrides in the current file (see test_override_through_composition); the network file
is never written to. Pointed at scene content, both are unchanged.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from graph_fixtures import _Scene, make_network
from usd_chunks import chunk_registry
from usd_chunks.chunk_stage import (
    ChunkStageSync, export_chunks, get_chunk_prim_path, initialize, is_group_chunk,
    is_network_host, is_subnet,
)
from usd_chunks.plugins import chunk_graph

_TX = "inputs:translate_grp:tx"


def _add(scene, module_name):
    module = chunk_registry.ChunkRegistry().get_chunk_module(module_name)
    assert module is not None, f"chunk module {module_name!r} not found"
    chunk = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(chunk)
    return chunk


def _open(path: str | None):
    stage = Usd.Stage.Open(path) if path else Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    initialize(stage, scene)
    return stage, scene


def _teardown(scene):
    for chunk in list(scene.get_chunks()):
        chunk.request_delete()


@pytest.fixture
def network_file(tmp_path):
    seq, _ = make_network(tmp_path / "seq.usda")
    return seq


@pytest.fixture
def scene_file(tmp_path):
    path = str(tmp_path / "asset.usda")
    stage = Usd.Stage.CreateNew(path)
    stage.DefinePrim("/Asset", "Xform")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/Asset"))
    stage.Save()
    return path


@pytest.fixture
def hosting(network_file):
    stage, scene = _open(None)
    reference = _add(scene, "reference")
    reference.get_param("file_path").value = network_file
    # A chunk not reachable from any view is blocked and does not cook on a param change;
    # this is the cook the view would have given it.
    reference.execute()
    try:
        yield stage, scene, reference, network_file
    finally:
        _teardown(scene)


def _children(scene, host):
    """Names of the chunks directly under host's prim -- by prim path, since a nested
    group answers get_context() with itself."""
    root = get_chunk_prim_path(host)
    return sorted(c.name for c in scene.get_chunks()
                  if c is not host and get_chunk_prim_path(c).rsplit("/", 1)[0] == root)


class TestPointingAtANetwork:
    def test_the_node_is_a_subnet(self, hosting):
        _, scene, reference, _ = hosting
        assert is_network_host(reference)
        assert is_subnet(reference)
        assert is_group_chunk(reference)
        chunks = {get_chunk_prim_path(c): c for c in scene.get_chunks()}
        assert chunk_graph.can_dive(chunks, get_chunk_prim_path(reference))

    def test_the_networks_chunks_are_its_children(self, hosting):
        _, scene, reference, _ = hosting
        assert _children(scene, reference) == ["Transform", "Transform1"]

    def test_the_children_sit_under_its_prim(self, hosting):
        stage, scene, reference, _ = hosting
        root = get_chunk_prim_path(reference)
        for child in scene.get_chunks():
            if child is reference or child.get_context() is not reference:
                continue
            assert get_chunk_prim_path(child) == f"{root}/{child.name}"
            assert stage.GetPrimAtPath(get_chunk_prim_path(child)).GetTypeName() == "Chunk"

    def test_no_context_is_made_for_the_file(self, hosting):
        """The loader used to give every source layer its own top-level context -- a tab
        named after the file. A hosted network nests; nothing else appears."""
        _, scene, reference, _ = hosting
        contexts = [c for c in scene.get_contexts() if c is not reference]
        assert all("seq" not in (c.name or "") for c in contexts)

    def test_the_arc_is_on_the_chunks_own_prim(self, hosting):
        stage, _, reference, network_file = hosting
        prim = stage.GetPrimAtPath(get_chunk_prim_path(reference))
        assert prim.HasAuthoredReferences()
        assert stage.GetPrimAtPath("/seq") is None or not stage.GetPrimAtPath("/seq").IsValid(), \
            "a network is hosted, not placed at primpath_location"

    def test_the_network_file_is_untouched(self, hosting):
        _, _, _, network_file = hosting
        assert not Sdf.Layer.Find(network_file).dirty


class TestPointingAtSceneContent:
    def test_a_plain_reference_stays_plain(self, scene_file):
        stage, scene = _open(None)
        try:
            reference = _add(scene, "reference")
            reference.get_param("file_path").value = scene_file
            reference.execute()
            assert not is_network_host(reference)
            assert not is_subnet(reference)
            assert not is_group_chunk(reference)
            location = reference.get_param("primpath_location").value
            assert stage.GetPrimAtPath(location).IsValid()
            assert not stage.GetPrimAtPath(get_chunk_prim_path(reference)).HasAuthoredReferences()
        finally:
            _teardown(scene)

    def test_retargeting_from_a_network_to_content_unhosts(self, hosting, scene_file):
        stage, scene, reference, _ = hosting
        reference.get_param("file_path").value = scene_file
        reference.execute()
        assert not is_network_host(reference)
        assert not stage.GetPrimAtPath(get_chunk_prim_path(reference)).HasAuthoredReferences()
        assert _children(scene, reference) == []


class TestEditingAndSaving:
    def test_an_inner_edit_is_an_override_in_the_shot(self, hosting):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 2.5
        path = f"{get_chunk_prim_path(transform)}.{_TX}"
        assert stage.GetAttributeAtPath(path).Get() == 2.5
        assert stage.GetRootLayer().GetAttributeAtPath(path).default == 2.5
        assert not Sdf.Layer.Find(network_file).dirty

    def test_the_saved_file_holds_the_arc_and_the_override_only(self, hosting, tmp_path):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 2.5
        shot = str(tmp_path / "shot.usda")
        export_chunks(scene, shot)
        saved = Sdf.Layer.FindOrOpen(shot)
        host = saved.GetPrimAtPath(get_chunk_prim_path(reference))
        assert host is not None and host.referenceList.GetAddedOrExplicitItems()
        child = saved.GetPrimAtPath(get_chunk_prim_path(transform))
        assert child is not None and child.specifier == Sdf.SpecifierOver
        assert saved.GetAttributeAtPath(f"{get_chunk_prim_path(transform)}.{_TX}").default == 2.5
        assert saved.GetPrimAtPath(f"{get_chunk_prim_path(reference)}/Transform1") is None

    def test_reopening_hosts_the_network_again_without_a_cook(self, hosting, tmp_path):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 2.5
        shot = str(tmp_path / "shot.usda")
        export_chunks(scene, shot)
        host_path = get_chunk_prim_path(reference)
        _teardown(scene)

        stage2, scene2 = _open(shot)
        try:
            host = next(c for c in scene2.get_chunks() if get_chunk_prim_path(c) == host_path)
            assert is_network_host(host)
            assert _children(scene2, host) == ["Transform", "Transform1"]
            assert stage2.GetAttributeAtPath(f"{host_path}/Transform.{_TX}").Get() == 2.5
            contexts = [c for c in scene2.get_contexts() if c is not host]
            assert all("seq" not in (c.name or "") for c in contexts)
        finally:
            _teardown(scene2)


def _sublayer_of(scene, *paths):
    sublayer = _add(scene, "sublayer")
    imports = sublayer.get_param("imports")
    imports.size = len(paths)
    for index, path in enumerate(paths):
        imports.get_indexed_param("sublayer_path", index).value = path
    sublayer.execute()
    return sublayer


class TestSublayerHostsToo:
    def test_a_network_file_is_hosted_not_sublayered(self, network_file):
        stage, scene = _open(None)
        try:
            sublayer = _sublayer_of(scene, network_file)
            assert is_network_host(sublayer)
            assert is_subnet(sublayer)
            assert _children(scene, sublayer) == ["Transform", "Transform1"]
            assert network_file not in [
                Sdf.Layer.Find(p).identifier if Sdf.Layer.Find(p) else p
                for p in sublayer.get_edit_context().subLayerPaths
            ]
            assert stage.GetPrimAtPath("/chunks/ChunkGroup/Transform").IsValid() is False or \
                get_chunk_prim_path(sublayer) == "/chunks/ChunkGroup", \
                "the network must nest under the node, not land at the file's own paths"
        finally:
            _teardown(scene)

    def test_a_scene_file_is_still_sublayered(self, scene_file):
        stage, scene = _open(None)
        try:
            sublayer = _sublayer_of(scene, scene_file)
            assert not is_network_host(sublayer)
            assert stage.GetPrimAtPath("/Asset").IsValid()
        finally:
            _teardown(scene)

    def test_two_network_files_stack_under_one_node_first_strongest(self, network_file, tmp_path):
        other, _ = make_network(tmp_path / "other" / "seq.usda")
        for path, value in ((network_file, 1.0), (other, 7.0)):
            saved = Usd.Stage.Open(path)
            saved.GetPrimAtPath("/chunks/ChunkGroup/Transform").CreateAttribute(
                _TX, Sdf.ValueTypeNames.Float).Set(value)
            saved.GetRootLayer().Save()
        stage, scene = _open(None)
        try:
            sublayer = _sublayer_of(scene, network_file, other)
            assert _children(scene, sublayer) == ["Transform", "Transform1"]
            transform = next(c for c in scene.get_chunks() if c.name == "Transform")
            assert stage.GetAttributeAtPath(f"{get_chunk_prim_path(transform)}.{_TX}").Get() == 1.0
        finally:
            _teardown(scene)


def _nest_a_group_and_a_sublayer(tmp_path, seq: str) -> str:
    """Give seq.usda a sublayer, part.usda, that contributes a loose chunk (Transform2)
    and a nested chunkgroup (Part, holding two chunks) to the same network."""
    part, _ = make_network(tmp_path / "part.usda")
    layer = Sdf.Layer.FindOrOpen(part)
    group = layer.GetPrimAtPath("/chunks/ChunkGroup")
    nested = Sdf.CreatePrimInLayer(layer, "/chunks/ChunkGroup/Part")
    nested.specifier = Sdf.SpecifierDef
    nested.typeName = "Chunk"
    Sdf.AttributeSpec(nested, "info:chunkType", Sdf.ValueTypeNames.Token).default = "chunkgroup"
    for name in ("Transform", "Transform1"):
        Sdf.CopySpec(layer, f"/chunks/ChunkGroup/{name}", layer, f"/chunks/ChunkGroup/Part/{name}")
        del group.nameChildren[name]
    Sdf.CopySpec(layer, "/chunks/ChunkGroup/Part/Transform", layer, "/chunks/ChunkGroup/Transform2")
    layer.Save()
    seq_layer = Sdf.Layer.FindOrOpen(seq)
    seq_layer.subLayerPaths.append("./part.usda")
    seq_layer.Save()
    return part


class TestWhatTheFileComposesIsWhatYouGet:
    def test_the_files_own_sublayers_and_nested_groups_come_along(self, network_file, tmp_path):
        """seq.usda sublayers part.usda. Diving into the host shows everything the file
        composes: its own chunks, the loose chunk its sublayer adds, and the nested group
        its sublayer adds -- itself a diveable chunkgroup with its own chunks."""
        _nest_a_group_and_a_sublayer(tmp_path, network_file)
        stage, scene = _open(None)
        try:
            host = _sublayer_of(scene, network_file)
            assert _children(scene, host) == ["Part", "Transform", "Transform1", "Transform2"]
            part = next(c for c in scene.get_chunks() if c.name == "Part")
            assert is_subnet(part)
            assert _children(scene, part) == ["Transform", "Transform1"]
            assert get_chunk_prim_path(part) == f"{get_chunk_prim_path(host)}/Part"
        finally:
            _teardown(scene)

    def test_a_wrapper_file_whose_network_is_all_in_its_sublayers_is_a_network(self, network_file, tmp_path):
        """A sequence file that is nothing but a subLayers list still hosts."""
        wrapper = str(tmp_path / "wrapper.usda")
        layer = Sdf.Layer.CreateNew(wrapper)
        layer.subLayerPaths.append("./seq.usda")
        layer.Save()
        stage, scene = _open(None)
        try:
            host = _sublayer_of(scene, wrapper)
            assert is_network_host(host)
            assert _children(scene, host) == ["Transform", "Transform1"]
        finally:
            _teardown(scene)


class TestPayloadHostsToo:
    def test_a_network_file_is_hosted_as_a_payload(self, network_file):
        stage, scene = _open(None)
        try:
            payload = _add(scene, "payload")
            payload.get_param("file_path").value = network_file
            payload.execute()
            assert is_network_host(payload)
            assert is_subnet(payload)
            assert _children(scene, payload) == ["Transform", "Transform1"]
            prim = stage.GetPrimAtPath(get_chunk_prim_path(payload))
            assert prim.HasAuthoredPayloads(), "a Payload chunk hosts with a payload arc"
            assert not prim.HasAuthoredReferences()
        finally:
            _teardown(scene)

    def test_the_saved_file_reopens_as_a_payload_host(self, network_file, tmp_path):
        stage, scene = _open(None)
        payload = _add(scene, "payload")
        payload.get_param("file_path").value = network_file
        payload.execute()
        host_path = get_chunk_prim_path(payload)
        shot = str(tmp_path / "shot.usda")
        export_chunks(scene, shot)
        _teardown(scene)
        assert Sdf.Layer.FindOrOpen(shot).GetPrimAtPath(host_path).payloadList.GetAddedOrExplicitItems()

        stage2, scene2 = _open(shot)
        try:
            host = next(c for c in scene2.get_chunks() if get_chunk_prim_path(c) == host_path)
            assert is_network_host(host)
            assert _children(scene2, host) == ["Transform", "Transform1"]
        finally:
            _teardown(scene2)

    def test_scene_content_is_still_a_plain_payload(self, scene_file):
        stage, scene = _open(None)
        try:
            payload = _add(scene, "payload")
            payload.get_param("file_path").value = scene_file
            payload.execute()
            assert not is_network_host(payload)
            location = payload.get_param("primpath_location").value
            assert stage.GetPrimAtPath(location).HasAuthoredPayloads()
        finally:
            _teardown(scene)
