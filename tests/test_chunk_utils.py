"""Tests for chunk_utils prim-pattern resolution, including collection (%name) refs."""

from __future__ import annotations

import importlib

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_utils as CU
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as init_chunk_usd


def _stage(*paths):
    stage = Usd.Stage.CreateInMemory()
    for p in paths:
        UsdGeom.Xform.Define(stage, p)
    return stage


def _author_collection(stage, owner, name, includes, excludes=(), rule=Usd.Tokens.expandPrims):
    prim = stage.DefinePrim(owner, "Scope")
    coll = Usd.CollectionAPI.Apply(prim, name)
    coll.CreateIncludesRel().SetTargets([Sdf.Path(p) for p in includes])
    if excludes:
        coll.CreateExcludesRel().SetTargets([Sdf.Path(p) for p in excludes])
    coll.CreateExpansionRuleAttr().Set(rule)
    return coll


def test_resolve_prim_paths_skips_pseudo_root():
    """The pseudo-root '/' must never be a target -- authoring an op on it raises. A param
    value of '/' (e.g. an empty PRIM_SELECTION) previously resolved to ['/'] and crashed
    chunks like Transform (AddTransformOp on the pseudo-root)."""
    stage = _stage("/a")
    assert CU.resolve_prim_paths(stage, "/") == []
    assert CU.resolve_prim_paths(stage, "/a") == ["/a"]
    assert CU.resolve_prim_paths(stage, "/, /a") == ["/a"]


# --- find_collection -------------------------------------------------------


def test_find_collection_by_name():
    stage = _stage("/World/a", "/World/b")
    _author_collection(stage, "/collections", "grp", ["/World/a"])
    coll = CU.find_collection(stage, "grp")
    assert coll is not None and coll.GetName() == "grp"


def test_find_collection_missing_returns_none():
    stage = _stage("/World/a")
    assert CU.find_collection(stage, "nope") is None


# --- resolve_collection_members -------------------------------------------


def test_resolve_collection_members_explicit():
    stage = _stage("/World/a", "/World/b", "/World/c")
    _author_collection(stage, "/collections", "grp", ["/World/a", "/World/b"], rule=Usd.Tokens.explicitOnly)
    assert sorted(CU.resolve_collection_members(stage, "grp")) == ["/World/a", "/World/b"]


def test_resolve_collection_members_excludes():
    stage = _stage("/World/a", "/World/b", "/World/c")
    _author_collection(stage, "/collections", "grp", ["/World"], excludes=["/World/b"])
    members = CU.resolve_collection_members(stage, "grp")
    assert "/World/a" in members and "/World/c" in members
    assert "/World/b" not in members


def test_resolve_collection_members_missing_returns_empty():
    stage = _stage("/World/a")
    assert CU.resolve_collection_members(stage, "nope") == []


# --- resolve_prim_paths ----------------------------------------------------


def test_resolve_explicit_absolute():
    stage = _stage("/World/a", "/World/b")
    assert CU.resolve_prim_paths(stage, "/World/a") == ["/World/a"]


def test_resolve_relative_name_gets_slash_prepended():
    stage = _stage("/World")
    # A bare relative name is not a valid prim path; it should be normalised to absolute.
    assert CU.resolve_prim_paths(stage, "World") == ["/World"]


def test_resolve_glob_single_component():
    stage = _stage("/World/a", "/World/b", "/World/c", "/Other/d")
    assert sorted(CU.resolve_prim_paths(stage, "/World/*")) == ["/World/a", "/World/b", "/World/c"]


def test_resolve_collection_ref():
    stage = _stage("/World/a", "/World/b", "/World/c")
    _author_collection(stage, "/collections", "grp", ["/World/a", "/World/b"], rule=Usd.Tokens.explicitOnly)
    assert sorted(CU.resolve_prim_paths(stage, "%grp")) == ["/World/a", "/World/b"]


def test_resolve_missing_collection_ref_is_empty():
    stage = _stage("/World/a")
    assert CU.resolve_prim_paths(stage, "%nope") == []


def test_resolve_dedups_overlapping_matches():
    """A prim matched by both an explicit path and a collection appears once."""
    stage = _stage("/World/a", "/World/b")
    _author_collection(stage, "/collections", "grp", ["/World/a"], rule=Usd.Tokens.explicitOnly)
    result = CU.resolve_prim_paths(stage, "/World/a, %grp")
    assert result.count("/World/a") == 1
    assert result == ["/World/a"]


def test_resolve_dedup_preserves_order():
    stage = _stage("/World/a", "/World/b", "/World/c")
    # /World/b listed explicitly first, then again via the glob -- order of first
    # appearance is kept, no duplicate.
    result = CU.resolve_prim_paths(stage, "/World/b, /World/*")
    assert result[0] == "/World/b"
    assert result.count("/World/b") == 1
    assert sorted(result) == ["/World/a", "/World/b", "/World/c"]


# --- author_in_variant_contexts / target_paths ------------------------------


class _StubParam:
    def __init__(self, paths):
        self._paths = paths

    def resolve_paths(self):
        return self._paths


class _PathParam:
    """A LineEdit stand-in whose paths resolve from its value, like the real one."""

    def __init__(self, stage, value):
        self._stage = stage
        self.value = value

    @property
    def raw_value(self):
        return self.value

    def resolve_paths(self):
        return CU.resolve_prim_paths(self._stage, self.value)


def _variant_stage():
    stage = _stage("/asset")
    vset = stage.GetPrimAtPath("/asset").GetVariantSets().AddVariantSet("lod")
    vset.AddVariant("high")
    vset.AddVariant("low")
    vset.SetVariantSelection("low")
    return stage


def test_author_in_variant_contexts_yields_resolved_paths():
    stage = _stage("/World/a", "/World/b")
    param = _StubParam(["/World/a", "/World/b"])
    assert list(CU.author_in_variant_contexts(stage, param)) == ["/World/a", "/World/b"]


def test_author_in_variant_contexts_authors_inside_the_variant():
    """A {set=variant} path yields the stripped path with that variant's edit context open."""
    stage = _variant_stage()
    param = _StubParam(["/asset{lod=high}/geo"])
    for path in CU.author_in_variant_contexts(stage, param):
        assert path == "/asset/geo"
        stage.DefinePrim(path, "Xform")

    layer = stage.GetRootLayer()
    assert layer.GetPrimAtPath("/asset{lod=high}geo")
    assert not layer.GetPrimAtPath("/asset/geo")
    # The selection it had to set to author is put back afterwards.
    assert stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSelection("lod") == "low"


def test_target_paths_strips_selections_without_entering_a_context():
    _stage = _variant_stage()
    param = _StubParam(["/asset{lod=high}/geo", "/other"])
    assert CU.target_paths(param) == ["/asset/geo", "/other"]


def test_target_paths_drops_paths_that_strip_to_the_same_prim():
    """Two variants of one prim are one relationship target, first occurrence kept."""
    param = _StubParam(["/b", "/asset{lod=high}/geo", "/asset{lod=low}/geo", "/a"])
    assert CU.target_paths(param) == ["/b", "/asset/geo", "/a"]


def test_unique_claims_a_free_path_and_writes_it_back():
    stage = _stage("/World/cam")
    stage.GetPrimAtPath("/World/cam").SetTypeName("Camera")
    param = _PathParam(stage, "/World/cam")
    assert list(CU.author_in_variant_contexts(stage, param, unique=True)) == ["/World/cam1"]
    assert param.value == "/World/cam1"


def test_unique_leaves_variant_paths_and_lists_alone():
    """Uniqueness would test a variant path against whatever variant is selected now, and a
    list or glob names existing targets -- neither is a path this chunk is claiming."""
    stage = _variant_stage()
    stage.DefinePrim("/asset/geo", "Xform")
    for value in ("/asset{lod=high}/geo", "/asset/geo, /asset", "/asset/*"):
        param = _PathParam(stage, value)
        list(CU.author_in_variant_contexts(stage, param, unique=True))
        assert param.value == value


# --- chunk_internal_layer_ids -----------------------------------------------


class _DummyChunkScene(SceneSender):
    def __init__(self):
        super().__init__()
        self._stage = None

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def test_chunk_internal_layer_ids_matches_layer_managers_own_set():
    """chunk_internal_layer_ids() must read ChunkLayerManager's own incrementally-
    maintained internal_layer_ids set, not rebuild it by walking every tracked layer's
    live subLayerPaths.

    That walk used to run inside _refresh_edit_target/_refresh_after_layer/export_usd's
    row refresh -- each registered listen_globally=True, so it re-ran once per live
    Layer/Edit Target/Export USD chunk on every single stage edit. A chunk delete
    fires several such edits, so tearing down N chunks paid for this walk O(N) times per
    edit -- superlinear in the number of chunks still alive, not just the ones being
    deleted (confirmed live: an autouse test fixture that cleaned up every chunk after
    each test made the suite ~500x slower to run than just leaking them). Reading the
    already-maintained set turns each call back into a cheap lookup, same as
    chunk_registry.is_chunk_internal_layer.

    Standalone (not appended to tests/test_edit_target_migration.py's own
    TestEditTargetMigration class): that file's shared scene/teardown state is already
    documented as order-sensitive via leaked global listeners, and inserting a test
    there was observed to make an unrelated, later test in the same file flaky.
    """
    create_mod = importlib.import_module("chunks.usd.layer")
    edit_mod = importlib.import_module("chunks.usd.edit_target")

    scene = _DummyChunkScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    stage = Usd.Stage.CreateInMemory()
    scene._stage = stage
    init_chunk_usd(stage, scene)

    try:
        cl = create_mod.main()
        cl._config = ChunkConfig(module="layer")
        scene.add_chunk(cl)
        cl.get_param("layer_name").value = "layerA"
        cl.recook()

        et = edit_mod.main()
        et._config = ChunkConfig(module="edit_target")
        scene.add_chunk(et)
        et.get_param("edit_target").value = "layerA"
        et.recook()

        lm = chunk_registry.ChunkLayerManager()
        assert lm.internal_layer_ids, "setup must be faithful: some layer must be tracked"
        assert CU.chunk_internal_layer_ids() == set(lm.internal_layer_ids)
    finally:
        for c in list(scene.chunks.get_my_params()):
            c.request_delete()


# ---------------------------------------------------------------------------
# define_with_ancestors
# ---------------------------------------------------------------------------


def test_define_with_ancestors_types_the_ancestors_it_creates() -> None:
    """The reason the helper exists: DefinePrim leaves the ancestors it invents typeless,
    and a typeless prim is not Xformable, so there is nothing to grab at /a or /a/b."""
    stage = Usd.Stage.CreateInMemory()

    CU.define_with_ancestors(stage, "/a/b/c", "Xform")

    assert [p.GetTypeName() for p in Usd.PrimRange(stage.GetPrimAtPath("/a"))] == [
        "Xform", "Xform", "Xform"
    ]


def test_define_with_ancestors_leaves_a_typed_ancestor_alone() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Scope.Define(stage, "/world")

    CU.define_with_ancestors(stage, "/world/asset", "Xform")

    assert stage.GetPrimAtPath("/world").GetTypeName() == "Scope"
    assert stage.GetPrimAtPath("/world/asset").GetTypeName() == "Xform"


def test_define_with_ancestors_defines_a_leaf_that_was_only_an_over() -> None:
    """`IsValid()` is true for an `over`, so guarding on it would skip this and hand back a
    prim that was never defined at all -- exactly the shape chunk_layers.define_ancestors
    exists to repair, so this must not create one."""
    stage = Usd.Stage.CreateInMemory()
    Sdf.CreatePrimInLayer(stage.GetRootLayer(), "/asset").typeName = "Xform"
    assert stage.GetPrimAtPath("/asset").IsValid()
    assert not stage.GetPrimAtPath("/asset").IsDefined()

    CU.define_with_ancestors(stage, "/asset", "Xform")

    assert stage.GetPrimAtPath("/asset").IsDefined()
    assert stage.GetPrimAtPath("/asset").GetSpecifier() == Sdf.SpecifierDef


def test_define_with_ancestors_retypes_a_leaf_that_is_not_what_was_asked_for() -> None:
    """Importing onto an existing Scope must still give back something transformable.

    The leaf is the thing the caller named, so it gets `type_name` whatever was there
    before; only ancestors, which are incidental, keep a type they already had.
    """
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Scope.Define(stage, "/root")

    prim = CU.define_with_ancestors(stage, "/root", "Xform")

    assert prim.GetTypeName() == "Xform"
    assert UsdGeom.Xformable(prim)
