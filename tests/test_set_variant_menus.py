"""Set Variant's two dropdowns are read off the prim, and follow the stage.

Variant Set lists the sets on the first prim Prim Path resolves to; Variant Selection lists
the variants of the chosen set, with (none) to clear. They were two free-text fields.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init, export_chunks


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


def _add_set(stage, path, name, variants):
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        vset = stage.GetPrimAtPath(path).GetVariantSets().AddVariantSet(name)
        for v in variants:
            vset.AddVariant(v)


@pytest.fixture
def scene(qapp):
    stage = Usd.Stage.CreateInMemory()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.DefinePrim("/asset", "Xform")
        stage.DefinePrim("/other", "Xform")
    _add_set(stage, "/asset", "color", ["red", "blue"])
    _add_set(stage, "/asset", "LOD", ["high", "low"])
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, sc)
    return sc


def _set_variant(qapp, scene, prim_path):
    module = chunk_registry.ChunkRegistry().get_chunk_module("set_variant")
    assert module is not None
    node = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(node)
    node.get_param("primpath_location").value = prim_path
    qapp.processEvents()
    return node


def test_the_set_menu_lists_the_prims_variant_sets(qapp, scene) -> None:
    node = _set_variant(qapp, scene, "/asset")

    assert sorted(node.get_param("variant_set").menu_items) == ["LOD", "color"]


def test_the_selection_menu_follows_the_chosen_set(qapp, scene) -> None:
    node = _set_variant(qapp, scene, "/asset")

    node.get_param("variant_set").value = "color"
    qapp.processEvents()
    # USD hands variant names back sorted.
    assert node.get_param("variant_selection").menu_items == ["(none)", "blue", "red"]

    node.get_param("variant_set").value = "LOD"
    qapp.processEvents()
    assert node.get_param("variant_selection").menu_items == ["(none)", "high", "low"]


def test_the_menus_follow_the_stage(qapp, scene) -> None:
    """A set added after the chunk exists -- by an upstream chunk cooking, in practice --
    shows up without touching this chunk's params."""
    node = _set_variant(qapp, scene, "/other")
    assert node.get_param("variant_set").menu_items == []

    _add_set(scene.get_stage(), "/other", "season", ["summer", "winter"])
    qapp.processEvents()

    assert node.get_param("variant_set").menu_items == ["season"]


def test_the_menus_fill_on_a_blocked_chunk(qapp, scene) -> None:
    """A node created in the graph while the context has a view is blocked until it is
    wired in, and blocking suppresses its params' VALUE_CHANGED -- so the prim-path
    callback never fires. The typed path is still recorded on the chunk prim, and that
    stage edit is what has to fill the dropdowns. They used to stay empty until wired."""
    node = _set_variant(qapp, scene, "/other")           # no sets on /other
    node.set_blocked(True)

    node.get_param("primpath_location").value = "/asset"
    qapp.processEvents()
    assert sorted(node.get_param("variant_set").menu_items) == ["LOD", "color"]

    node.get_param("variant_set").value = "color"
    qapp.processEvents()
    assert node.get_param("variant_selection").menu_items == ["(none)", "blue", "red"]


def test_none_clears_and_a_variant_selects(qapp, scene) -> None:
    stage = scene.get_stage()
    node = _set_variant(qapp, scene, "/asset")
    vset = stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color")

    node.get_param("variant_set").value = "color"
    node.get_param("variant_selection").value = "red"
    qapp.processEvents()
    assert vset.GetVariantSelection() == "red"

    node.get_param("variant_selection").value = "(none)"
    qapp.processEvents()
    assert vset.GetVariantSelection() == ""


def test_a_value_the_prim_does_not_offer_stays_listed(qapp, scene) -> None:
    """A saved selection loads before the upstream chunk that defines its set has cooked;
    dropping it then would lose it for good."""
    node = _set_variant(qapp, scene, "/other")           # no sets on /other
    node.get_param("variant_set").menu_items = []
    node.get_param("variant_set").value = "season"
    qapp.processEvents()

    assert node.get_param("variant_set").value == "season"
    assert "season" in node.get_param("variant_set").menu_items

    _add_set(scene.get_stage(), "/other", "season", ["summer", "winter"])
    qapp.processEvents()
    assert node.get_param("variant_set").menu_items == ["season"], "listed once, now for real"
    assert node.get_param("variant_selection").menu_items == ["(none)", "summer", "winter"]


def test_a_saved_selection_survives_a_reload_ahead_of_its_variant_set(qapp, tmp_path) -> None:
    """The Menu-on-load trap (see test_edit_target reload history): the file restores the
    value before the Add Variants chunk upstream has cooked the set it names. An empty menu
    accepts any value, the refresh is held off while loading, and the menus fill in once
    the stage settles -- with the saved value still selected."""

    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, sc)
    reg = chunk_registry.ChunkRegistry()

    add_mod = reg.get_chunk_module("variant")
    add = (add_mod.create() if add_mod.loaded else add_mod.load())[0]
    sc.add_chunk(add)
    add.get_param("primpath_location").value = "/asset"
    rows = add.get_param("variant_entries")
    rows.get_indexed_param(rows.get_param("vs_name"), 0).value = "color"
    rows.get_indexed_param(rows.get_param("v_names"), 0).value = "red, blue"
    qapp.processEvents()

    node = _set_variant(qapp, sc, "/asset")
    node.add_dependency(add)
    node.get_param("variant_set").value = "color"
    qapp.processEvents()
    node.get_param("variant_selection").value = "red"
    qapp.processEvents()
    assert stage.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").GetVariantSelection() == "red"

    out = tmp_path / "variants.usda"
    export_chunks(sc, out)

    stage2 = Usd.Stage.Open(str(out))
    sc2 = _Scene(stage2)
    chunk_registry.ChunkRegistry().initialize(sc2, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage2)
    _chunk_usd_init(stage2, sc2)
    qapp.processEvents()

    restored = next(c for c in sc2.get_chunks() if c.get_config().module == "set_variant")
    assert restored.get_param("variant_set").value == "color"
    assert restored.get_param("variant_selection").value == "red"
    assert restored.get_param("variant_set").menu_items == ["color"]
    assert restored.get_param("variant_selection").menu_items == ["(none)", "blue", "red"]
    assert stage2.GetPrimAtPath("/asset").GetVariantSets().GetVariantSet("color").GetVariantSelection() == "red"
