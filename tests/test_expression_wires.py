"""A param in a group gets a wire for an expression typed into it, as for one dragged in."""
from __future__ import annotations

from tests.test_copy_paste import _make_nodes, _pump, graph  # noqa: F401 -- graph is a fixture
from usd_chunks.frontends.qt import nodegraph


def _wires_into(scene, node, name: str) -> list:
    path = f"{node.prim.GetPath()}.inputs:{name}"
    return [e for e in scene._edges if e.dst_port.attr_path == path]


def _port(node, kind: str, name: str):
    return node.get_port(f"{node.prim.GetPath()}.{kind}:{name}")


def _two_transforms(graph):  # noqa: F811
    """Two Transform nodes, once their bodies and ports are built."""
    _, gv, chunks, nodes = _make_nodes(graph, n=2)
    _pump(graph[0], 0.5)
    return gv, chunks, nodes


def test_a_typed_expression_draws_a_wire(graph):  # noqa: F811
    qapp = graph[0]
    gv, chunks, nodes = _two_transforms(graph)
    source, dest = chunks

    # tx sits in Translate, so it lives on inputs:translate_grp:tx.
    dest.get_param("tx").value = f"@{source.name}.tx@"
    _pump(qapp, 0.5)
    gv.scene()._build_edges()

    [wire] = _wires_into(gv.scene(), nodes[1], "tx")
    assert wire.src_port is _port(nodes[0], "outputs", "tx")
    assert isinstance(wire, nodegraph.ChunkEdgeItem)


def test_a_dragged_wire_is_drawn_once(graph):  # noqa: F811
    qapp = graph[0]
    gv, _, nodes = _two_transforms(graph)

    # Dragging authors inputs:tx, and the expression it writes connects
    # inputs:translate_grp:tx too: still one wire.
    gv._author_connection(_port(nodes[0], "outputs", "tx"), _port(nodes[1], "inputs", "tx"))
    _pump(qapp, 0.5)
    gv.scene()._build_edges()

    assert len(_wires_into(gv.scene(), nodes[1], "tx")) == 1
