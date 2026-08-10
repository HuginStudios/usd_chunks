from __future__ import annotations

import time

import pytest
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
    # inputs:chunk. It writes into the chunk-prim bookkeeping (session) layer, which is where
    # the live graph's own wiring lands. Hand-authoring the connection first goes to the
    # stage's AMBIENT edit target -- the root layer -- and is doubly wrong: the listener then
    # sees the connection already present, skips its own session-layer write, and the
    # root-layer opinion is later dropped, leaving inputs:chunk with no connections at all.
    # The exec chain then reads as unwired nodes and the whole cook set collapses to "nothing
    # cooks" -- no /ScatterInstancer to read at all, rather than a wrong position.
    dst.add_dependency(src)


def test_scatter_is_reactive_and_ignores_downstream_transform() -> None:
    """Regression for the Transform1 -> Scatter -> Transform2 bug reported live in usdview.

    Scatter used to be a manual "Scatter" button reading the plain composed stage, which could
    pick up a downstream chunk's edit to the same prim (layer-strength order reflects DAG depth,
    not who's genuinely upstream of the reader). Scatter is now reactive (a plain module-level
    execute(), like Transform) instead of button-driven -- it runs as part of the same ordered
    DAG solve pass as its upstream, so a chunk wired *after* it is never part of that pass yet
    when it reads. No manual button, no auto-scatter toggle, no hidden-view trick required.
    """
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

    transform_mod = reg.get_chunk_module("transform")
    scatter_mod = reg.get_chunk_module("scatter_points")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    t1 = _mk(transform_mod, scene)
    scatter = _mk(scatter_mod, scene)
    t2 = _mk(transform_mod, scene)
    _wire(scatter, t1)
    _wire(t2, scatter)

    t1.get_param("primpath_location").value = "/apple"
    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 1
    t2.get_param("primpath_location").value = "/apple"

    # No button, no toggle -- the old manual-exec affordances are gone.
    assert scatter.get_param("scatter") is None
    assert scatter.get_param("auto_scatter") is None

    def _x() -> float:
        return stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions").Get()[0][0]

    t1.get_param("tx").value = 1.0
    x_after_t1 = _x()

    # Transform2 is downstream of Scatter -- must never affect an already-computed scatter.
    t2.get_param("tx").value = 99.0
    assert _x() == x_after_t1

    # Re-triggering via t1 (the real upstream) must still only reflect t1, never t2's 99.
    t1.get_param("tx").value = 5.0
    assert _x() == pytest.approx(x_after_t1 + 4.0, abs=1e-4)


def test_scatter_direct_param_edit_ignores_downstream_transform(qapp) -> None:
    """Regression reported live in usdview: Scatter -> Transform -> View (Transform targets
    the same mesh Scatter scatters from, e.g. to move the original out of the way of the new
    instances). Once a real View has been activated (wired to Transform, the tail), simply
    going back to Scatter and editing one of its own params directly -- not touching Transform
    or the view -- made the scattered instances "jump away" to Transform's offset.

    Root cause: activating the view runs a real recook_for_view pass, whose closing step ranks
    Transform (closer to the view) stronger than Scatter for their shared prim -- correctly, for
    the settled/visible result. But editing Scatter's own param afterward is a *plain* reactive
    param change; it never goes through recook_for_view at all, so nothing re-scopes the read --
    Scatter's execute() just reads the live composed stage, where Transform's edit now
    permanently outranks Scatter's own view of the same prim, regardless of real DAG order.
    execute() now resolves surface_prim/proto_prim through get_upstream_stage(), which is
    unaffected by that ranking -- a pure read-only composition, not dependent on recook state.

    Needs the real Qt graph widget + real view activation (view:slot + _view_state.set_slot) to
    establish that ranking in the first place -- a hand-wired chunk graph with no view/recook
    activity doesn't reach the same layer-strength state (confirmed while investigating the
    equivalent recook_for_view bug earlier)."""
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

    transform_mod = reg.get_chunk_module("transform")
    scatter_mod = reg.get_chunk_module("scatter_points")
    view_mod = reg.get_chunk_module("view")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter = _mk(scatter_mod, scene)
    transform = _mk(transform_mod, scene)
    qapp.processEvents()

    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 1
    transform.get_param("primpath_location").value = "/apple"  # same mesh scatter reads from
    _wire(transform, scatter)
    qapp.processEvents()

    def _pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()

    # Activate a real view wired to transform (the tail) -- this is what establishes the
    # "closer to view wins" rank that later poisons a plain param edit on scatter.
    gv = docker.tab_widget.get_current_graph_view()
    context = gv._graph_context()
    view = _mk(view_mod, scene)
    _wire(view, transform)
    qapp.processEvents()
    gv._activate_menu_view(view, context, stage)
    _pump(0.5)
    assert gv._view_state.active_slot, "view must be properly activated for this repro to be faithful"

    def _x() -> float:
        pos = stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions").Get()
        return pos[0][0]

    transform.get_param("tx").value = 99.0
    _pump(0.5)

    # Going back to Scatter and editing one of ITS OWN params -- not transform, not the view.
    scatter.get_param("seed").value = 5
    _pump(0.5)

    x = _x()
    assert 0.0 <= x <= 1.0, (
        f"Scatter's own param edit must still only reflect its real upstream (the raw mesh, "
        f"local X in [0,1]) -- transform is downstream and must never leak in. Got x={x}"
    )


def test_scatter_survives_prototype_vanishing_after_upstream_read(monkeypatch) -> None:
    """Regression (issue #82): proto_prim is resolved off get_upstream_stage()'s scratch
    composition (see execute()'s own docstring for why), which can still show a prim whose
    real source (a sublayer, a reference chunk) has since been disconnected from the live
    stage by the time the internal reference is actually authored against it -- a real gap
    between "upstream read" and "write", the same one a shake-to-disconnect recook hits live.
    The unresolved reference used to leave ref_prim typeless and crash Set() on its
    xformOpOrder attr; it should fall back to the same default cube used when no prototype is
    set at all."""
    from usd_chunks.plugins import chunk_usd as _chunk_usd

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

    scatter_mod = reg.get_chunk_module("scatter_points")
    scatter = _mk(scatter_mod, scene)

    surface = UsdGeom.Mesh.Define(stage, "/surface")
    surface.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    surface.GetFaceVertexCountsAttr().Set([3])
    surface.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter.get_param("surface_prim").value = "/surface"
    scatter.get_param("proto_prim").value = "/vanished_proto"
    scatter.get_param("max_count").value = 1

    # get_upstream_stage() is what proto_prim actually gets read through -- point it at a
    # separate stage where the prototype still resolves, while the real stage (what the
    # internal reference is authored against) never had it at all.
    scratch = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(scratch, "/vanished_proto")
    monkeypatch.setattr(_chunk_usd, "get_upstream_stage", lambda *a, **k: scratch)

    scatter_mod.module.execute(scatter)  # must not raise

    proto = stage.GetPrimAtPath("/ScatterInstancer/Prototypes/proto_0")
    assert proto.IsValid()
    assert proto.GetTypeName() == "Cube", "should fall back to the default cube, not a dangling reference"
    assert not proto.HasAuthoredReferences(), "no reference should be left dangling"
