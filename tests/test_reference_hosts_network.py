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
from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.chunk_stage import (
    param_override_source,
    export_chunks, get_chunk_prim_path, initialize, is_group_chunk,
    is_network_host, is_subnet,
    is_chunkgroup, localise_network, publish_network, loft_param_to_context,
)
from usd_chunks.graph import engine as chunk_graph

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
    reference.recook()
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
            reference.recook()
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
        reference.recook()
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
    sublayer.recook()
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
            payload.recook()
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
        payload.recook()
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
            payload.recook()
            assert not is_network_host(payload)
            location = payload.get_param("primpath_location").value
            assert stage.GetPrimAtPath(location).HasAuthoredPayloads()
        finally:
            _teardown(scene)



class TestLocalise:
    """Localise = convert the host into a chunkgroup of this file, its network now local."""

    def test_the_host_becomes_a_chunkgroup_with_the_same_chunks(self, hosting):
        stage, scene, reference, network_file = hosting
        host_path = get_chunk_prim_path(reference)
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 2.5

        group = localise_network(reference)

        assert group is not None and is_chunkgroup(group)
        assert get_chunk_prim_path(group) == host_path
        assert not is_network_host(group)
        assert _children(scene, group) == ["Transform", "Transform1"]
        assert not stage.GetPrimAtPath(host_path).HasAuthoredReferences()

    def test_the_override_survives_as_the_local_value(self, hosting):
        stage, scene, reference, network_file = hosting
        host_path = get_chunk_prim_path(reference)
        next(c for c in scene.get_chunks() if c.name == "Transform").get_param("tx").value = 2.5
        localise_network(reference)
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        assert transform.get_param("tx").value == 2.5
        attr = stage.GetAttributeAtPath(f"{host_path}/Transform.{_TX}")
        assert all(not chunk_utils.is_foreign_layer(s.layer, stage)
                   for s in attr.GetPropertyStack(Usd.TimeCode.Default()))

    def test_the_file_is_no_longer_needed(self, hosting, tmp_path):
        stage, scene, reference, network_file = hosting
        host_path = get_chunk_prim_path(reference)
        localise_network(reference)
        shot = str(tmp_path / "shot.usda")
        export_chunks(scene, shot)
        saved = Sdf.Layer.FindOrOpen(shot)
        assert "seq.usda" not in saved.ExportToString()
        assert saved.GetPrimAtPath(f"{host_path}/Transform").specifier == Sdf.SpecifierDef
        assert saved.GetAttributeAtPath(f"{host_path}.info:chunkType").default == "chunkgroup"


class TestPublish:
    """Publish = write a group out and make it the host of that file: the inverse."""

    def _group_with_chunks(self, scene):
        a = _add(scene, "transform")
        b = _add(scene, "transform")
        b.add_dependency(a)
        a.get_param("tx").value = 4.0
        return a, b

    def test_the_file_holds_the_group_at_chunks_name(self, tmp_path):
        stage, scene = _open(None)
        try:
            a, b = self._group_with_chunks(scene)
            group = a.get_context()
            out = str(tmp_path / "room.usda")
            publish_network(group, out)
            saved = Sdf.Layer.FindOrOpen(out)
            root = f"/chunks/{group.name}"
            assert saved.GetAttributeAtPath(f"{root}.info:chunkType").default == "chunkgroup"
            assert saved.GetPrimAtPath(f"{root}/{a.name}") is not None
            assert saved.GetPrimAtPath(f"{root}/{b.name}") is not None
            wire = saved.GetAttributeAtPath(f"{root}/{b.name}.inputs:chunk")
            assert [str(p) for p in wire.connectionPathList.GetAddedOrExplicitItems()] == [
                f"{root}/{a.name}.outputs:chunk"
            ]
            assert saved.GetAttributeAtPath(f"{root}/{a.name}.{_TX}").default == 4.0
        finally:
            _teardown(scene)

    def test_the_group_now_composes_from_the_file(self, tmp_path):
        stage, scene = _open(None)
        try:
            a, b = self._group_with_chunks(scene)
            group = a.get_context()
            group_path = get_chunk_prim_path(group)
            out = str(tmp_path / "room.usda")
            publish_network(group, out)
            prim = stage.GetPrimAtPath(group_path)
            assert prim.HasAuthoredReferences()
            assert chunk_utils.is_composed_in(stage.GetPrimAtPath(get_chunk_prim_path(a)))
            assert a.get_param("tx").value == 4.0
            assert a in scene.get_chunks() and b in scene.get_chunks()
        finally:
            _teardown(scene)

    def test_an_edit_after_publishing_is_an_override(self, tmp_path):
        stage, scene = _open(None)
        try:
            a, b = self._group_with_chunks(scene)
            out = str(tmp_path / "room.usda")
            publish_network(a.get_context(), out)
            a.get_param("tx").value = 9.0
            assert param_override_source(a.get_param("tx")) == Sdf.Layer.Find(out)
            assert not Sdf.Layer.Find(out).dirty
        finally:
            _teardown(scene)

    def test_publish_then_localise_is_a_round_trip(self, tmp_path):
        """A published chunkgroup is a chunkgroup that references its file -- not a network
        host, but localisable: taking the network back drops the arc and authors the
        chunks locally again, objects untouched."""
        stage, scene = _open(None)
        try:
            a, b = self._group_with_chunks(scene)
            group = a.get_context()
            group_path = get_chunk_prim_path(group)
            out = str(tmp_path / "room.usda")
            publish_network(group, out)
            assert is_chunkgroup(group) and not is_network_host(group)
            a.get_param("tx").value = 9.0

            back = localise_network(group)

            assert back is group
            assert not stage.GetPrimAtPath(group_path).HasAuthoredReferences()
            assert not chunk_utils.is_composed_in(stage.GetPrimAtPath(get_chunk_prim_path(a)))
            assert a.get_param("tx").value == 9.0
            assert param_override_source(a.get_param("tx")) is None
            shot = str(tmp_path / "shot.usda")
            export_chunks(scene, shot)
            assert "room.usda" not in Sdf.Layer.FindOrOpen(shot).ExportToString()
        finally:
            _teardown(scene)


@pytest.fixture
def prim_network(tmp_path):
    """A network that authors scene content: one Prim chunk at /thing."""
    seq, context = make_network(tmp_path / "prims.usda", modules=("prim",), chain=False)
    stage = Usd.Stage.Open(seq)
    prim = stage.GetPrimAtPath(f"{context}/Prim")
    prim.CreateAttribute("inputs:primpath_location", Sdf.ValueTypeNames.String).Set("/thing")
    stage.GetRootLayer().Save()
    return seq


def _reference(scene, path, location=None):
    reference = _add(scene, "reference")
    reference.get_param("file_path").value = path
    if location is not None:
        reference.get_param("primpath_location").value = location
    reference.recook()
    return reference


class TestNamespace:
    """A host's location field is the scene root its network's prim paths resolve under.
    Empty -- the default for a network -- means the paths as the sequence wrote them."""

    def test_a_network_is_not_namespaced_by_default(self, prim_network):
        stage, scene = _open(None)
        try:
            host = _reference(scene, prim_network)
            assert host.get_param("primpath_location").value == ""
            inner = next(c for c in scene.get_chunks() if c.name == "Prim")
            inner.recook()
            assert stage.GetPrimAtPath("/thing").IsValid()
        finally:
            _teardown(scene)

    def test_a_location_puts_the_network_under_it(self, prim_network):
        stage, scene = _open(None)
        try:
            _host = _reference(scene, prim_network, "/room")
            inner = next(c for c in scene.get_chunks() if c.name == "Prim")
            path = inner.get_param("primpath_location")
            assert path.raw_value == "/thing", "the stored text is the file's"
            assert path.value == "/room/thing", "the resolved path is the scene's"
            inner.recook()
            assert stage.GetPrimAtPath("/room/thing").IsValid()
            assert not stage.GetPrimAtPath("/thing").IsValid()
            assert not Sdf.Layer.Find(prim_network).dirty
        finally:
            _teardown(scene)

    def test_two_hosts_of_one_network_do_not_collide(self, prim_network):
        stage, scene = _open(None)
        try:
            _reference(scene, prim_network, "/left")
            _reference(scene, prim_network, "/right")
            for inner in [c for c in scene.get_chunks() if c.name == "Prim"]:
                inner.recook()
            assert stage.GetPrimAtPath("/left/thing").IsValid()
            assert stage.GetPrimAtPath("/right/thing").IsValid()
        finally:
            _teardown(scene)

    def test_a_path_written_back_is_stored_relative(self, prim_network):
        """prim.py writes its actual path back into its param (and a viewport pick hands
        over a full stage path). Inside a namespace that must not double up."""
        stage, scene = _open(None)
        try:
            _reference(scene, prim_network, "/room")
            inner = next(c for c in scene.get_chunks() if c.name == "Prim")
            inner.recook()
            path = inner.get_param("primpath_location")
            path.value = "/room/thing"
            assert path.raw_value == "/thing"
            assert path.value == "/room/thing"
            inner.recook()
            assert not stage.GetPrimAtPath("/room/room/thing").IsValid()
        finally:
            _teardown(scene)


class TestWhatTheShotCannotDoToTheSequence:
    def test_deleting_a_hosted_chunk_deactivates_it_and_leaves_the_file_alone(self, hosting):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        path = get_chunk_prim_path(transform)
        transform.request_delete()
        assert not Sdf.Layer.Find(network_file).dirty
        prim = stage.GetPrimAtPath(path)
        assert prim.IsValid(), "a referenced prim cannot be deleted, only deactivated"
        assert not prim.IsActive()
        assert stage.GetRootLayer().GetPrimAtPath(path).active is False

    def test_renaming_a_hosted_chunk_is_refused(self, hosting):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        path = get_chunk_prim_path(transform)
        transform.name = "Moved"
        assert transform.name == "Transform"
        assert stage.GetPrimAtPath(path).IsValid()
        assert not Sdf.Layer.Find(network_file).dirty

    def test_a_saved_arc_is_relative_when_the_file_sits_beside_the_shot(self, hosting, tmp_path):
        _, scene, reference, network_file = hosting
        shot = str(tmp_path / "shot.usda")
        export_chunks(scene, shot)
        text = open(shot).read()
        assert "@./seq.usda@" in text, text


class TestOverridesBelongToTheHostNode:
    """An override on a hosted chunk is authored under the host's prim -- inside the
    Reference node's own block in the saved file -- so the node's fate is the overrides'
    fate: disable the node and they go dormant with its subtree, delete it and they go
    with the subtree. Not in the host's procedural layer, which a cook clears and a save
    does not write."""

    def test_the_saved_file_nests_the_override_inside_the_host_block(self, hosting, tmp_path):
        stage, scene, reference, _ = hosting
        next(c for c in scene.get_chunks() if c.name == "Transform").get_param("tx").value = 2.5
        shot = str(tmp_path / "shot.usda")
        export_chunks(scene, shot)
        saved = Sdf.Layer.FindOrOpen(shot)
        host = saved.GetPrimAtPath(get_chunk_prim_path(reference))
        assert "Transform" in host.nameChildren
        assert host.nameChildren["Transform"].attributes[_TX].default == 2.5

    def test_disabling_the_host_takes_its_network_and_overrides_out_of_effect(self, hosting):
        stage, scene, reference, _ = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 2.5
        path = get_chunk_prim_path(transform)
        chunk_graph.toggle_disabled([reference])
        assert not stage.GetPrimAtPath(get_chunk_prim_path(reference)).IsActive()
        assert not stage.GetPrimAtPath(path).IsValid(), "an inactive prim prunes its subtree"
        chunk_graph.toggle_disabled([reference])
        assert stage.GetAttributeAtPath(f"{path}.{_TX}").Get() == 2.5, "and they come back with it"

    def test_deleting_the_host_deletes_its_overrides(self, hosting):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 2.5
        host_path = get_chunk_prim_path(reference)
        seq_layer = Sdf.Layer.Find(network_file)
        reference.request_delete()
        for layer in stage.GetLayerStack(includeSessionLayers=True):
            if not chunk_utils.is_foreign_layer(layer, stage):
                assert layer.GetPrimAtPath(host_path) is None, layer.identifier
        assert not seq_layer.dirty


class TestTheSequenceIsLive:
    """Change the sequence file while the shot is open and reload the layer: what the
    sequence added appears, what it dropped leaves, the shot's overrides stay. No sync
    step, no reimport -- the arc is composition."""

    def _rewrite_sequence(self, seq: str, add: str, drop: str):
        other = Usd.Stage.Open(seq)
        new = other.DefinePrim(f"/chunks/ChunkGroup/{add}", "Chunk")
        attr = new.CreateAttribute("info:chunkType", Sdf.ValueTypeNames.Token)
        attr.SetVariability(Sdf.VariabilityUniform)
        attr.Set("transform")
        other.RemovePrim(f"/chunks/ChunkGroup/{drop}")
        other.GetRootLayer().Save()

    def test_a_layer_reload_follows_the_sequence(self, hosting):
        stage, scene, reference, network_file = hosting
        transform = next(c for c in scene.get_chunks() if c.name == "Transform")
        transform.get_param("tx").value = 3.5
        gone = next(c for c in scene.get_chunks() if c.name == "Transform1")

        self._rewrite_sequence(network_file, add="Transform9", drop="Transform1")
        Sdf.Layer.Find(network_file).Reload()

        assert _children(scene, reference) == ["Transform", "Transform9"]
        assert gone not in scene.get_chunks()
        assert transform.get_param("tx").value == 3.5
        assert not Sdf.Layer.Find(network_file).dirty

    def test_unhosting_drops_the_children_it_composed_in(self, hosting, scene_file):
        stage, scene, reference, _ = hosting
        inner = [c for c in scene.get_chunks() if c is not reference]
        reference.get_param("file_path").value = scene_file
        reference.recook()
        assert all(c not in scene.get_chunks() for c in inner)


class TestTheHostNodeInTheQtGraph:
    """The node existed before it was pointed at the file -- the order a user does it in."""

    @pytest.fixture
    def graph(self, qapp, network_file):
        stage, scene = _open(None)
        reference = _add(scene, "reference")
        graph_scene = ChunkGraphScene()
        graph_scene.build(stage, scene, reference.get_context())
        try:
            yield stage, scene, reference, graph_scene, network_file
        finally:
            graph_scene.deleteLater()
            _teardown(scene)

    def test_pointing_it_at_a_network_leaves_it_movable_and_makes_it_diveable(self, graph):
        stage, scene, reference, graph_scene, network_file = graph
        node = graph_scene._nodes[get_chunk_prim_path(reference)]
        assert not node._locked and not node._is_chunkgroup

        reference.get_param("file_path").value = network_file
        reference.recook()
        graph_scene._rebuild_nodes()

        node = graph_scene._nodes[get_chunk_prim_path(reference)]
        assert not node._locked, "the host is this graph's own node"
        assert node._is_chunkgroup, "and a subnet to dive into"

    def test_the_location_field_reads_back_what_was_typed(self, graph):
        stage, scene, reference, graph_scene, network_file = graph
        reference.get_param("file_path").value = network_file
        reference.recook()
        location = reference.get_param("primpath_location")
        for typed in ("/room", "/room/east", "/other", ""):
            location.value = typed
            reference.recook()
            assert location.value == typed
            assert getattr(reference, "_network_namespace") == typed.rstrip("/")


from usd_chunks.frontends.qt.nodegraph import ChunkGraphScene  # noqa: E402


@pytest.fixture
def lofted_network(tmp_path):
    """A network whose group lofts Transform's tx as a public param."""
    stage, scene = _open(None)
    a = _add(scene, "transform")
    group = a.get_context()
    loft_param_to_context(a.get_param("tx"), group)
    lofted = next(p for p in group.get_my_params() if getattr(p, "_lofted", False))
    lofted.value = 1.5
    out = str(tmp_path / "lofted.usda")
    export_chunks(scene, out)
    _teardown(scene)
    layer = Sdf.Layer.FindOrOpen(out)
    assert any(a.name.startswith("inputs:") for a in layer.GetPrimAtPath("/chunks/ChunkGroup").attributes.values()), \
        layer.ExportToString()
    return out, lofted.name


class TestLoftedParamsOnTheHost:
    def test_the_hosts_node_shows_the_groups_public_params(self, lofted_network):
        path, name = lofted_network
        stage, scene = _open(None)
        try:
            host = _reference(scene, path)
            public = [p for p in host.get_my_params() if getattr(p, "_lofted", False)]
            assert [p.name for p in public] == [name]
            assert public[0].value == 1.5
        finally:
            _teardown(scene)

    def test_editing_the_public_param_drives_the_inner_one_as_an_override(self, lofted_network):
        path, name = lofted_network
        stage, scene = _open(None)
        try:
            host = _reference(scene, path)
            public = next(p for p in host.get_my_params() if getattr(p, "_lofted", False))
            inner = next(c for c in scene.get_chunks() if c.get_context() is host and c.name == "Transform")
            public.value = 4.0
            assert inner.get_param("tx").value == 4.0
            attr = stage.GetAttributeAtPath(f"{get_chunk_prim_path(host)}.inputs:{name}")
            assert attr.Get() == 4.0
            assert param_override_source(public) == Sdf.Layer.Find(path)
            assert not Sdf.Layer.Find(path).dirty
        finally:
            _teardown(scene)

    def test_the_qt_node_grows_the_public_param_when_the_file_is_set(self, qapp, lofted_network):
        """The node's widget exists before the file is set -- the order a user does it in.
        The public param arrives through the same PARAM_ADDED a live loft uses."""
        path, name = lofted_network
        stage, scene = _open(None)
        graph_scene = ChunkGraphScene()
        try:
            host = _add(scene, "reference")
            graph_scene.build(stage, scene, host.get_context())
            qapp.processEvents()
            node = graph_scene._nodes[get_chunk_prim_path(host)]
            widget = node._proxy.widget() if node._proxy is not None else None
            if widget is None:
                for _ in range(20):
                    qapp.processEvents()
                    if node._proxy is not None:
                        widget = node._proxy.widget()
                        break
            assert widget is not None, "the node has no embedded widget to check"

            host.get_param("file_path").value = path
            host.recook()
            qapp.processEvents()

            public = next(p for p in host.get_my_params() if getattr(p, "_lofted", False))
            assert public.get_hash() in widget.param_widgets
        finally:
            graph_scene.deleteLater()
            _teardown(scene)
