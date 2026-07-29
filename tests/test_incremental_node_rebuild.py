from __future__ import annotations

import time

from pxr import Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init
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


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def test_unrelated_chunk_create_does_not_recreate_existing_node_or_proxy(qapp) -> None:
    """Regression reported live in usdview (by a teammate, as an actual OS-level popup-window
    flicker; independently as visible flashing/collapsing/expanding of every node): creating or
    deleting one chunk used to tear down and rebuild EVERY node's ChunkWidget proxy -- a native
    Qt window under the hood -- via ChunkGraphScene._rebuild_nodes()'s unconditional
    _teardown_items()+full-recreate on every structural change, regardless of whether that node
    had anything to do with the change. create_proxy()'s "never let the OS show a top-level
    window during reparenting" workaround was therefore being exercised once per EXISTING node
    on every single create/delete, not just for the node that actually changed.

    _rebuild_nodes() now reuses a node (and its live proxy widget) when its chunk is still
    present and unchanged, only tearing down/recreating nodes whose chunk actually appeared or
    disappeared.
    """
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
    transform_mod = reg.get_chunk_module("transform")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter = _mk(scatter_mod, scene)
    qapp.processEvents()
    _pump(qapp, 0.3)

    gv = docker.tab_widget.get_current_graph_view()
    scatter_path = get_chunk_prim_path(scatter)
    node_before = gv.scene()._nodes.get(scatter_path)
    assert node_before is not None, "scatter's node must exist before the unrelated create"
    proxy_before = node_before._proxy
    widget_before = node_before._chunk_widget
    assert proxy_before is not None and widget_before is not None

    # Create a totally unrelated chunk -- not wired to scatter at all.
    stray = _mk(transform_mod, scene)
    qapp.processEvents()
    _pump(qapp, 0.3)

    node_after = gv.scene()._nodes.get(scatter_path)
    assert node_after is node_before, (
        "scatter's node item must be the SAME object after an unrelated create — rebuilding it "
        "loses selection/paint state for no reason"
    )
    assert node_after._proxy is proxy_before, (
        "scatter's proxy widget must not be torn down and recreated just because an unrelated "
        "chunk was created — that's the actual mechanism behind the reported flicker/popup"
    )
    assert node_after._chunk_widget is widget_before

    # The new chunk must still show up as its own, real node.
    stray_path = get_chunk_prim_path(stray)
    stray_node = gv.scene()._nodes.get(stray_path)
    assert stray_node is not None, "the newly created chunk must still get its own node"
    _pump(qapp, 0.3)
    assert stray_node._proxy is not None, "the newly created node must still get a real proxy widget"


def test_deleting_one_chunk_only_tears_down_that_nodes_proxy(qapp) -> None:
    """Companion sanity check: the incremental reuse above must not break real removal — a
    deleted chunk's node must actually disappear, and an unrelated node's proxy must survive
    that deletion too."""
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
    transform_mod = reg.get_chunk_module("transform")

    scatter = _mk(scatter_mod, scene)
    stray = _mk(transform_mod, scene)
    qapp.processEvents()
    _pump(qapp, 0.3)

    gv = docker.tab_widget.get_current_graph_view()
    scatter_path = get_chunk_prim_path(scatter)
    stray_path = get_chunk_prim_path(stray)
    scatter_node = gv.scene()._nodes.get(scatter_path)
    proxy_before = scatter_node._proxy
    assert proxy_before is not None

    from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView

    ChunkGraphView.mark_pending_delete_relevance([stray])
    stray.request_delete()
    qapp.processEvents()
    _pump(qapp, 0.3)

    assert stray_path not in gv.scene()._nodes, "the deleted chunk's node must actually be gone"
    scatter_node_after = gv.scene()._nodes.get(scatter_path)
    assert scatter_node_after is scatter_node
    assert scatter_node_after._proxy is proxy_before, (
        "an unrelated chunk's node/proxy must survive a delete elsewhere in the graph"
    )
