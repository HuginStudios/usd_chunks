from __future__ import annotations

import time

from pxr import Sdf, Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.plugins import chunk_usd
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _wire(dst, src):
    # Wire through the dependency API alone and let ChunkStageSync's listener author
    # inputs:chunk into the chunk-prim bookkeeping (session) layer, where the live graph's own
    # wiring lands. Hand-authoring the connection first goes to the stage's AMBIENT edit target
    # -- the root layer -- and is doubly wrong: the listener then sees the connection already
    # present and skips its own session-layer write, and the root-layer opinion is later
    # dropped, leaving inputs:chunk with no connections at all and the exec chain reading as
    # unwired nodes.
    dst.add_dependency(src)


def _setup(qapp):
    """Common scaffolding: a real graph widget with an active view wired straight to a
    reactive Scatter chunk, so Scatter is unambiguously part of the view's cook set."""
    from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    scatter_mod = reg.get_chunk_module("scatter_points")
    view_mod = reg.get_chunk_module("view")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter = _mk(scatter_mod, scene)
    qapp.processEvents()
    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 1

    gv = docker.tab_widget.get_current_graph_view()
    context = gv._graph_context()
    view = _mk(view_mod, scene)
    _wire(view, scatter)
    qapp.processEvents()
    gv._activate_menu_view(view, context, stage)

    def _pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()

    _pump(0.5)
    assert gv._view_state.active_slot, "view must be properly activated for this repro to be faithful"

    # docker has no Qt parent -- the caller must keep a reference to it alive for the rest of
    # the test, or Python GC'ing it mid-test destroys the whole widget tree (gv included) and
    # any later call into gv raises "Internal C++ object already deleted".
    return docker, scene, reg, stage, gv, scatter, view, _pump


def test_unrelated_chunk_create_and_delete_do_not_recook(qapp) -> None:
    """Regression reported live in usdview: creating or deleting a chunk with no relation to
    the active view's upstream tree still recooked -- re-executing every chunk in the view's
    cook set for real (a stray "Scattered N instances" re-print from a totally unrelated
    create/delete) and re-muting/dimming every other chunk (looked like "everything
    collapses"). Uses ChunkGraphView._delete_selected() (select + Delete, the real UI path) --
    not a direct call into _on_chunk_deleted -- because the relevance judgment now happens
    THERE, before request_delete() runs: request_delete() force-unblocks the chunk as a
    prerequisite for its own CHUNK_DELETED to fire at all, so by the time the notice reaches
    _on_chunk_deleted, is_blocked() no longer reflects whether the chunk was ever really
    cooking -- calling the notice handler directly (as an earlier version of this test did)
    skips the mutation and silently stops testing anything real. _delete_selected now judges
    relevance from live graph reachability, captured before any deletion starts.
    """
    docker, scene, reg, stage, gv, scatter, view, _pump = _setup(qapp)
    transform_mod = reg.get_chunk_module("transform")

    # The module object the reactive dispatch actually calls execute() on -- not
    # importlib.import_module("scripts.modelling.scatter_points"), which can resolve to a different
    # module object than the registry's own plugin-loaded one.
    real_scatter_module = reg.get_chunk_module("scatter_points").module
    call_count = {"n": 0}
    orig_execute = real_scatter_module.execute

    def traced_execute(node):
        call_count["n"] += 1
        return orig_execute(node)

    real_scatter_module.execute = traced_execute
    try:
        # Create a totally unrelated, disconnected chunk -- not wired to anything.
        stray = _mk(transform_mod, scene)
        qapp.processEvents()
        _pump(0.3)
        assert call_count["n"] == 0, (
            "creating an unrelated, disconnected chunk must not re-execute the active view's "
            "cook set"
        )

        # Delete it again -- still unrelated to the view's tree. Select + delete, the real path.
        # _activate_menu_view (in _setup) leaves the VIEW chunk itself selected as its own
        # bookkeeping -- clear that first, or _delete_selected() would delete the view too,
        # which would trivially zero out call_count regardless of whether this fix works
        # (no active view means nothing cooks, full stop).
        view.set_selected(False)
        stray.set_selected(True)
        gv._delete_selected()
        qapp.processEvents()
        _pump(0.3)
        assert call_count["n"] == 0, (
            "deleting an unrelated chunk must not re-execute the active view's cook set"
        )
        assert gv._view_state.active_slot, "the view itself must still be intact after this delete"
    finally:
        real_scatter_module.execute = orig_execute


def test_unrelated_chunk_removed_via_right_click_menu_does_not_recook(qapp) -> None:
    """Regression reported live in usdview, distinct from the Delete-key path above: every
    chunk registers a default "remove" MENU_ACTION_NOTICE callback (ChunkBase.__attrs_post_init__
    in chunk_core.py) that calls request_delete() directly -- the right-click "remove" menu
    item, dispatched from chunk_qt_widgets.show_chunk_menu, never goes anywhere near
    ChunkGraphView._delete_selected(). It was still recooking (and re-executing Scatter) for a
    totally disconnected node, because the relevance capture only ever ran for the Delete-key
    path. show_chunk_menu now calls ChunkGraphView.mark_pending_delete_relevance([grp]) itself,
    right before dispatching the "remove" callback -- this test drives that exact sequence
    without needing a real QMenu popup.
    """
    docker, scene, reg, stage, gv, scatter, view, _pump = _setup(qapp)
    transform_mod = reg.get_chunk_module("transform")

    from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView

    real_scatter_module = reg.get_chunk_module("scatter_points").module
    call_count = {"n": 0}
    orig_execute = real_scatter_module.execute

    def traced_execute(node):
        call_count["n"] += 1
        return orig_execute(node)

    real_scatter_module.execute = traced_execute
    try:
        stray = _mk(transform_mod, scene)
        qapp.processEvents()
        _pump(0.3)
        assert call_count["n"] == 0, "creating the unrelated chunk must not recook"

        # Exactly what show_chunk_menu does for the "remove" action: capture relevance, then
        # request_delete() directly -- no selection, no _delete_selected() involved at all.
        ChunkGraphView.mark_pending_delete_relevance([stray])
        stray.request_delete()
        qapp.processEvents()
        _pump(0.3)
        assert call_count["n"] == 0, (
            "removing an unrelated chunk via the right-click 'remove' menu action must not "
            "re-execute the active view's cook set"
        )
        assert gv._view_state.active_slot, "the view itself must still be intact after this delete"
    finally:
        real_scatter_module.execute = orig_execute


def test_relevant_chunk_delete_still_recooks(qapp) -> None:
    """Companion sanity check: deleting a chunk that IS part of the active view's upstream tree
    must still trigger a real recook (chain-heal + re-execute) -- the scoping optimization above
    must not silently swallow a genuine cook-set change."""
    docker, scene, reg, stage, gv, scatter, view, _pump = _setup(qapp)
    transform_mod = reg.get_chunk_module("transform")

    # Insert Transform between Scatter and the view: Scatter -> Transform -> View.
    transform = _mk(transform_mod, scene)
    qapp.processEvents()
    _wire(transform, scatter)
    _wire(view, transform)
    qapp.processEvents()
    gv._recook_active_view()
    _pump(0.5)

    real_scatter_module = reg.get_chunk_module("scatter_points").module
    call_count = {"n": 0}
    orig_execute = real_scatter_module.execute

    def traced_execute(node):
        call_count["n"] += 1
        return orig_execute(node)

    real_scatter_module.execute = traced_execute
    try:
        assert not transform.is_blocked(), "setup must leave transform genuinely cooking"
        # _recook_active_view() above leaves the VIEW chunk selected (its own bookkeeping) --
        # clear that first so _delete_selected() deletes only transform, not the view too.
        view.set_selected(False)
        transform.set_selected(True)
        gv._delete_selected()
        qapp.processEvents()
        _pump(0.5)
        assert call_count["n"] > 0, (
            "deleting a chunk that WAS part of the active view's tree must still trigger a real "
            "recook -- the unrelated-change optimization must not suppress this"
        )
    finally:
        real_scatter_module.execute = orig_execute


def test_bulk_chunk_creation_burst_recooks_once(qapp) -> None:
    """Regression: a bulk file load fires CHUNK_LOADED once per chunk, synchronously and in
    one call stack (see chunk_usd.load_chunks' _add_prim_mods). ChunkWidgetDocker rebuilt the
    graph tab on every single CHUNK_LOADED, and each rebuild's post-build step (build()'s own
    100ms-later recook) re-ran chunk_usd.recook_for_view -- which re-executes its whole cook
    set every time it runs, not just what changed -- so an N-chunk load re-ran the entire
    upstream chain N times. Live-reported as repeated "Updated stage with sublayer" /
    "Scattered N instances" spam on every reload of a saved scene.
    ChunkGraphView._schedule_restore_active_view now debounces that post-build recook (via a
    generation token, same idea as ChunkGraphScene._on_chunk_deleted's existing debounce for a
    CHUNK_DELETED burst) so every build() call that lands in the same synchronous burst
    collapses into a single recook instead of one per call -- scene.build() itself (the actual
    node/widget creation) stays synchronous and undebounced, since several callers rely on a
    just-created node existing in scene()._nodes immediately.

    Counts chunk_usd.recook_for_view calls specifically, not a downstream chunk's own
    execute() -- wiring a new chunk into an already-cooking chain also triggers a real,
    separate DAG-level reactive execute() (chunk_notice's own dependency-changed dispatch,
    nothing to do with view-driven recook), which would conflate the two mechanisms and make
    this assert too strict.
    """
    docker, scene, reg, stage, gv, scatter, view, _pump = _setup(qapp)
    transform_mod = reg.get_chunk_module("transform")

    call_count = {"n": 0}
    orig_recook = chunk_usd.recook_for_view

    def traced_recook(*a, **kw):
        call_count["n"] += 1
        return orig_recook(*a, **kw)

    chunk_usd.recook_for_view = traced_recook
    try:
        # Create several Transform chunks, then wire them into the view's cook set -- both
        # loops entirely synchronous, no qapp.processEvents() in between, matching how
        # load_chunks fires CHUNK_LOADED for every chunk (and wires dependencies) all within
        # one call stack before anything ever gets a chance to actually recook.
        n_transforms = 4
        transforms = [_mk(transform_mod, scene) for _ in range(n_transforms)]
        prev = scatter
        for t in transforms:
            _wire(t, prev)
            prev = t
        _wire(view, prev)

        qapp.processEvents()
        _pump(0.5)

        assert call_count["n"] == 1, (
            f"a burst of {n_transforms} chunk creations that are all relevant to the active "
            f"view must collapse into a single recook, not one per chunk "
            f"(got {call_count['n']})"
        )
    finally:
        chunk_usd.recook_for_view = orig_recook


def test_a_value_set_while_blocked_is_not_lost_once_unblocked(qapp) -> None:
    """Regression: reported live -- a freshly created chunk, not yet wired to anything,
    is blocked (nothing reachable from any view yet -- see recook_for_view's own
    docstring: blocking "stops it reacting to param changes"). That block also silently
    suppressed the ATTRIBUTE_CHANGED notice _sync_param_input needs to author a param's
    value into a USD attr for the very first time -- so a value typed into a chunk
    before wiring it in never got an attr at all, and nothing later ever retried, since
    _sync_param_input only ever runs reactively off that same notice. Wiring the chunk
    in afterward only triggers reconcile_edit_targets' migration, which has nothing to
    migrate: the attr was never created in the first place. Confirmed live losing an
    entire Reference chunk's params (file path, imported prim path, insert location)
    this way, every time the value was set before the wire rather than after.
    """
    docker, scene, reg, stage, gv, scatter, view, _pump = _setup(qapp)
    reference_mod = reg.get_chunk_module("reference")

    stray = _mk(reference_mod, scene)
    qapp.processEvents()
    # A creation alone deliberately does not force a recook (see the test above this one) --
    # blocked stays at its unset default until SOME recook runs, same as it would once any
    # other, unrelated change in the live session (e.g. wiring up Create Layer/Edit Target)
    # triggers one and iterates every chunk, this stray one included.
    chunk_usd.recook_for_view(stage, scene, gv._graph_context(), view)
    qapp.processEvents()
    assert stray.is_blocked(), "setup must be faithful: a fresh, unwired chunk ends up blocked"

    stray.get_param("import_primpath").value = "/food_apple_01_4k"
    qapp.processEvents()
    _pump(0.2)

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(stray)))
    # The gap is now closed where it opened: a blocked chunk still records its values (it
    # only stops reacting to them -- see AttributeSender.is_blocked), so the attr exists
    # before the chunk is ever wired. The unblock catch-up below remains as a backstop.
    blocked_attr = prim.GetAttribute("inputs:import_primpath")
    assert blocked_attr.IsValid() and blocked_attr.Get() == "/food_apple_01_4k", (
        "a value set while blocked is recorded straight away"
    )

    _wire(scatter, stray)
    qapp.processEvents()
    _pump(0.2)
    # _wire (add_dependency alone) doesn't itself trigger a fresh recook in this harness --
    # the real UI drag-to-connect path does, through its own reactive cascade. Force the
    # recook explicitly rather than relying on that cascade being faithfully reproduced here.
    chunk_usd.recook_for_view(stage, scene, gv._graph_context(), view)
    qapp.processEvents()
    _pump(0.3)

    assert not stray.is_blocked(), "wiring into the view's upstream tree must unblock it"
    attr = prim.GetAttribute("inputs:import_primpath")
    assert attr.IsValid(), "the value set while blocked must not be lost once unblocked"
    assert attr.Get() == "/food_apple_01_4k"
