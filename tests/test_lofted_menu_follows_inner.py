"""A lofted Menu offers what the inner Menu offers, live and after a reopen.

Reported: loft Set Variant's Variant Selection up to its chunkgroup, save, reopen -- and
the group's dropdown holds one entry, the saved value. The loft is a clone taken once,
and menu_items came across with it: whatever the inner menu listed at that instant. On a
reopen that instant is before the inner chunk has read its prim (its menus fill on
CHUNK_LOADED and again as the stage cooks), so the clone froze an empty list plus the
saved value and never heard about the real one.

Not a reopen-only bug either: an inner Menu whose items follow the stage (Set Variant's
do, off the prim at Prim Path) changed under a live loft without the group's copy
noticing. Those items are read as the menu opens now (the menu_items hook), so opening
the group's copy asks the inner one.
"""
from __future__ import annotations

import weakref

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import export_chunks, initialize as _chunk_usd_init


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
        prim = stage.GetPrimAtPath(path)
        if not prim:
            prim = stage.DefinePrim(path, "Xform")
        vset = prim.GetVariantSets().AddVariantSet(name)
        for v in variants:
            vset.AddVariant(v)


def _settle(qapp):
    for _ in range(25):
        qapp.processEvents()
        QtCore.QThread.msleep(10)
    qapp.processEvents()


def _init(stage):
    scene = _Scene(stage)
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _mk(scene, key, context=None):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    prev = scene._current_context
    scene._current_context = weakref.ref(context) if context is not None else None
    try:
        for c in cs:
            scene.add_chunk(c)
    finally:
        scene._current_context = prev
    return cs[0]


def _opened(menu):
    """The menu's items as a frontend shows them: asked for as it opens (Menu.refresh_items).
    A lofted menu asks its inner one, whose items it mirrors."""
    menu.refresh_items()
    return list(menu.menu_items)


def _lofted_menu(group, suffix):
    return next(p for p in group.get_my_params() if p.name.endswith(suffix))


@pytest.fixture
def lofted(qapp):
    """A chunkgroup publishing its inner Set Variant's two menus, on a prim with sets."""
    stage = Usd.Stage.CreateInMemory()
    _add_set(stage, "/asset", "color", ["red", "blue"])
    _add_set(stage, "/other", "season", ["summer", "winter"])
    scene = _init(stage)

    group = _mk(scene, "chunkgroup")
    inner = _mk(scene, "set_variant", context=group)
    _settle(qapp)
    inner.get_param("primpath_location").value = "/asset"
    _settle(qapp)
    inner.get_param("variant_set").value = "color"
    _settle(qapp)
    inner.get_param("variant_selection").value = "red"
    _settle(qapp)
    assert _opened(inner.get_param("variant_selection")) == ["(none)", "blue", "red"]

    chunk_stage.loft_param_to_context(inner.get_param("variant_set"), group)
    chunk_stage.loft_param_to_context(inner.get_param("variant_selection"), group)
    _settle(qapp)
    return dict(stage=stage, scene=scene, group=group, inner=inner)


def test_a_fresh_loft_carries_the_items(lofted):
    assert _opened(_lofted_menu(lofted["group"], "variant_selection")) == ["(none)", "blue", "red"]


def test_the_lofted_menu_follows_the_inner_menus_items(qapp, lofted):
    """Retarget the inner chunk: its menus rebuild off the new prim, so must the group's."""
    inner, group = lofted["inner"], lofted["group"]

    inner.get_param("primpath_location").value = "/other"
    _settle(qapp)

    assert _opened(inner.get_param("variant_set")) == ["season", "color"]
    assert _opened(_lofted_menu(group, "variant_set")) == ["season", "color"]


def test_picking_through_the_lofted_menu_still_reaches_the_inner_one(qapp, lofted):
    """Pinned: the value direction that already worked."""
    inner, group = lofted["inner"], lofted["group"]

    _lofted_menu(group, "variant_selection").value = "blue"
    _settle(qapp)

    assert inner.get_param("variant_selection").value == "blue"


def test_a_reopened_lofted_menu_offers_every_item_not_just_the_saved_one(qapp, lofted, tmp_path):
    out = tmp_path / "lofted_menu.usda"
    export_chunks(lofted["scene"], out)

    stage2 = Usd.Stage.Open(str(out))
    # Only chunk prims are exported; the asset the menus read off comes from elsewhere
    # (an upstream chunk's cook, in practice). Present before the chunks load.
    _add_set(stage2, "/asset", "color", ["red", "blue"])
    scene2 = _init(stage2)
    _settle(qapp)

    group2 = next(c for c in scene2.get_chunks() if c.name == lofted["group"].name)
    inner2 = next(c for c in group2.get_my_params() if c.name == lofted["inner"].name)
    sel2 = _lofted_menu(group2, "variant_selection")
    assert sel2.value == "red", "the saved pick comes back"
    assert _opened(inner2.get_param("variant_selection")) == ["(none)", "blue", "red"]
    assert _opened(sel2) == ["(none)", "blue", "red"], (
        f"a reopened lofted menu must offer what the inner one offers, got {sel2.menu_items}"
    )


def test_a_reopened_lofted_menu_follows_a_prim_that_arrives_later(qapp, lofted, tmp_path):
    """The real reopen order: the chunks load first and the prim they read shows up when
    something upstream cooks. The inner menu fills then; the lofted one must too."""
    out = tmp_path / "lofted_menu_late.usda"
    export_chunks(lofted["scene"], out)

    stage2 = Usd.Stage.Open(str(out))
    scene2 = _init(stage2)
    _settle(qapp)

    group2 = next(c for c in scene2.get_chunks() if c.name == lofted["group"].name)
    sel2 = _lofted_menu(group2, "variant_selection")
    # No prim yet: Set Variant lists its clear entry plus the kept pick, and so must the loft.
    assert _opened(sel2) == ["(none)", "red"]

    _add_set(stage2, "/asset", "color", ["red", "blue"])
    _settle(qapp)

    assert _opened(sel2) == ["(none)", "blue", "red"]
