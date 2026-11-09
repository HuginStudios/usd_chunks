"""Inherit and Specialize add their arc, and each has its own strength."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks.chunk_script import Scene


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    # An asset that brings size=2 with it, referenced in at /geo/a; and a base class in the
    # shot saying size=1.
    asset = Sdf.Layer.CreateAnonymous(".usda")
    asset.ImportFromString('#usda 1.0\ndef Cube "Asset"\n{\n    double size = 2\n}\n')
    stage.DefinePrim("/geo/a").GetReferences().AddReference(asset.identifier, "/Asset")
    stage.CreateClassPrim("/_base").CreateAttribute("size", Sdf.ValueTypeNames.Double).Set(1.0)
    s = Scene(stage, "pytest")
    s._asset = asset  # keep the anonymous layer alive
    yield s
    s.close()


def _arcs(scene, field, path="/geo/a"):
    """The arc targets authored on the prim, e.g. field="inheritPathList"."""
    prim = scene.get_stage().GetPrimAtPath(path)
    return [item for spec in prim.GetPrimStack() for item in getattr(spec, field).GetAddedOrExplicitItems()]


def _size(scene, path="/geo/a"):
    return scene.get_stage().GetPrimAtPath(path).GetAttribute("size").Get()


def test_inherits_lets_the_base_override_the_referenced_asset(scene):
    scene.add("inherit", prim_path="/geo/a", base_path="/_base")

    assert _arcs(scene, "inheritPathList") == [Sdf.Path("/_base")]
    assert _size(scene) == 1.0


def test_specializes_leaves_the_base_weaker_than_the_asset(scene):
    scene.add("specialize", prim_path="/geo/a", base_path="/_base")

    assert _arcs(scene, "specializesList") == [Sdf.Path("/_base")]
    assert not _arcs(scene, "inheritPathList")
    assert _size(scene) == 2.0


def test_a_new_base_replaces_the_old_arc(scene):
    node = scene.add("specialize", prim_path="/geo/a", base_path="/_base")
    node.get_param("base_path").value = "/_other_base"

    assert _arcs(scene, "specializesList") == [Sdf.Path("/_other_base")]


def test_a_missing_base_is_created_as_an_abstract_class(scene):
    scene.add("inherit", prim_path="/geo/a", base_path="/_new_base")

    base = scene.get_stage().GetPrimAtPath("/_new_base")
    assert base.IsValid() and base.IsAbstract()
    assert base.GetSpecifier() == Sdf.SpecifierClass


def test_a_prim_under_the_base_is_skipped(scene):
    scene.add("inherit", prim_path="/_base/child", base_path="/_base")

    assert not scene.get_stage().GetPrimAtPath("/_base/child").IsValid()


def test_nothing_picked_writes_nothing(scene):
    scene.add("inherit", base_path="/_other")

    assert not scene.get_stage().GetPrimAtPath("/_other").IsValid()
