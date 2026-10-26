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
    ParamsSearchPanel,
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

def _found(w: ChunkWidget) -> set:
    """The chunks the page's search box shows, by hash."""
    return set(w._search_panel.results.panels())


def _boxed(w: ChunkWidget) -> set:
    """The chunks in the page's group box, by hash."""
    return set(w._lofted_chunks.panels())


def test_search_box_live_filters_by_name(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c1 = _chunk_with_module("transform_a", module="xform")
    c2 = _chunk_with_module("light_b", module="lamp")
    scene.add_chunk(c1)
    scene.add_chunk(c2)

    w = ChunkWidget(ctx, is_tab_context=True)
    assert _found(w) == set()

    w._filter_bar.search_box.setText("transform")
    assert _found(w) == {c1.get_hash()}

    w._filter_bar.search_box.setText("")
    assert _found(w) == set()


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

    w = ChunkWidget(ctx, is_tab_context=True)
    _set_scope(w._filter_bar, "type")
    w._filter_bar.search_box.setText("transform_a")  # matches c1's NAME, not its type
    assert _found(w) == set()  # name match doesn't count, scope is type-only

    w._filter_bar.search_box.setText("xform")
    assert _found(w) == {c1.get_hash()}


def test_scope_combo_only_ever_has_one_of_three_values(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    assert w._filter_bar.current_scope() == "both"  # default
    for scope in ("name", "type", "both"):
        _set_scope(w._filter_bar, scope)
        assert w._filter_bar.current_scope() == scope


def test_a_search_leaves_the_groups_box_alone(qapp) -> None:
    """Two meshes and a Transform, one mesh and the Transform lofted, then a search for "me":
    the Transform stayed in the group's box. The search shows only the other mesh."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    mesh1 = _chunk_with_module("mesh1", module="mesh")
    mesh2 = _chunk_with_module("mesh2", module="mesh")
    xform = _chunk_with_module("transform", module="transform")
    for c in (mesh1, mesh2, xform):
        scene.add_chunk(c)
    chunk_stage.set_chunk_lofted(mesh1, True)
    chunk_stage.set_chunk_lofted(xform, True)
    w = ChunkWidget(ctx, is_tab_context=True)
    lofted = {mesh1.get_hash(), xform.get_hash()}
    assert _boxed(w) == lofted

    w._filter_bar.search_box.setText("me")
    assert _boxed(w) == lofted
    assert _found(w) == {mesh2.get_hash()}  # the lofted mesh is already in the box
    assert w._search_panel.status.text() == "1 found, plus 1 lofted above"

    w._filter_bar.search_box.setText("zzz")
    assert _boxed(w) == lofted
    assert _found(w) == set()
    assert w._search_panel.status.text() == "Nothing found"

    w._filter_bar.search_box.setText("")
    assert _boxed(w) == lofted
    assert _found(w) == set()


def test_the_search_line_says_what_it_found() -> None:
    text = ParamsSearchPanel.status_text
    assert text("", 0, 0) == "Type to find any chunk in this graph."
    assert text("me", 2, 0) == "2 found"
    assert text("me", 2, 1) == "2 found, plus 1 lofted above"
    assert text("me", 0, 1) == "Nothing else found: 1 lofted above"
    assert text("me", 0, 0) == "Nothing found"


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
# page layout: the group's box (title, prim path, lofted params, lofted chunks), then the
# search box (filter bar, a status line, what it found)
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


def test_the_page_is_the_groups_box_then_the_search_box(qapp) -> None:
    """Two boxes: the group's (title, prim path, lofted params and chunks), then the search."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    assert w.content_layout.itemAt(0).widget() is w._group_box
    assert w.content_layout.itemAt(1).widget() is w._search_panel
    assert w.content_layout.count() == 2  # chunks live in the two boxes, not beside them
    for part in (w.minimize_button, w._prim_path_field, w._lofted_container, w._lofted_chunks):
        assert w._group_box.isAncestorOf(part)
    assert w._search_panel.isAncestorOf(w._filter_bar)
    assert not w._group_box.isAncestorOf(w._filter_bar)
    assert isinstance(w._filter_bar, ParamsFilterBar)
    assert w._lofted_container.isVisible() is False  # plain tabs have no own params to loft


def test_a_chunkgroups_description_sits_in_its_box_above_its_lofted_chunks(qapp) -> None:
    """A chunkgroup's own top-level params (e.g. chunks/core/chunkgroup.py's "doc" Description
    TextEdit) render lofted, via ChunkWidget.add_widget's lofted-params branch for CONTEXT
    chunks, in the group's box above the chunks lofted to it."""

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
    assert w._lofted_layout.count() == 1  # the "doc" field landed here
    body = _layout_widgets(w._group_box_body.layout())
    assert body.index(w._prim_path_field) < body.index(w._lofted_container) < body.index(w._lofted_chunks)


def test_the_two_boxes_look_apart(qapp) -> None:
    """CHUNK_STYLESHEET's global `QFrame { border: none; }` strips plain frame borders, so each
    box sets its own background and edge, selected by object name."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    for box in (w._group_box, w._search_panel):
        assert f"#{box.objectName()}" in box.styleSheet()
        assert "border:" in box.styleSheet()
    assert _PARAMS_HEADER_BG in w._group_box.styleSheet()
    assert _PARAMS_HEADER_BG not in w._search_panel.styleSheet()


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
