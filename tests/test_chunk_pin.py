from __future__ import annotations

from pxr import Usd, UsdUtils

from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import initialize as _chunk_usd_init
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


def _make_legacy(stage: Usd.Stage, prim_path: str) -> None:
    """Strip chunk:pinned back off a prim to simulate a scene saved before this feature
    shipped -- issue 03 means every *newly* created chunk now gets the attribute explicitly
    authored (true or false), so 'absent' no longer occurs naturally in a fresh test chunk.

    Usd.Prim.RemoveProperty() only removes a spec from the stage's *ambient* edit
    target (the root layer by default for a fresh stage) -- chunk:pinned is chunk-prim
    bookkeeping, authored via chunk_utils.get_bookkeeping_layer (session layer, absent
    an active Edit Target chunk), so RemoveProperty would silently no-op here. Search
    every layer directly instead, matching how the production code itself finds a
    chunk-prim spec wherever it actually lives (e.g. ChunkStageSync._handle_rename).
    """
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        spec = layer.GetPrimAtPath(prim_path)
        if spec and "chunk:pinned" in spec.properties:
            del spec.properties["chunk:pinned"]


# ---------------------------------------------------------------------------
# persistence (ADR-0003)
# ---------------------------------------------------------------------------

def test_is_chunk_pinned_defaults_false_when_attribute_absent() -> None:
    """Absent means unpinned. Pinning is the exception a user asks for, so it is the only
    state worth authoring -- the alternative wrote chunk:pinned = 0 onto every chunk prim
    in every scene to say nothing at all."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    _make_legacy(stage, "/Tab1/c1")

    assert chunk_stage.is_chunk_pinned(c) is False


def test_set_chunk_pinned_roundtrips_through_usd_attribute() -> None:
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)

    chunk_stage.set_chunk_pinned(c, True)
    assert chunk_stage.is_chunk_pinned(c) is True
    assert stage.GetPrimAtPath("/Tab1/c1").GetAttribute("chunk:pinned").Get() is True

    chunk_stage.set_chunk_pinned(c, False)
    assert chunk_stage.is_chunk_pinned(c) is False
    assert not stage.GetPrimAtPath("/Tab1/c1").GetAttribute("chunk:pinned").IsValid(), (
        "unpinning removes the opinion rather than writing a false one"
    )


def test_pinned_true_survives_a_real_export_reload_cycle() -> None:
    """The actual save/reload path: export the *composed* stage to a .usda string and
    open a fresh stage from it, rather than reading the same in-memory attribute back.

    Chunk-prim bookkeeping (chunk:pinned included) is authored via
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
    chunk_stage.set_chunk_pinned(c, True)

    usda = UsdUtils.FlattenLayerStack(stage).ExportToString()

    reloaded = Usd.Stage.CreateInMemory()
    reloaded.GetRootLayer().ImportFromString(usda)
    attr = reloaded.GetPrimAtPath("/Tab1/c1").GetAttribute("chunk:pinned")
    assert attr.IsValid()
    assert attr.Get() is True


# ---------------------------------------------------------------------------
# Params-page filtering + live toggle (issue 02)
# ---------------------------------------------------------------------------

def test_params_page_filters_unpinned_chunks(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget, ViewSlotState

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c_pinned = _chunk_with_module("pinned_chunk")
    c_unpinned = _chunk_with_module("unpinned_chunk")
    scene.add_chunk(c_pinned)
    scene.add_chunk(c_unpinned)
    chunk_stage.set_chunk_pinned(c_pinned, True)
    chunk_stage.set_chunk_pinned(c_unpinned, False)

    w = ChunkWidget(ctx, is_tab_context=True)
    vs = ViewSlotState()
    w.set_view_state(vs)

    assert c_pinned.get_hash() in w.group_widgets
    assert c_unpinned.get_hash() not in w.group_widgets


def test_pin_changed_signal_shows_and_hides_live(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget, ViewSlotState

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("c1")
    scene.add_chunk(c)
    chunk_stage.set_chunk_pinned(c, True)

    w = ChunkWidget(ctx, is_tab_context=True)
    vs = ViewSlotState()
    w.set_view_state(vs)
    assert c.get_hash() in w.group_widgets

    # Unpin: widget must be removed live, without a rebuild.
    chunk_stage.set_chunk_pinned(c, False)
    vs.pin_changed.emit(c, False)
    assert c.get_hash() not in w.group_widgets

    # Re-pin: widget must reappear live.
    chunk_stage.set_chunk_pinned(c, True)
    vs.pin_changed.emit(c, True)
    assert c.get_hash() in w.group_widgets


def test_a_chunk_with_no_pin_attribute_is_filtered_out(qapp) -> None:
    """Absent means unpinned, so a chunk saved before pinning existed opens with an
    unfiltered Params page rather than everything pinned."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    c = _chunk_with_module("legacy_chunk")
    scene.add_chunk(c)
    _make_legacy(stage, "/Tab1/legacy_chunk")

    assert not stage.GetPrimAtPath("/Tab1/legacy_chunk").GetAttribute("chunk:pinned").IsValid()

    w = ChunkWidget(ctx, is_tab_context=True)
    assert c.get_hash() not in w.group_widgets


# ---------------------------------------------------------------------------
# auto-pin by creation source (issue 03)
# ---------------------------------------------------------------------------

def test_new_chunk_defaults_to_not_pinned() -> None:
    """A chunk created with no auto-pin flag set (the Graph-page create menu's baseline)
    authors nothing at all: unpinned is the default, and every line in the file should
    carry information."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    c = _chunk_with_module("graph_created")
    scene.add_chunk(c)

    attr = stage.GetPrimAtPath("/Tab1/graph_created").GetAttribute("chunk:pinned")
    assert not attr.IsValid(), "the default state writes nothing"
    assert chunk_stage.is_chunk_pinned(c) is False


def test_pin_new_chunks_flag_marks_creation_as_pinned() -> None:
    """The Params-tab create menu (and a Params-surface paste) sets _pin_new_chunks around
    the create call -- mirrors _params_tab_create's existing flag-around-the-call pattern."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")

    chunk_stage._pin_new_chunks = True
    try:
        c = _chunk_with_module("params_created")
        scene.add_chunk(c)
    finally:
        chunk_stage._pin_new_chunks = None

    assert chunk_stage.is_chunk_pinned(c) is True
    # The flag must not leak into chunks created afterwards.
    c2 = _chunk_with_module("later_graph_created")
    scene.add_chunk(c2)
    assert chunk_stage.is_chunk_pinned(c2) is False


# ---------------------------------------------------------------------------
# PARAM_ADDED listener race (found after shipping issue 03 -- real usdview testing)
# ---------------------------------------------------------------------------
#
# The Params-page widget and ChunkStageSync both react to the same PARAM_ADDED notice for a
# newly-created chunk. Delivery order between two independent listeners on one notice isn't
# guaranteed, and in the real app the widget's listener can run BEFORE ChunkStageSync's --
# meaning the widget can call is_chunk_pinned() before _define_prim has authored chunk:pinned
# at all, read "absent", and fall back to the legacy pinned=true default. Nothing re-checks
# after _define_prim's own (correct) write lands moments later, so the chunk stays visible in
# Params despite ending up correctly unpinned on the stage. These tests build the tab's
# ChunkWidget FIRST, then add the new chunk live -- the ordering that actually exercises this,
# unlike test_params_page_filters_unpinned_chunks above which builds the widget after the chunk
# already exists (no race possible there, which is why it didn't catch this).

def test_graph_surface_flag_resolves_the_race_to_unpinned(qapp) -> None:
    """With _pin_new_chunks explicitly set False (what ChunkGraphView.contextMenuEvent now
    does), a racing widget-listener must resolve to unpinned, not the legacy True default."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    chunk_stage._pin_new_chunks = False
    try:
        c = _chunk_with_module("graph_created_explicit")
        scene.add_chunk(c)
    finally:
        chunk_stage._pin_new_chunks = None

    assert c.get_hash() not in w.group_widgets


def test_params_surface_flag_resolves_the_race_to_pinned(qapp) -> None:
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    chunk_stage._pin_new_chunks = True
    try:
        c = _chunk_with_module("params_created")
        scene.add_chunk(c)
    finally:
        chunk_stage._pin_new_chunks = None

    assert c.get_hash() in w.group_widgets


def test_view_chunk_never_defaults_to_pinned(qapp) -> None:
    """Views are graph-wiring utility nodes -- never auto-pinned, regardless of which surface
    (or none at all, e.g. _ensure_default_view's standalone auto-created default view) created
    them, and regardless of the listener race."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    w = ChunkWidget(ctx, is_tab_context=True)

    # No flag set at all -- matches _ensure_default_view's auto-created view, which doesn't
    # (and per this test, doesn't need to) touch _pin_new_chunks.
    v = chunk.Chunk("View1")
    v._config = ChunkConfig(module="view")
    scene.add_chunk(v)

    assert not stage.GetPrimAtPath("/Tab1/View1").GetAttribute("chunk:pinned").IsValid()
    assert chunk_stage.is_chunk_pinned(v) is False
    assert v.get_hash() not in w.group_widgets

    # Even a genuinely legacy view (attribute stripped, as if saved before this feature
    # shipped) must not fall back to the migration pinned=true default.
    _make_legacy(stage, "/Tab1/View1")
    assert chunk_stage.is_chunk_pinned(v) is False


def test_view_chunk_pinned_still_overridable_via_explicit_menu_action(qapp) -> None:
    """The unconditional view default doesn't prevent someone from explicitly pinning a view
    via the same Pin/Unpin menu action used for any other chunk."""
    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    scene.add_context("Tab1")
    v = chunk.Chunk("View1")
    v._config = ChunkConfig(module="view")
    scene.add_chunk(v)

    chunk_stage.set_chunk_pinned(v, True)
    assert chunk_stage.is_chunk_pinned(v) is True


def test_chunkgroup_member_created_via_graph_not_shown_in_dive_in_params(qapp) -> None:
    """Members created inside a chunkgroup's own dive-in tab go through the exact same
    ChunkGraphView.contextMenuEvent as a top-level tab, so they hit the identical race -- this
    confirms the fix covers a chunkgroup's own Params page too, not just a top-level tab's."""
    from usd_chunks.plugins.chunk_qt_widgets import ChunkWidget
    from usd_chunks.chunk_flags import ChunkFlags
    import weakref

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
        chunk_stage._pin_new_chunks = False  # what ChunkGraphView.contextMenuEvent sets now
        try:
            member = _chunk_with_module("member1")
            scene.add_chunk(member)
        finally:
            chunk_stage._pin_new_chunks = None
    finally:
        scene._current_context = prev_ctx

    assert member.get_context() is grp
    assert member.get_hash() not in w.group_widgets


def test_focused_view_is_graph_detects_graph_page_ancestor(qapp) -> None:
    from usd_chunks.plugins.chunk_qt import _focused_view_is_graph
    from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView
    from PySide6 import QtWidgets

    plain = QtWidgets.QWidget()
    assert _focused_view_is_graph(plain) is False
    assert _focused_view_is_graph(None) is False

    graph_view = ChunkGraphView()
    child = QtWidgets.QWidget(graph_view)
    assert _focused_view_is_graph(child) is True
    assert _focused_view_is_graph(graph_view) is True
