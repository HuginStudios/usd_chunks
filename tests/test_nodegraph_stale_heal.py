"""The graph must not stay frozen when its node set stops matching the stage.

Reported live: undo a chunk create, undo back further, then redo. The chunk prim is back
and active, the chunk object is alive, they pair up perfectly -- and the node never returns
to the graph. Diagnosed in a real session (state() in diagnose_redo.py):

    === chunk PRIMS ===       /Tab1/Sublayer  /Tab1/View1  /Tab1/Transform   (all active)
    === live CHUNKS ===       /Tab1/Sublayer  /Tab1/View1  /Tab1/Transform
    === THE ANSWER ===        prims and chunks pair up perfectly
    === graph nodes ===       ['/Tab1/Sublayer', '/Tab1/View1']

Two things combine. Undoing away every chunk makes the root invalid, which _rebuild_nodes
treats as a transient recomposition -- it keeps the graph and retries, then gives up. And
the redo reports its restore as property-level resyncs, which the rebuild trigger ignores
by design (every cook emits hundreds of those; rebuilding on them was 286 rebuilds for one
group expand). So nothing was left to notice.
"""
from __future__ import annotations

import time

from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _pump(qapp, seconds: float = 0.3) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _mk(reg, scene, module_name):
    mod = reg.get_chunk_module(module_name)
    chunk = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(chunk)
    return chunk


def _graph_session(qapp):
    from usd_chunks.frontends.qt.factory import ChunkWidgetDocker

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    _pump(qapp)
    return stage, reg, scene, docker


def _touch_a_property(stage, prim_path):
    """A property-level resync under the root -- the notice shape the redo produced.

    Not a prim-level one: that already triggers a rebuild, so using it would prove nothing.
    """
    path = Sdf.Path(f"{prim_path}.inputs:_probe")
    layer = stage.GetSessionLayer()
    with Usd.EditContext(stage, layer):
        prim_spec = layer.GetPrimAtPath(path.GetPrimPath())
        assert prim_spec is not None, f"no prim spec at {prim_path}"
        with Sdf.ChangeBlock():
            Sdf.AttributeSpec(prim_spec, path.name, Sdf.ValueTypeNames.Float).default = 1.0


def test_a_prim_with_no_node_heals_on_the_next_resync(qapp) -> None:
    stage, reg, scene, docker = _graph_session(qapp)

    keep = _mk(reg, scene, "sublayer")
    chunk = _mk(reg, scene, "transform")
    _pump(qapp)

    gv = docker.tab_widget.get_current_graph_view()
    graph = gv.scene()
    path = get_chunk_prim_path(chunk)
    assert path in graph._nodes, "node never appeared in the first place"

    # The reported state: the prim and the chunk are both fine, the graph has simply
    # stopped tracking one of them and nothing is scheduled to correct it.
    graph._teardown_node(graph._nodes.pop(path))
    assert stage.GetPrimAtPath(Sdf.Path(path)).IsValid()
    assert path not in graph._nodes

    _touch_a_property(stage, get_chunk_prim_path(keep))
    _pump(qapp)

    assert path in graph._nodes, "the graph stayed frozen with a prim it was not showing"


def test_a_property_resync_on_a_healthy_graph_does_not_rebuild(qapp) -> None:
    """The reason this is a self-heal and not a widening of the fast path. Property resyncs
    are the hot path -- one cook emits hundreds -- and rebuilding on each was measured at
    286 rebuilds for a single group expand. A graph that already agrees with the stage must
    not schedule anything."""
    stage, reg, scene, docker = _graph_session(qapp)

    keep = _mk(reg, scene, "sublayer")
    _mk(reg, scene, "transform")
    _pump(qapp)

    graph = docker.tab_widget.get_current_graph_view().scene()
    graph._rebuild_pending = False

    _touch_a_property(stage, get_chunk_prim_path(keep))

    assert not graph._rebuild_pending, "a healthy graph scheduled a needless rebuild"
