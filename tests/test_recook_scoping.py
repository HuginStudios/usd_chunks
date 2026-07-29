from __future__ import annotations

import time

from pxr import Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init
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
    # importlib.import_module("scripts.scatter_points"), which can resolve to a different
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
