"""A param in a group gets a wire for an expression typed into it, as for one dragged in."""
from __future__ import annotations

from PySide6 import QtCore

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


def test_a_value_typed_over_the_expression_takes_the_wire_away(graph):  # noqa: F811
    qapp = graph[0]
    gv, chunks, nodes = _two_transforms(graph)
    source, dest = chunks
    tx = dest.get_param("tx")
    tx.value = f"@{source.name}.tx@"
    _pump(qapp, 0.5)

    # What dragging the wire off does: the param goes back to a plain value.
    tx.value = 0.0
    _pump(qapp, 0.5)
    gv.scene()._build_edges()

    assert _wires_into(gv.scene(), nodes[1], "tx") == []
    assert not tx.get_usd_prop().HasAuthoredConnections()


def test_a_text_typed_over_an_expression_takes_the_wire_away(graph):  # noqa: F811
    qapp = graph[0]
    gv, chunks, nodes = _two_transforms(graph)
    source, dest = chunks
    # A String param: its attr keeps the expression as its value.
    path = dest.get_param("primpath_location")
    path.value = f"@{source.name}.primpath_location@"
    _pump(qapp, 0.5)
    assert path.get_usd_prop().HasAuthoredConnections()

    path.value = "/somewhere"
    _pump(qapp, 0.5)

    assert not path.get_usd_prop().HasAuthoredConnections()


def test_a_typed_wire_can_be_dragged_off(graph):  # noqa: F811
    qapp = graph[0]
    gv, chunks, nodes = _two_transforms(graph)
    source, dest = chunks
    tx = dest.get_param("tx")
    tx.value = f"@{source.name}.tx@"
    _pump(qapp, 0.5)
    gv.scene()._build_edges()
    port = _port(nodes[1], "inputs", "tx")

    # Grab the wire at the input and let go on empty canvas.
    gv._start_wire_drag(port, port.scenePos())
    assert gv._drag_reused_edge is not None  # picked up, not a new wire
    gv._finish_wire_drag(port.scenePos() + QtCore.QPointF(-400, 600))
    _pump(qapp, 0.5)
    gv.scene()._build_edges()

    assert _wires_into(gv.scene(), nodes[1], "tx") == []
    assert tx.raw_value == tx.default_value
    assert not tx.get_usd_prop().HasAuthoredConnections()
