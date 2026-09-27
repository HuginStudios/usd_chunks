"""Regression coverage for wire-drop snapping to a nearby port.

Live-reported: dragging a wire to connect is too hard -- a drop had to land pixel-perfect
on a port's tiny dot (radius 5 scene units) or it silently failed. ChunkGraphView's
_finish_wire_drag now falls back to the nearest valid port within a comfortable on-screen
tolerance (_PORT_SNAP_PIXELS) when the exact drop point misses.
"""

from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker


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


@pytest.fixture
def graph(qapp):
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

    return qapp, reg, scene, docker


def _ports(gv, a, b, attr="tx"):
    a_node = gv.scene()._nodes[get_chunk_prim_path(a)]
    b_node = gv.scene()._nodes[get_chunk_prim_path(b)]
    a_path = str(a_node.prim.GetPath())
    b_path = str(b_node.prim.GetPath())
    a_out = a_node.get_port(f"{a_path}.outputs:{attr}")
    b_in = b_node.get_port(f"{b_path}.inputs:{attr}")
    assert a_out is not None and b_in is not None, "typed ports must exist by now"
    return a_out, b_in


def test_wire_drop_snaps_to_nearby_port_within_tolerance(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    a = _mk(mod, scene)
    b = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    a_out, b_in = _ports(gv, a, b)

    gv._start_wire_drag(a_out, a_out.scenePos())
    # 12 scene units off-center: outside the 5-unit port dot (would have missed before this
    # fix) but inside the 20px snap tolerance at the default 1:1 zoom.
    near_pos = b_in.scenePos() + QtCore.QPointF(12, 0)
    gv._finish_wire_drag(near_pos)

    b_attr = gv.scene()._stage.GetAttributeAtPath(b_in.attr_path)
    assert list(b_attr.GetConnections()), "drop near (not exactly on) the port should still connect"


def test_wire_drop_far_from_any_port_does_not_snap(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    a = _mk(mod, scene)
    b = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    a_out, b_in = _ports(gv, a, b)
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()

    # Pick the existing connection back up from b_in and drop it far from any port -- must
    # disconnect (existing behavior), not snap to some unrelated port across the canvas.
    gv._start_wire_drag(b_in, b_in.scenePos())
    far_pos = b_in.scenePos() + QtCore.QPointF(5000, 5000)
    gv._finish_wire_drag(far_pos)

    b_attr = gv.scene()._stage.GetAttributeAtPath(b_in.attr_path)
    assert not list(b_attr.GetConnections()), "dropping far from any port should disconnect, not snap"


def test_pulling_an_exec_wire_off_cuts_only_that_wire(graph) -> None:
    """A -> B -> C, drag B's exec input off into empty space.

    Live-reported: the wire seemed to reconnect itself to the next node down the chain.
    The disconnect healed as if B were being removed, rewiring C to A and dropping B -> C.
    """
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("prim")
    a, b, c = _mk(mod, scene), _mk(mod, scene), _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    nodes = gv.scene()._nodes
    pa, pb, pc = (get_chunk_prim_path(x) for x in (a, b, c))
    gv._author_connection(nodes[pa].get_port(f"{pa}.outputs:chunk"), nodes[pb].get_port(f"{pb}.inputs:chunk"))
    gv._author_connection(nodes[pb].get_port(f"{pb}.outputs:chunk"), nodes[pc].get_port(f"{pc}.inputs:chunk"))
    gv.scene()._build_edges()
    stage = gv.scene()._stage
    # In a scene opened from disk the wires live in the root layer, which is also the edit
    # target. Recreate that here: wired live they land in a stronger layer, where a heal
    # authored on the edit target is silently outvoted and the bug never shows.
    home = chunk_utils.get_attr_home_layer(stage.GetAttributeAtPath(f"{pc}.inputs:chunk"))
    stage.SetEditTarget(Usd.EditTarget(home))

    b_in = gv.scene()._nodes[pb].get_port(f"{pb}.inputs:chunk")
    gv._start_wire_drag(b_in, b_in.scenePos())
    gv._finish_wire_drag(b_in.scenePos() + QtCore.QPointF(5000, 5000))

    assert not stage.GetAttributeAtPath(f"{pb}.inputs:chunk").GetConnections()
    assert [str(p) for p in stage.GetAttributeAtPath(f"{pc}.inputs:chunk").GetConnections()] == [
        f"{pb}.outputs:chunk"
    ], "C must stay wired to B, not be handed B's old input"
