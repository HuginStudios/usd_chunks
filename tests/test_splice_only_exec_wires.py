"""Dropping a node on a wire splices the EXEC chain only.

Live-reported as "frustrating": moving a node anywhere near a param connection silently
re-pointed that connection at the newcomer, so on a busy graph -- where exec and data
wires run through the same space -- dropping a node onto the exec chain was a coin toss.

It is not merely riskier for a data wire, it is a different operation. A -> B on a data
wire carries A's VALUE to B, and A -> self -> B only reproduces that if self forwards it
untouched, which nothing here can know. The exec chain has no such problem: a chunk in the
middle of it is exactly what the wire means.
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import (
    connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
)


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
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.resize(900, 700)
    docker.show()
    qapp.processEvents()
    return qapp, reg, scene, docker, stage


def _three_transforms(graph):
    qapp, reg, scene, docker, stage = graph
    mod = reg.get_chunk_module("transform")
    a, b, c = _mk(mod, scene), _mk(mod, scene), _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    nodes = [gv.scene()._nodes[get_chunk_prim_path(x)] for x in (a, b, c)]
    return qapp, scene, stage, gv, (a, b, c), nodes


def _drop_onto(qapp, node, src_port, dst_port):
    """Put node over the middle of the wire and run the drop handler."""
    mid = (src_port.scenePos() + dst_port.scenePos()) / 2
    node.setPos(
        mid - QtCore.QPointF(node.boundingRect().width() / 2, node.boundingRect().height() / 2)
    )
    qapp.processEvents()
    node._try_insert_on_edge()
    qapp.processEvents()


def test_a_data_wire_is_left_alone(graph):
    qapp, scene, stage, gv, (a, b, c), (a_node, b_node, c_node) = _three_transforms(graph)
    a_out = a_node.get_port(f"{a_node.prim.GetPath()}.outputs:tx")
    b_in = b_node.get_port(f"{b_node.prim.GetPath()}.inputs:tx")
    gv._author_connection(a_out, b_in)
    gv.scene()._build_edges()
    qapp.processEvents()
    before = [str(x) for x in stage.GetAttributeAtPath(b_in.attr_path).GetConnections()]
    assert before, "the data wire must exist before the drop means anything"

    _drop_onto(qapp, c_node, a_out, b_in)

    after = [str(x) for x in stage.GetAttributeAtPath(b_in.attr_path).GetConnections()]
    assert after == before, (
        f"a node dropped across a param wire must not re-point it: {before} -> {after}"
    )


def test_an_exec_wire_still_splices(graph):
    """The feature itself -- pinned, since the fix is a narrowing."""
    qapp, scene, stage, gv, (a, b, c), (a_node, b_node, c_node) = _three_transforms(graph)
    connect_exec(stage, scene, get_chunk_prim_path(a), get_chunk_prim_path(b))
    gv.scene()._build_edges()
    qapp.processEvents()
    a_out = a_node.get_port(f"{a_node.prim.GetPath()}.outputs:chunk")
    b_in = b_node.get_port(f"{b_node.prim.GetPath()}.inputs:chunk")

    _drop_onto(qapp, c_node, a_out, b_in)

    conns = [str(x) for x in stage.GetAttributeAtPath(b_in.attr_path).GetConnections()]
    assert conns == [f"{get_chunk_prim_path(c)}.outputs:chunk"], (
        f"B should now be fed by the dropped node, got {conns}"
    )
    c_prim = stage.GetPrimAtPath(get_chunk_prim_path(c))
    c_in = [str(x) for x in c_prim.GetAttribute("inputs:chunk").GetConnections()]
    assert c_in == [f"{get_chunk_prim_path(a)}.outputs:chunk"], (
        f"and fed by A itself, got {c_in}"
    )
