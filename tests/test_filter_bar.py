from __future__ import annotations

from pxr import Usd
from PySide6 import QtCore

from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init
from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _chunk_with_module(name: str, module: str = "prim") -> chunk.Chunk:
    c = chunk.Chunk(name)
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
# filter:pinEnabled persistence (issue 05)
# ---------------------------------------------------------------------------

def test_pin_filter_defaults_enabled_when_attribute_absent() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    assert chunk_usd.is_pin_filter_enabled(ctx) is True


def test_set_pin_filter_enabled_roundtrips_and_defines_prim_on_demand() -> None:
    """A plain tab context's own prim is never authored by _define_prim (PARAM_ADDED never
    fires for it — see SceneSender.add_chunk's send_added=False for contexts without a
    module) — an empty, just-created tab has no prim at all yet. set_pin_filter_enabled must
    define one on demand rather than silently no-op."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    assert not stage.GetPrimAtPath("/Tab1").IsValid()

    chunk_usd.set_pin_filter_enabled(ctx, False)
    assert chunk_usd.is_pin_filter_enabled(ctx) is False
    assert stage.GetPrimAtPath("/Tab1").IsValid()

    chunk_usd.set_pin_filter_enabled(ctx, True)
    assert chunk_usd.is_pin_filter_enabled(ctx) is True


def test_pin_filter_enabled_uses_own_prim_path_not_get_chunk_prim_path() -> None:
    """Regression: get_chunk_prim_path(context) double-appends the context's own name for a
    plain tab (ChunkBase.get_context() returns self for a CONTEXT chunk, so
    _resolve_chunk_root resolves to the context's own prim_path, and get_chunk_prim_path then
    appends "/{name}" a second time) — only chunkgroups dodge this via their own prim_path
    short-circuit. The context's own .prim_path must be used directly instead."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    assert chunk_usd.get_chunk_prim_path(ctx) == "/Tab1/Tab1"  # the trap, not what we want
    chunk_usd.set_pin_filter_enabled(ctx, False)
    assert stage.GetPrimAtPath("/Tab1").GetAttribute("filter:pinEnabled").Get() is False
    assert not stage.GetPrimAtPath("/Tab1/Tab1").IsValid()


def test_pin_filter_enabled_works_for_chunkgroup_context_too() -> None:
    from usd_chunks.chunk_flags import ChunkFlags

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    grp = chunk.ChunkBase("mygroup", flags=ChunkFlags.GroupFlags.CONTEXT)
    grp._config = ChunkConfig(module="group")
    scene.add_chunk(grp)

    assert chunk_usd.is_pin_filter_enabled(grp) is True
    chunk_usd.set_pin_filter_enabled(grp, False)
    assert chunk_usd.is_pin_filter_enabled(grp) is False
    assert stage.GetPrimAtPath("/Tab1/mygroup").GetAttribute("filter:pinEnabled").Get() is False


# ---------------------------------------------------------------------------
# generalized gate + live toggle (issue 05)
# ---------------------------------------------------------------------------

def test_pin_filter_on_shows_only_pinned(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c_pinned = _chunk_with_module("pinned_chunk")
    c_unpinned = _chunk_with_module("unpinned_chunk")
    scene.add_chunk(c_pinned)
    scene.add_chunk(c_unpinned)
    chunk_usd.set_chunk_pinned(c_pinned, True)
    chunk_usd.set_chunk_pinned(c_unpinned, False)

    w = ChunkWidget(ctx, is_tab_context=True)
    assert c_pinned.get_hash() in w.group_widgets
    assert c_unpinned.get_hash() not in w.group_widgets


def test_toggling_pin_filter_off_reveals_every_chunk_live(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c_pinned = _chunk_with_module("pinned_chunk")
    c_unpinned = _chunk_with_module("unpinned_chunk")
    scene.add_chunk(c_pinned)
    scene.add_chunk(c_unpinned)
    chunk_usd.set_chunk_pinned(c_pinned, True)
    chunk_usd.set_chunk_pinned(c_unpinned, False)

    w = ChunkWidget(ctx, is_tab_context=True)
    assert c_unpinned.get_hash() not in w.group_widgets

    w._filter_bar.pin_filter_checkbox.setChecked(False)
    assert c_unpinned.get_hash() in w.group_widgets
    assert c_pinned.get_hash() in w.group_widgets  # unaffected
    assert chunk_usd.is_pin_filter_enabled(ctx) is False

    w._filter_bar.pin_filter_checkbox.setChecked(True)
    assert c_unpinned.get_hash() not in w.group_widgets
    assert chunk_usd.is_pin_filter_enabled(ctx) is True


def test_pin_toggle_menu_action_respects_disabled_pin_filter(qapp) -> None:
    """With the pin filter off, explicitly unpinning a chunk via the menu (pin_changed signal)
    must NOT hide it — the filter is off, so it should stay visible regardless of pin state."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget, ViewSlotState

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    chunk_usd.set_chunk_pinned(c, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    vs = ViewSlotState()
    w.set_view_state(vs)
    w._filter_bar.pin_filter_checkbox.setChecked(False)
    assert c.get_hash() in w.group_widgets

    chunk_usd.set_chunk_pinned(c, False)
    vs.pin_changed.emit(c, False)
    assert c.get_hash() in w.group_widgets  # still shown — filter is off


def test_pin_filter_toggle_persists_across_export_reload_cycle(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    chunk_usd.set_pin_filter_enabled(ctx, False)

    usda = stage.GetRootLayer().ExportToString()
    reloaded = Usd.Stage.CreateInMemory()
    reloaded.GetRootLayer().ImportFromString(usda)
    attr = reloaded.GetPrimAtPath("/Tab1").GetAttribute("filter:pinEnabled")
    assert attr.IsValid()
    assert attr.Get() is False


def test_legacy_tab_with_no_filter_attribute_behaves_as_pin_filter_on(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    chunk_usd.set_chunk_pinned(c, False)
    # No filter:pinEnabled ever written for this tab — matches a scene from before issue 05.

    w = ChunkWidget(ctx, is_tab_context=True)
    assert c.get_hash() not in w.group_widgets  # pin filter defaults on, chunk is unpinned


# ---------------------------------------------------------------------------
# fuzzy_matches / chunk_matches_search (issue 06)
# ---------------------------------------------------------------------------

def test_fuzzy_matches_exact_substring() -> None:
    assert chunk_usd.fuzzy_matches("trans", "transform_scale") is True
    assert chunk_usd.fuzzy_matches("VIEW", "View1") is True  # case-insensitive


def test_fuzzy_matches_empty_term_matches_everything() -> None:
    assert chunk_usd.fuzzy_matches("", "anything") is True
    assert chunk_usd.fuzzy_matches("   ", "anything") is True


def test_fuzzy_matches_tolerates_a_small_typo() -> None:
    assert chunk_usd.fuzzy_matches("transfrom", "transform") is True


def test_fuzzy_matches_rejects_unrelated_term() -> None:
    assert chunk_usd.fuzzy_matches("xyz123", "transform_scale") is False


def test_fuzzy_matches_rejects_a_shorter_name_that_merely_shares_a_prefix() -> None:
    """Searching for the longer of two similarly-named chunks returns only that one.

    Two transforms named "haha" and "haha222": searching "haha222" used to bring back
    "haha" as well. Nothing was wrong with the search term -- SequenceMatcher normalises
    by combined length, so the four shared characters carried "haha" to 0.73, over the
    typo threshold. The shorter name is not a misspelling of what was typed.
    """
    assert chunk_usd.fuzzy_matches("haha222", "haha") is False
    assert chunk_usd.fuzzy_matches("haha222", "haha222") is True


def test_fuzzy_matches_still_finds_longer_names_from_a_fragment() -> None:
    """The narrowing above is one-directional: typing a prefix still widens, as before."""
    assert chunk_usd.fuzzy_matches("haha", "haha222") is True


def test_chunk_matches_search_scopes_to_name_or_type() -> None:
    c = _chunk_with_module("transform_a", module="xform")
    assert chunk_usd.chunk_matches_search(c, "xform", scope="type") is True
    assert chunk_usd.chunk_matches_search(c, "xform", scope="name") is False
    assert chunk_usd.chunk_matches_search(c, "transform", scope="type") is False
    assert chunk_usd.chunk_matches_search(c, "transform", scope="name") is True
    assert chunk_usd.chunk_matches_search(c, "xform", scope="both") is True


def test_chunk_matches_search_empty_query_matches_everything() -> None:
    c = _chunk_with_module("anything", module="whatever")
    assert chunk_usd.chunk_matches_search(c, "", scope="name") is True


# ---------------------------------------------------------------------------
# search persistence (issue 06)
# ---------------------------------------------------------------------------

def test_search_query_and_scope_default_when_absent() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    assert chunk_usd.get_search_query(ctx) == ""
    assert chunk_usd.get_search_scope(ctx) == "both"


def test_search_query_and_scope_roundtrip_through_export_reload() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    chunk_usd.set_search_query(ctx, "transform")
    chunk_usd.set_search_scope(ctx, "type")

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
    chunk_usd.set_search_scope(ctx, "bogus")
    assert chunk_usd.get_search_scope(ctx) == "both"


# ---------------------------------------------------------------------------
# search box + scope chips live-filter the Params page (issue 06)
# ---------------------------------------------------------------------------

def test_search_box_live_filters_by_name(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c1 = _chunk_with_module("transform_a", module="xform")
    c2 = _chunk_with_module("light_b", module="light")
    scene.add_chunk(c1)
    scene.add_chunk(c2)
    chunk_usd.set_chunk_pinned(c1, True)
    chunk_usd.set_chunk_pinned(c2, True)

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
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c1 = _chunk_with_module("transform_a", module="xform")
    c2 = _chunk_with_module("light_b", module="light")
    scene.add_chunk(c1)
    scene.add_chunk(c2)
    chunk_usd.set_chunk_pinned(c1, True)
    chunk_usd.set_chunk_pinned(c2, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    _set_scope(w._filter_bar, "type")
    w._filter_bar.search_box.setText("transform_a")  # matches c1's NAME, not its type

    assert c1.get_hash() not in w.group_widgets  # name match doesn't count, scope is type-only
    assert c2.get_hash() not in w.group_widgets


def test_scope_combo_only_ever_has_one_of_three_values(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    assert w._filter_bar.current_scope() == "both"  # default
    for scope in ("name", "type", "both"):
        _set_scope(w._filter_bar, scope)
        assert w._filter_bar.current_scope() == scope


def test_search_filter_ands_with_pin_filter(qapp) -> None:
    """Pin filter on + search: only chunks that are BOTH pinned AND matching show."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c_pinned_match = _chunk_with_module("transform_a", module="xform")
    c_unpinned_match = _chunk_with_module("transform_b", module="xform")
    c_pinned_nomatch = _chunk_with_module("light_a", module="light")
    scene.add_chunk(c_pinned_match)
    scene.add_chunk(c_unpinned_match)
    scene.add_chunk(c_pinned_nomatch)
    chunk_usd.set_chunk_pinned(c_pinned_match, True)
    chunk_usd.set_chunk_pinned(c_unpinned_match, False)
    chunk_usd.set_chunk_pinned(c_pinned_nomatch, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    w._filter_bar.search_box.setText("transform")

    assert c_pinned_match.get_hash() in w.group_widgets
    assert c_unpinned_match.get_hash() not in w.group_widgets  # matches search, but not pinned
    assert c_pinned_nomatch.get_hash() not in w.group_widgets  # pinned, but doesn't match search

    # Turning the pin filter off lets the unpinned match through — search alone now decides.
    w._filter_bar.pin_filter_checkbox.setChecked(False)
    assert c_unpinned_match.get_hash() in w.group_widgets
    assert c_pinned_nomatch.get_hash() not in w.group_widgets  # still filtered out by search


def test_search_state_persists_and_reflects_in_filter_bar_on_rebuild(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

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
# params, e.g. a chunkgroup's "doc" Description field from scripts/chunkgroup.py
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
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget, ParamsFilterBar

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
    """A chunkgroup's own top-level params (e.g. scripts/chunkgroup.py's "doc" Description
    TextEdit) render lofted, above the filter bar — via ChunkWidget.add_widget's existing
    lofted-params branch for CONTEXT chunks, not anything the filter bar itself builds. Both
    live inside the same tinted header panel as the prim path field and the filter bar."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget
    from usd_chunks.chunk_flags import ChunkFlags

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
    """A plain QFrame HLine would be invisible here — CHUNK_STYLESHEET's global
    `QFrame { border: none; }` rule strips the border frame-shape painting relies on — so the
    header panel's tint and the separator both use explicit background styling instead."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

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
    """Both are plain QWidgets with no background of their own by default — they must be
    explicitly tinted to match the header panel rather than showing some other default,
    with WA_StyledBackground set so a bare QWidget actually paints it."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget, _PARAMS_HEADER_BG

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

def test_toggling_the_pin_filter_still_works_after_the_edit_target_moves() -> None:
    """Reported as the pin toggle going dead partway through a session.

    These three setters wrote through the stage's *ambient* edit target, re-resolved on
    every write. An Edit Target chunk moves that target while the graph is live, so a
    toggle made before the move and one made after landed in two different layers -- and
    the older, stronger opinion went on winning. The state stopped responding while the
    widget went on reporting what the user clicked.

    set_chunk_pinned next door had the right shape already: update an existing spec where
    it already lives (chunk_utils.get_attr_home_layer), and only resolve a target for a
    genuinely new one.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    chunk_usd.set_pin_filter_enabled(ctx, False)
    assert chunk_usd.is_pin_filter_enabled(ctx) is False

    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    chunk_usd.set_pin_filter_enabled(ctx, True)

    assert chunk_usd.is_pin_filter_enabled(ctx) is True


def test_the_search_query_still_updates_after_the_edit_target_moves() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    chunk_usd.set_search_query(ctx, "apple")
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    chunk_usd.set_search_query(ctx, "pear")

    assert chunk_usd.get_search_query(ctx) == "pear"


def test_the_search_scope_still_updates_after_the_edit_target_moves() -> None:
    """The third setter of the same shape. Untested in the original report only because
    the scope menu is reached less often than the other two, not because it behaves."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")

    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    chunk_usd.set_search_scope(ctx, "name")
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    chunk_usd.set_search_scope(ctx, "type")

    assert chunk_usd.get_search_scope(ctx) == "type"
