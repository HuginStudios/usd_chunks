"""Tests for chunk_utils prim-pattern resolution, including collection (%name) refs."""

from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_utils as CU


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
    # /World/b listed explicitly first, then again via the glob — order of first
    # appearance is kept, no duplicate.
    result = CU.resolve_prim_paths(stage, "/World/b, /World/*")
    assert result[0] == "/World/b"
    assert result.count("/World/b") == 1
    assert sorted(result) == ["/World/a", "/World/b", "/World/c"]


# --- iter_prim_targets -----------------------------------------------------


class _StubParam:
    def __init__(self, paths):
        self._paths = paths

    def resolve_paths(self):
        return self._paths


def test_iter_prim_targets_yields_resolved_paths():
    stage = _stage("/World/a", "/World/b")
    param = _StubParam(["/World/a", "/World/b"])
    assert list(CU.iter_prim_targets(stage, param)) == ["/World/a", "/World/b"]
