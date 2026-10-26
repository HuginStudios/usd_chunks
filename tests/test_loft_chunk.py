"""Loft up chunk: a chunk lofted to its group shows on the group's Params page; nothing else does."""
from __future__ import annotations

import weakref
from pxr import Sdf, Usd, UsdUtils

from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from PySide6 import QtWidgets
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.frontends.qt.factory import _focused_view_is_graph
from usd_chunks.frontends.qt.nodegraph import ChunkGraphView
from usd_chunks.frontends.qt.widgets import ChunkWidget, ViewSlotState

from chunk_stubs import attach_stub_module
from tests.test_unloft import env  # noqa: F401 -- a fixture


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


def _make_legacy(stage: Usd.Stage, prim_path: str) -> None:
    """Strip chunk:lofted back off a prim to simulate a scene saved before this feature
    shipped -- issue 03 means every *newly* created chunk now gets the attribute explicitly
    authored (true or false), so 'absent' no longer occurs naturally in a fresh test chunk.

    Usd.Prim.RemoveProperty() only removes a spec from the stage's *ambient* edit
    target (the root layer by default for a fresh stage) -- chunk:lofted is chunk-prim
    bookkeeping, authored via chunk_utils.get_bookkeeping_layer (session layer, absent
    an active Edit Target chunk), so RemoveProperty would silently no-op here. Search
    every layer directly instead, matching how the production code itself finds a
    chunk-prim spec wherever it actually lives (e.g. ChunkStageSync._handle_rename).
    """
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        spec = layer.GetPrimAtPath(prim_path)
        if spec and "chunk:lofted" in spec.properties:
            del spec.properties["chunk:lofted"]


# ---------------------------------------------------------------------------
# persistence (ADR-0003)
# ---------------------------------------------------------------------------

def test_is_chunk_lofted_defaults_false_when_attribute_absent() -> None:
    """Absent means not lofted. Lofting is the exception a user asks for, so it is the only
    state worth authoring -- the alternative wrote chunk:lofted = 0 onto every chunk prim
    in every scene to say nothing at all."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    _make_legacy(stage, "/Tab1/c1")

    assert chunk_stage.is_chunk_lofted(c) is False


def test_set_chunk_lofted_roundtrips_through_usd_attribute() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)

    chunk_stage.set_chunk_lofted(c, True)
    assert chunk_stage.is_chunk_lofted(c) is True
    assert stage.GetPrimAtPath("/Tab1/c1").GetAttribute("chunk:lofted").Get() is True

    chunk_stage.set_chunk_lofted(c, False)
    assert chunk_stage.is_chunk_lofted(c) is False
    assert not stage.GetPrimAtPath("/Tab1/c1").GetAttribute("chunk:lofted").IsValid(), (
        "unlofting removes the opinion rather than writing a false one"
    )


def test_lofted_survives_a_real_export_reload_cycle() -> None:
    """The actual save/reload path: export the *composed* stage to a .usda string and
    open a fresh stage from it, rather than reading the same in-memory attribute back.

    Chunk-prim bookkeeping (chunk:lofted included) is authored via
    chunk_utils.get_bookkeeping_layer -- the session layer, absent an active Edit
    Target chunk -- not the root layer. stage.GetRootLayer().ExportToString() alone
    would only ever capture root-layer content, missing it entirely; flatten the full
    layer stack first so this genuinely tests "the composed result survives a
    save/reload", regardless of which real layer bookkeeping happens to live in.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    chunk_stage.set_chunk_lofted(c, True)

    usda = UsdUtils.FlattenLayerStack(stage).ExportToString()

    reloaded = Usd.Stage.CreateInMemory()
    reloaded.GetRootLayer().ImportFromString(usda)
    attr = reloaded.GetPrimAtPath("/Tab1/c1").GetAttribute("chunk:lofted")
    assert attr.IsValid()
    assert attr.Get() is True


# ---------------------------------------------------------------------------
# Params-page filtering + live toggle (issue 02)
# ---------------------------------------------------------------------------

def test_params_page_shows_only_lofted_chunks(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c_lofted = _chunk_with_module("lofted_chunk")
    c_other = _chunk_with_module("other_chunk")
    scene.add_chunk(c_lofted)
    scene.add_chunk(c_other)
    chunk_stage.set_chunk_lofted(c_lofted, True)
    chunk_stage.set_chunk_lofted(c_other, False)

    w = ChunkWidget(ctx, is_tab_context=True)
    vs = ViewSlotState()
    w.set_view_state(vs)

    assert c_lofted.get_hash() in w.group_widgets
    assert c_other.get_hash() not in w.group_widgets


def test_loft_changed_signal_shows_and_hides_live(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    chunk_stage.set_chunk_lofted(c, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    vs = ViewSlotState()
    w.set_view_state(vs)
    assert c.get_hash() in w.group_widgets

    # Unloft: widget must be removed live, without a rebuild.
    chunk_stage.set_chunk_lofted(c, False)
    vs.loft_changed.emit(c, False)
    assert c.get_hash() not in w.group_widgets

    # Re-loft: widget must reappear live.
    chunk_stage.set_chunk_lofted(c, True)
    vs.loft_changed.emit(c, True)
    assert c.get_hash() in w.group_widgets


def test_a_chunk_with_no_loft_attribute_is_not_shown(qapp) -> None:
    """Absent means not lofted, so a chunk saved before lofting existed opens with an
    unfiltered Params page rather than everything lofted."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("legacy_chunk")
    scene.add_chunk(c)
    _make_legacy(stage, "/Tab1/legacy_chunk")

    assert not stage.GetPrimAtPath("/Tab1/legacy_chunk").GetAttribute("chunk:lofted").IsValid()

    w = ChunkWidget(ctx, is_tab_context=True)
    assert c.get_hash() not in w.group_widgets


# ---------------------------------------------------------------------------
# auto-loft by creation source (issue 03)
# ---------------------------------------------------------------------------

def test_new_chunk_defaults_to_not_lofted() -> None:
    """A chunk created with no auto-loft flag set (the Graph-page create menu's baseline)
    authors nothing at all: not lofted is the default, and every line in the file should
    carry information."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("graph_created")
    scene.add_chunk(c)

    attr = stage.GetPrimAtPath("/Tab1/graph_created").GetAttribute("chunk:lofted")
    assert not attr.IsValid(), "the default state writes nothing"
    assert chunk_stage.is_chunk_lofted(c) is False


def test_loft_new_chunks_flag_marks_creation_as_lofted() -> None:
    """The Params-tab create menu (and a Params-surface paste) sets _loft_new_chunks around
    the create call -- mirrors _params_tab_create's existing flag-around-the-call pattern."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")

    chunk_stage._loft_new_chunks = True
    try:
        c = _chunk_with_module("params_created")
        scene.add_chunk(c)
    finally:
        chunk_stage._loft_new_chunks = None

    assert chunk_stage.is_chunk_lofted(c) is True
    # The flag must not leak into chunks created afterwards.
    c2 = _chunk_with_module("later_graph_created")
    scene.add_chunk(c2)
    assert chunk_stage.is_chunk_lofted(c2) is False


# ---------------------------------------------------------------------------
# PARAM_ADDED listener race (found after shipping issue 03 -- real usdview testing)
# ---------------------------------------------------------------------------
#
# The Params-page widget and ChunkStageSync both react to the same PARAM_ADDED notice for a
# newly-created chunk. Delivery order between two independent listeners on one notice isn't
# guaranteed, and in the real app the widget's listener can run BEFORE ChunkStageSync's --
# meaning the widget can call is_chunk_lofted() before _define_prim has authored chunk:lofted
# at all, read "absent", and fall back to the legacy lofted=true default. Nothing re-checks
# after _define_prim's own (correct) write lands moments later, so the chunk stays visible in
# Params despite ending up correctly not lofted on the stage. These tests build the tab's
# ChunkWidget FIRST, then add the new chunk live -- the ordering that actually exercises this,
# unlike test_params_page_shows_only_lofted_chunks above which builds the widget after the chunk
# already exists (no race possible there, which is why it didn't catch this).

def test_graph_surface_flag_resolves_the_race_to_not_lofted(qapp) -> None:
    """With _loft_new_chunks explicitly set False (what ChunkGraphView.contextMenuEvent now
    does), a racing widget-listener must resolve to not lofted, not the legacy True default."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    chunk_stage._loft_new_chunks = False
    try:
        c = _chunk_with_module("graph_created_explicit")
        scene.add_chunk(c)
    finally:
        chunk_stage._loft_new_chunks = None

    assert c.get_hash() not in w.group_widgets


def test_params_surface_flag_resolves_the_race_to_lofted(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    chunk_stage._loft_new_chunks = True
    try:
        c = _chunk_with_module("params_created")
        scene.add_chunk(c)
    finally:
        chunk_stage._loft_new_chunks = None

    assert c.get_hash() in w.group_widgets


def test_view_chunk_never_defaults_to_lofted(qapp) -> None:
    """Views are graph-wiring utility nodes -- never lofted on creation, regardless of which surface
    (or none at all, e.g. _ensure_default_view's standalone auto-created default view) created
    them, and regardless of the listener race."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    # No flag set at all -- matches _ensure_default_view's auto-created view, which doesn't
    # (and per this test, doesn't need to) touch _loft_new_chunks.
    v = chunk.Chunk("View1")
    v._config = ChunkConfig(module="view")
    scene.add_chunk(v)

    assert not stage.GetPrimAtPath("/Tab1/View1").GetAttribute("chunk:lofted").IsValid()
    assert chunk_stage.is_chunk_lofted(v) is False
    assert v.get_hash() not in w.group_widgets

    # Even a genuinely legacy view (attribute stripped, as if saved before this feature
    # shipped) must not fall back to the migration lofted=true default.
    _make_legacy(stage, "/Tab1/View1")
    assert chunk_stage.is_chunk_lofted(v) is False


def test_view_chunk_still_loftable_via_explicit_menu_action(qapp) -> None:
    """The unconditional view default doesn't prevent someone from explicitly lofting a view
    via the same Loft up chunk / Unloft chunk menu action used for any other chunk."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    v = chunk.Chunk("View1")
    v._config = ChunkConfig(module="view")
    scene.add_chunk(v)

    chunk_stage.set_chunk_lofted(v, True)
    assert chunk_stage.is_chunk_lofted(v) is True


def test_chunkgroup_member_created_via_graph_not_shown_in_dive_in_params(qapp) -> None:
    """Members created inside a chunkgroup's own dive-in tab go through the exact same
    ChunkGraphView.contextMenuEvent as a top-level tab, so they hit the identical race -- this
    confirms the fix covers a chunkgroup's own Params page too, not just a top-level tab's."""

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")

    grp = chunk.ChunkBase("mygroup", flags=ChunkFlags.GroupFlags.CONTEXT)
    grp._config = ChunkConfig(module="group")
    scene.add_chunk(grp)

    # Dive-in tab's own ChunkWidget, already open -- matches the real app.
    w = ChunkWidget(grp, is_tab_context=True)

    prev_ctx = scene._current_context
    scene._current_context = weakref.ref(grp)
    try:
        chunk_stage._loft_new_chunks = False  # what ChunkGraphView.contextMenuEvent sets now
        try:
            member = _chunk_with_module("member1")
            scene.add_chunk(member)
        finally:
            chunk_stage._loft_new_chunks = None
    finally:
        scene._current_context = prev_ctx

    assert member.get_context() is grp
    assert member.get_hash() not in w.group_widgets


def test_focused_view_is_graph_detects_graph_page_ancestor(qapp) -> None:
    plain = QtWidgets.QWidget()
    assert _focused_view_is_graph(plain) is False
    assert _focused_view_is_graph(None) is False

    graph_view = ChunkGraphView()
    child = QtWidgets.QWidget(graph_view)
    assert _focused_view_is_graph(child) is True
    assert _focused_view_is_graph(graph_view) is True


# ---------------------------------------------------------------------------
# replacing pinning
# ---------------------------------------------------------------------------

def test_a_chunk_pinned_in_an_older_file_opens_lofted() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("old")
    scene.add_chunk(c)
    prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(c))
    prim.CreateAttribute("chunk:pinned", Sdf.ValueTypeNames.Bool, custom=True).Set(True)

    assert chunk_stage.is_chunk_lofted(c)

    # Lofting writes the new name and drops the old one; unlofting leaves neither.
    chunk_stage.set_chunk_lofted(c, True)
    assert not prim.GetAttribute("chunk:pinned") and prim.GetAttribute("chunk:lofted").Get()
    chunk_stage.set_chunk_lofted(c, False)
    assert not prim.GetAttribute("chunk:pinned") and not prim.GetAttribute("chunk:lofted")


def test_the_search_bar_has_no_pin_filter(qapp) -> None:
    from usd_chunks.frontends.qt.widgets import ParamsFilterBar

    bar = ParamsFilterBar()
    assert not [b for b in bar.findChildren(QtWidgets.QCheckBox)]


def _in_box(page: ChunkWidget, c) -> bool:
    """Whether c's panel sits in the page's group box, above the search bar."""
    widget = page.group_widgets.get(c.get_hash())
    return widget is not None and page._lofted_container.isAncestorOf(widget)


def test_a_lofted_chunk_sits_in_the_groups_box_and_a_search_match_below(qapp) -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    lofted = _chunk_with_module("mesh_a", module="mesh")
    other = _chunk_with_module("mesh_b", module="mesh")
    scene.add_chunk(lofted)
    scene.add_chunk(other)
    chunk_stage.set_chunk_lofted(lofted, True)
    page = ChunkWidget(ctx, is_tab_context=True)
    page.set_view_state(ViewSlotState())

    assert _in_box(page, lofted)

    page._filter_bar.search_box.setText("mesh")
    assert _in_box(page, lofted)
    assert other.get_hash() in page.group_widgets and not _in_box(page, other)

    # Lofting the match moves it up into the box, and unlofting moves it back down.
    chunk_stage.set_chunk_lofted(other, True)
    ChunkWidget.chunk_loft_changed(other, True)
    assert _in_box(page, other)
    chunk_stage.set_chunk_lofted(other, False)
    ChunkWidget.chunk_loft_changed(other, False)
    assert other.get_hash() in page.group_widgets and not _in_box(page, other)


def test_the_groups_box_scrolls_with_the_page(qapp) -> None:
    """A lofted chunk can be tall: outside the scroll area it would push the page past the dock."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    page = ChunkWidget(ctx, is_tab_context=True)

    assert page.content.widget().isAncestorOf(page._header_container)


def test_inside_a_group_its_lofted_chunk_sits_in_its_box(env) -> None:  # noqa: F811
    group = env["mk"]("chunkgroup", name="Group")
    env["settle"]()
    inner = env["mk"]("mesh", context=group)
    env["settle"]()
    page = ChunkWidget(group, is_tab_context=True)
    page.set_view_state(ViewSlotState())
    page.show()
    env["settle"]()
    assert not _in_box(page, inner)

    chunk_stage.set_chunk_lofted(inner, True)
    ChunkWidget.chunk_loft_changed(inner, True)  # what the menu does
    env["settle"]()
    assert _in_box(page, inner)


def test_a_groups_section_shows_the_chunks_lofted_into_it(env) -> None:  # noqa: F811
    group = env["mk"]("chunkgroup", name="Group")
    env["settle"]()
    inner = env["mk"]("transform", context=group)
    env["settle"]()
    page = ChunkWidget(chunk_stage.owning_group(group), is_tab_context=True)
    view_state = ViewSlotState()
    page.set_view_state(view_state)
    page.show()
    env["settle"]()
    chunk_stage.set_chunk_lofted(inner, True)  # the Transform, into the group's section
    # The group onto the page above, as the menu does it.
    chunk_stage.set_chunk_lofted(group, True)
    view_state.loft_changed.emit(group, True)
    env["settle"]()
    section = page.group_widgets[group.get_hash()]

    assert inner.get_hash() in section.group_widgets

    chunk_stage.set_chunk_lofted(inner, False)
    ChunkWidget.chunk_loft_changed(inner, False)  # what the menu does after it
    env["settle"]()
    assert inner.get_hash() not in section.group_widgets
