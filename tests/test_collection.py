"""Tests for the Collection chunk and its %name reference token support in
chunk_utils.resolve_prim_paths — the mechanism that makes collections a first-class,
reusable prim-targeting primitive across every other chunk's prim-pattern field.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkBase, ChunkConfig, SceneSender
from usd_chunks.plugins.chunk_usd import initialize as init_chunk_usd


def _exec(mod, node):
    """Mirror the real auto-execute chain (chunk_notice.py), which runs module.execute()
    inside Usd.EditContext(stage, node.get_edit_context()) — each chunk's own anonymous
    layer. Calling mod.execute(node) directly would target the ambient root edit target
    for every chunk, so two clearing chunks would stomp on each other's output."""
    stage = node.get_stage()
    ctx = node.get_edit_context()
    if stage and ctx:
        with Usd.EditContext(stage, ctx):
            mod.execute(node)
    else:
        mod.execute(node)


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.DefinePrim("/geo/a", "Xform")
        stage.DefinePrim("/geo/b", "Xform")
        stage.DefinePrim("/geo/c", "Xform")

    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    init_chunk_usd(stage, sc)
    yield sc


def _make_collection(mod, name: str):
    c = mod.main()
    c.name = name
    c._config = ChunkConfig(module="collection")
    return c


class TestCollectionChunk:
    def setup_method(self):
        self.mod = importlib.import_module("scripts.collection")

    def test_main_returns_chunk(self, scene):
        c = self.mod.main()
        assert isinstance(c, ChunkBase)
        assert c.name == "Collection"

    def test_execute_authors_collection_with_explicit_includes(self, scene):
        c = _make_collection(self.mod, "Coll1")
        scene.add_chunk(c)
        c.get_param("collection_name").value = "myCollection"
        c.get_param("owner_path").value = "/collections"
        c.get_param("include_pattern").value = "/geo/*"

        _exec(self.mod, c)

        stage = scene.get_stage()
        prim = stage.GetPrimAtPath("/collections")
        assert prim.IsValid()

        coll = Usd.CollectionAPI.Get(prim, "myCollection")
        assert coll.GetName() == "myCollection"
        query = coll.ComputeMembershipQuery()
        members = {str(p) for p in Usd.ComputeIncludedPathsFromCollection(query, stage)}
        assert members == {"/geo/a", "/geo/b", "/geo/c"}

    def test_exclude_pattern_removes_members(self, scene):
        c = _make_collection(self.mod, "Coll1")
        scene.add_chunk(c)
        c.get_param("collection_name").value = "myCollection"
        c.get_param("include_pattern").value = "/geo/*"
        c.get_param("exclude_pattern").value = "/geo/b"

        _exec(self.mod, c)

        stage = scene.get_stage()
        coll = Usd.CollectionAPI.Get(stage.GetPrimAtPath("/collections"), "myCollection")
        query = coll.ComputeMembershipQuery()
        members = {str(p) for p in Usd.ComputeIncludedPathsFromCollection(query, stage)}
        assert members == {"/geo/a", "/geo/c"}

    def test_reference_output_token(self, scene):
        c = _make_collection(self.mod, "Coll1")
        scene.add_chunk(c)
        c.get_param("collection_name").value = "myCollection"
        c.get_param("include_pattern").value = "/geo/*"

        _exec(self.mod, c)

        assert c.get_param("reference").value == "%myCollection"


class TestCollectionTokenResolution:
    def setup_method(self):
        self.mod = importlib.import_module("scripts.collection")

    def test_resolve_prim_paths_expands_collection_token(self, scene):
        c = _make_collection(self.mod, "Coll1")
        scene.add_chunk(c)
        c.get_param("collection_name").value = "myCollection"
        c.get_param("include_pattern").value = "/geo/a /geo/c"
        _exec(self.mod, c)

        stage = scene.get_stage()
        resolved = chunk_utils.resolve_prim_paths(stage, "%myCollection")
        assert set(resolved) == {"/geo/a", "/geo/c"}

    def test_unknown_collection_token_resolves_to_nothing(self, scene):
        stage = scene.get_stage()
        assert chunk_utils.resolve_prim_paths(stage, "%doesNotExist") == []

    def test_transform_consumes_collection_reference(self, scene):
        """Integration: Transform's Prim Path field already calls resolve_paths() —
        pointing it at a %name token must transform every collection member with zero
        changes to transform.py itself."""
        coll_chunk = _make_collection(self.mod, "Coll1")
        scene.add_chunk(coll_chunk)
        coll_chunk.get_param("collection_name").value = "myCollection"
        coll_chunk.get_param("include_pattern").value = "/geo/*"
        _exec(self.mod, coll_chunk)

        transform_mod = importlib.import_module("scripts.transform")
        t = transform_mod.main()
        t.name = "Xform1"
        t._config = ChunkConfig(module="transform")
        scene.add_chunk(t)

        t.get_param("primpath_location").value = "%myCollection"
        t.get_param("tx").value = 5.0
        t.get_param("ty").value = 2.0
        t.get_param("tz").value = 1.0
        _exec(transform_mod, t)

        from pxr import UsdGeom
        stage = scene.get_stage()
        for p in ("/geo/a", "/geo/b", "/geo/c"):
            xformable = UsdGeom.Xformable(stage.GetPrimAtPath(p))
            translation = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()
            assert translation == pytest.approx((5.0, 2.0, 1.0))
