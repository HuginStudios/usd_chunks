"""Bind Material authors inside the variant a path selects.

A Prim Path of "/asset{color=red}/geo" means: bind on /asset/geo, inside the "red" variant
of /asset's "color" set. It was binding on /asset/geo for every colour. execute() collected
iter_prim_targets into a list before binding, and that generator holds the variant edit
context open only while it is suspended at its yield -- so by the time anything was
authored, every context had been left.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom, UsdShade

from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init

MATERIAL = "/materials/Gold"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []

    def should_install_package(self, package):
        return False


def _asset_with_colours(stage, selected="blue"):
    """/asset with a "color" variant set (red, blue), /asset/geo under it, and a material."""
    asset = UsdGeom.Xform.Define(stage, "/asset").GetPrim()
    vset = asset.GetVariantSets().AddVariantSet("color")
    for name in ("red", "blue"):
        vset.AddVariant(name)
    vset.SetVariantSelection(selected)
    UsdGeom.Cube.Define(stage, "/asset/geo")
    UsdShade.Material.Define(stage, MATERIAL)
    return asset


def _bind_material(qapp, scene, prim_path):
    module = chunk_registry.ChunkRegistry().get_chunk_module("bind_material")
    assert module is not None
    node = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(node)
    node.get_param("material_path").value = MATERIAL
    node.get_param("primpath_location").value = prim_path
    qapp.processEvents()
    return node


def _binding_on(stage, path):
    rel = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).GetDirectBindingRel()
    return [str(t) for t in rel.GetTargets()] if rel else []


def _spec_in_variant(layer, anchor, vset, variant, child):
    """The spec for `child` inside a variant: Sdf spells it /anchor{vset=variant}child."""
    return layer.GetPrimAtPath(f"{anchor}{{{vset}={variant}}}{child}")


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    _asset_with_colours(stage)
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, sc)
    return sc


def test_the_binding_lives_inside_the_selected_variant(qapp, scene) -> None:
    stage = scene.get_stage()
    node = _bind_material(qapp, scene, "/asset{color=red}/geo")

    layer = chunk_registry.ChunkLayerManager().get_or_create_layer(node)
    spec = _spec_in_variant(layer, "/asset", "color", "red", "geo")
    assert spec is not None and "material:binding" in spec.relationships, (
        f"the binding must be authored in the red variant, got:\n{layer.ExportToString()}"
    )
    assert layer.GetPrimAtPath("/asset/geo") is None or "material:binding" not in (
        layer.GetPrimAtPath("/asset/geo").relationships
    ), "and not on the prim outside the variant"

    # Composed: blue is selected, so the red-only binding is not in effect...
    assert _binding_on(stage, "/asset/geo") == []
    # ...until red is.
    stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").SetVariantSelection("red")
    assert _binding_on(stage, "/asset/geo") == [MATERIAL]


def test_the_variant_selection_is_not_left_behind(qapp, scene) -> None:
    """Entering the variant to author must not change what the scene shows afterwards."""
    stage = scene.get_stage()
    _bind_material(qapp, scene, "/asset{color=red}/geo")

    assert stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").GetVariantSelection() == "blue"


def test_a_plain_path_still_binds_outside_any_variant(qapp, scene) -> None:
    stage = scene.get_stage()
    _bind_material(qapp, scene, "/asset/geo")

    assert _binding_on(stage, "/asset/geo") == [MATERIAL]


def test_a_prim_that_only_exists_in_the_variant_is_found(qapp, scene) -> None:
    """Definedness is checked inside the variant too: a prim defined only in "red" is not
    defined while "blue" is selected, and used to be skipped as unresolvable."""
    stage = scene.get_stage()
    asset = stage.GetPrimAtPath("/asset")
    vset = asset.GetVariantSets().GetVariantSet("color")
    with _in_variant(vset, "red"):
        UsdGeom.Sphere.Define(stage, "/asset/red_only")
    outside = stage.GetPrimAtPath("/asset/red_only")
    assert not (outside and outside.IsDefined()), "precondition: blue is selected"

    node = _bind_material(qapp, scene, "/asset{color=red}/red_only")

    layer = chunk_registry.ChunkLayerManager().get_or_create_layer(node)
    spec = _spec_in_variant(layer, "/asset", "color", "red", "red_only")
    assert spec is not None and "material:binding" in spec.relationships


# ---------------------------------------------------------------------------
# a glob after the variant selection
# ---------------------------------------------------------------------------
#
# "/asset{color=red}/**/*Body*" matched nothing: the whole token, braces included, was
# turned into a regex and matched against composed prim paths, which never contain braces.


def _bodies(stage):
    """Two Body meshes that exist in every colour, one that exists only in red."""
    UsdGeom.Mesh.Define(stage, "/asset/geo/Body_L")
    UsdGeom.Mesh.Define(stage, "/asset/geo/Body_R")
    UsdGeom.Mesh.Define(stage, "/asset/geo/Wheel")
    vset = stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color")
    with _in_variant(vset, "red"):
        UsdGeom.Mesh.Define(stage, "/asset/geo/RedBody")


def test_a_glob_after_a_variant_selection_matches_and_keeps_the_selection(qapp, scene) -> None:
    stage = scene.get_stage()
    _bodies(stage)

    resolved = chunk_utils.resolve_prim_paths(stage, "/asset{color=red}/**/*Body*")

    assert sorted(resolved) == [
        "/asset{color=red}/geo/Body_L",
        "/asset{color=red}/geo/Body_R",
        "/asset{color=red}/geo/RedBody",
    ], "every match carries the selection, and the red-only prim is found"
    assert stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").GetVariantSelection() == "blue", (
        "matching inside the variant must not leave it selected"
    )


def test_a_glob_after_a_variant_binds_inside_that_variant(qapp, scene) -> None:
    """The reported case, end to end: @Reference.primpath_location@{color=red}/**/*Body*."""
    stage = scene.get_stage()
    _bodies(stage)

    node = _bind_material(qapp, scene, "/asset{color=red}/**/*Body*")

    layer = chunk_registry.ChunkLayerManager().get_or_create_layer(node)
    for name in ("Body_L", "Body_R", "RedBody"):
        spec = _spec_in_variant(layer, "/asset", "color", "red", f"geo/{name}")
        assert spec is not None and "material:binding" in spec.relationships, (
            f"{name} must be bound inside red, got:\n{layer.ExportToString()}"
        )
    assert _binding_on(stage, "/asset/geo/Body_L") == [], "blue is selected: not bound yet"
    assert _binding_on(stage, "/asset/geo/Wheel") == []

    stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").SetVariantSelection("red")
    assert _binding_on(stage, "/asset/geo/Body_L") == [MATERIAL]
    assert _binding_on(stage, "/asset/geo/RedBody") == [MATERIAL]
    assert _binding_on(stage, "/asset/geo/Wheel") == [], "the glob did not match it"


def test_selections_go_back_onto_a_match_at_every_level() -> None:
    put_back = chunk_utils._with_variant_selections
    one = [("/asset", "color", "red")]
    assert put_back("/asset/geo/Body_L", one) == "/asset{color=red}/geo/Body_L"
    assert put_back("/asset", one) == "/asset{color=red}"
    assert put_back("/assets/other", one) == "/assets/other", "a prefix that is not an ancestor"
    two = [("/a", "x", "1"), ("/a/b", "y", "2")]
    assert put_back("/a/b/c", two) == "/a{x=1}/b{y=2}/c"


class _in_variant:
    """Select a variant, author inside it, restore the previous selection."""

    def __init__(self, vset, name):
        self.vset, self.name = vset, name

    def __enter__(self):
        self.prev = self.vset.GetVariantSelection()
        self.vset.SetVariantSelection(self.name)
        self.ctx = self.vset.GetVariantEditContext()
        self.ctx.__enter__()
        return self

    def __exit__(self, *exc):
        self.ctx.__exit__(*exc)
        self.vset.SetVariantSelection(self.prev)
        return False
