from __future__ import annotations

from pxr import Usd
from PySide6 import QtCore

from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.frontends.qt.widgets import (
    ChunkWidget,
    ParamsFilterBar,
    _PARAMS_HEADER_BG,
)

from chunk_stubs import attach_stub_module


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _chunk_with_module(name: str, module: str | None = None) -> chunk.Chunk:
    """A plain chunk backed by a registered module: by default a stub with no hooks
    (chunk_stubs), so the test neither cooks nor depends on a module in chunks/."""
    c = chunk.Chunk(name)
    if module is None:
        attach_stub_module(c)
    else:
        c._config = ChunkConfig(module=module)
    return c


def _make_scene(stage: Usd.Stage):
    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    return scene


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# generalized gate + live toggle (issue 05)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# fuzzy_matches / chunk_matches_search (issue 06)
# ---------------------------------------------------------------------------

def test_fuzzy_matches_exact_substring() -> None:
    assert chunk_stage.fuzzy_matches("trans", "transform_scale") is True
    assert chunk_stage.fuzzy_matches("VIEW", "View1") is True  # case-insensitive


def test_fuzzy_matches_empty_term_matches_everything() -> None:
    assert chunk_stage.fuzzy_matches("", "anything") is True
    assert chunk_stage.fuzzy_matches("   ", "anything") is True


def test_fuzzy_matches_tolerates_a_small_typo() -> None:
    assert chunk_stage.fuzzy_matches("transfrom", "transform") is True


def test_fuzzy_matches_rejects_unrelated_term() -> None:
    assert chunk_stage.fuzzy_matches("xyz123", "transform_scale") is False


def test_fuzzy_matches_rejects_a_shorter_name_that_merely_shares_a_prefix() -> None:
    """Searching for the longer of two similarly-named chunks returns only that one.

    Two transforms named "haha" and "haha222": searching "haha222" used to bring back
    "haha" as well. Nothing was wrong with the search term -- SequenceMatcher normalises
    by combined length, so the four shared characters carried "haha" to 0.73, over the
    typo threshold. The shorter name is not a misspelling of what was typed.
    """
    assert chunk_stage.fuzzy_matches("haha222", "haha") is False
    assert chunk_stage.fuzzy_matches("haha222", "haha222") is True


def test_fuzzy_matches_still_finds_longer_names_from_a_fragment() -> None:
    """The narrowing above is one-directional: typing a prefix still widens, as before."""
    assert chunk_stage.fuzzy_matches("haha", "haha222") is True


def test_chunk_matches_search_scopes_to_name_or_type() -> None:
    c = _chunk_with_module("transform_a", module="xform")
    assert chunk_stage.chunk_matches_search(c, "xform", scope="type") is True
    assert chunk_stage.chunk_matches_search(c, "xform", scope="name") is False
    assert chunk_stage.chunk_matches_search(c, "transform", scope="type") is False
    assert chunk_stage.chunk_matches_search(c, "transform", scope="name") is True
    assert chunk_stage.chunk_matches_search(c, "xform", scope="both") is True


def test_chunk_matches_search_empty_query_matches_everything() -> None:
    c = _chunk_with_module("anything", module="whatever")
    assert chunk_stage.chunk_matches_search(c, "", scope="name") is True


# ---------------------------------------------------------------------------
# search persistence (issue 06)
# ---------------------------------------------------------------------------

def test_search_query_and_scope_default_when_absent() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    assert chunk_stage.get_search_query(ctx) == ""
    assert chunk_stage.get_search_scope(ctx) == "both"


def test_search_query_and_scope_roundtrip_through_export_reload() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    chunk_stage.set_search_query(ctx, "transform")
    chunk_stage.set_search_scope(ctx, "type")

    usda = stage.GetRootLayer().ExportToString()
    reloaded = Usd.Stage.CreateInMemory()
    reloaded.GetRootLayer().ImportFromString(usda)
    prim = reloaded.GetPrimAtPath("/Tab1")
    assert prim.GetAttribute("filter:searchQuery").Get() == "transform"
    assert prim.GetAttribute("filter:searchScope").Get() == "type"


def test_set_search_scope_rejects_unrecognized_value() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    chunk_stage.set_search_scope(ctx, "bogus")
    assert chunk_stage.get_search_scope(ctx) == "both"


# ---------------------------------------------------------------------------
# search box + scope chips live-filter the Params page (issue 06)
# ---------------------------------------------------------------------------

def test_search_box_live_filters_by_name(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c1 = _chunk_with_module("transform_a", module="xform")
    c2 = _chunk_with_module("light_b", module="lamp")
    scene.add_chunk(c1)
    scene.add_chunk(c2)
    chunk_stage.set_chunk_lofted(c1, True)
    chunk_stage.set_chunk_lofted(c2, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    assert c1.get_hash() in w.group_widgets and c2.get_hash() in w.group_widgets

    w._filter_bar.search_box.setText("transform")
    assert c1.get_hash() in w.group_widgets
    assert c2.get_hash() not in w.group_widgets

    w._filter_bar.search_box.setText("")
    assert c1.get_hash() in w.group_widgets
    assert c2.get_hash() in w.group_widgets


def _set_scope(filter_bar, scope: str) -> None:
    filter_bar.scope_combo.setCurrentIndex(filter_bar.scope_combo.findData(scope))


def test_scope_combo_narrows_search_to_type_only(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c1 = _chunk_with_module("transform_a", module="xform")
    c2 = _chunk_with_module("light_b", module="lamp")
    scene.add_chunk(c1)
    scene.add_chunk(c2)
    chunk_stage.set_chunk_lofted(c1, True)
    chunk_stage.set_chunk_lofted(c2, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    _set_scope(w._filter_bar, "type")
    w._filter_bar.search_box.setText("transform_a")  # matches c1's NAME, not its type

    assert c1.get_hash() not in w.group_widgets  # name match doesn't count, scope is type-only
    assert c2.get_hash() not in w.group_widgets


def test_scope_combo_only_ever_has_one_of_three_values(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    assert w._filter_bar.current_scope() == "both"  # default
    for scope in ("name", "type", "both"):
        _set_scope(w._filter_bar, scope)
        assert w._filter_bar.current_scope() == scope


def test_search_shows_every_matching_chunk_lofted_or_not(qapp) -> None:
    """The page shows what is lofted; a search shows every chunk that matches instead."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    lofted_match = _chunk_with_module("transform_a", module="xform")
    other_match = _chunk_with_module("transform_b", module="xform")
    lofted_nomatch = _chunk_with_module("light_a", module="lamp")
    scene.add_chunk(lofted_match)
    scene.add_chunk(other_match)
    scene.add_chunk(lofted_nomatch)
    chunk_stage.set_chunk_lofted(lofted_match, True)
    chunk_stage.set_chunk_lofted(other_match, False)
    chunk_stage.set_chunk_lofted(lofted_nomatch, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    assert other_match.get_hash() not in w.group_widgets  # not lofted: not on the page

    w._filter_bar.search_box.setText("transform")
    assert lofted_match.get_hash() in w.group_widgets
    assert other_match.get_hash() in w.group_widgets  # matches, though not lofted
    assert lofted_nomatch.get_hash() not in w.group_widgets  # lofted, but doesn't match

    # Cleared: back to what is lofted.
    w._filter_bar.search_box.setText("")
    assert other_match.get_hash() not in w.group_widgets
    assert lofted_nomatch.get_hash() in w.group_widgets


def test_search_state_persists_and_reflects_in_filter_bar_on_rebuild(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w1 = ChunkWidget(ctx, is_tab_context=True)
    w1._filter_bar.search_box.setText("transform")
    _set_scope(w1._filter_bar, "type")

    # Simulate reopening the tab (a fresh widget reading the same persisted context).
    w2 = ChunkWidget(ctx, is_tab_context=True)
    assert w2._filter_bar.search_box.text() == "transform"
    assert w2._filter_bar.current_scope() == "type"


# ---------------------------------------------------------------------------
# filter bar placement: a tinted "ParamsHeader" panel (prim path -> lofted own-
# params, e.g. a chunkgroup's "doc" Description field from chunks/core/chunkgroup.py
# -> filter bar) -> an embossed separator -> the chunk list
# ---------------------------------------------------------------------------

def _layout_widgets(layout) -> list:
    widgets = []
    for i in range(layout.count()):
        widgets.append(layout.itemAt(i).widget())
    return widgets


def _index_of(widgets: list, target) -> int:
    for i, w in enumerate(widgets):
        if w is target:
            return i
    raise AssertionError(f"{target!r} not found in layout")


def test_filter_bar_follows_prim_path_in_the_header_panel_on_a_plain_tab(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    assert w._lofted_container.isVisible() is False  # plain tabs have no own params to loft

    top_level = _layout_widgets(w.__layout__)
    header_idx = _index_of(top_level, w._header_container)
    separator_idx = _index_of(top_level, w._filter_bar_separator)
    assert separator_idx == header_idx + 1  # separator directly follows the tinted header panel

    header_children = _layout_widgets(w._header_container.layout())
    prim_path_idx = _index_of(header_children, w._prim_path_field)
    lofted_idx = _index_of(header_children, w._lofted_container)
    filter_bar_idx = _index_of(header_children, w._filter_bar)
    assert lofted_idx == prim_path_idx + 1
    assert filter_bar_idx == lofted_idx + 1
    assert isinstance(w._filter_bar, ParamsFilterBar)


def test_filter_bar_follows_lofted_description_in_the_header_panel_on_a_chunkgroup_tab(qapp) -> None:
    """A chunkgroup's own top-level params (e.g. chunks/core/chunkgroup.py's "doc" Description
    TextEdit) render lofted, above the filter bar -- via ChunkWidget.add_widget's existing
    lofted-params branch for CONTEXT chunks, not anything the filter bar itself builds. Both
    live inside the same tinted header panel as the prim path field and the filter bar."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    grp = chunk.ChunkBase(
        "mygroup",
        flags=ChunkFlags.GroupFlags.CONTEXT | ChunkFlags.GroupFlags.LABELED,
    )
    grp._config = ChunkConfig(module="group")
    doc = chunk.TextEdit("doc", "Description", rows=3, flags=ChunkFlags.ParamFlags.LABELED)
    grp.add_param(doc)
    scene.add_chunk(grp)

    w = ChunkWidget(grp, is_tab_context=True)
    assert w._lofted_container.isVisible() is True
    assert w._lofted_layout.count() == 1  # the "doc" field landed here, not in the chunk list

    header_children = _layout_widgets(w._header_container.layout())
    prim_path_idx = _index_of(header_children, w._prim_path_field)
    lofted_idx = _index_of(header_children, w._lofted_container)
    filter_bar_idx = _index_of(header_children, w._filter_bar)
    assert lofted_idx == prim_path_idx + 1
    assert filter_bar_idx == lofted_idx + 1


def test_header_panel_has_a_distinct_tint_and_separator_is_visible(qapp) -> None:
    """A plain QFrame HLine would be invisible here -- CHUNK_STYLESHEET's global
    `QFrame { border: none; }` rule strips the border frame-shape painting relies on -- so the
    header panel's tint and the separator both use explicit background styling instead."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    assert "background-color" in w._header_container.styleSheet()
    assert w._filter_bar_separator.height() > 0
    assert "background" in w._filter_bar_separator.styleSheet()

    top_level = _layout_widgets(w.__layout__)
    header_idx = _index_of(top_level, w._header_container)
    assert top_level[header_idx + 1] is w._filter_bar_separator


def test_prim_path_and_lofted_container_match_header_panel_tint(qapp) -> None:
    """Both are plain QWidgets with no background of their own by default -- they must be
    explicitly tinted to match the header panel rather than showing some other default,
    with WA_StyledBackground set so a bare QWidget actually paints it."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    for widget in (w._prim_path_field, w._lofted_container):
        assert _PARAMS_HEADER_BG in widget.styleSheet()
        assert widget.testAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground) is True


# ---------------------------------------------------------------------------
# the filter bar's own state, once an Edit Target chunk has moved the target
# ---------------------------------------------------------------------------

def test_the_search_query_still_updates_after_the_edit_target_moves() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    chunk_stage.set_search_query(ctx, "apple")
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    chunk_stage.set_search_query(ctx, "pear")

    assert chunk_stage.get_search_query(ctx) == "pear"


def test_the_search_scope_still_updates_after_the_edit_target_moves() -> None:
    """The third setter of the same shape. Untested in the original report only because
    the scope menu is reached less often than the other two, not because it behaves."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    chunk_stage.set_search_scope(ctx, "name")
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    chunk_stage.set_search_scope(ctx, "type")

    assert chunk_stage.get_search_scope(ctx) == "type"
