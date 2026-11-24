"""An exec wire dropped on a collapsed node's "+" chains the node.

A collapsed node's exec port and its "+" sit a few pixels apart (in Blender, two 9 px sockets
1 px apart at Home zoom), so a wire meant to chain the node often lands on the "+". The "+"
offers params only, and an exec wire is never one, so the drop said "nothing there takes that
wire" and did nothing. chunk_graph.plus_as_exec swaps the "+" for the node's exec port.
"""
from __future__ import annotations

from usd_chunks.graph import engine as chunk_graph


def _exec(path, is_input):
    name = chunk_graph.EXEC_INPUT if is_input else chunk_graph.EXEC_OUTPUT
    return chunk_graph.PortRef(path, f"{path}.{name}", chunk_graph.PORT_EXEC, is_input)


def test_an_exec_output_on_an_input_plus_becomes_the_exec_input() -> None:
    out = _exec("/chunks/A", False)

    a, b = chunk_graph.plus_as_exec(out, chunk_graph.PlusRef("/chunks/B", True))

    assert a == out
    assert b == _exec("/chunks/B", True)


def test_an_output_plus_met_by_an_exec_input_becomes_the_exec_output() -> None:
    inp = _exec("/chunks/B", True)

    a, b = chunk_graph.plus_as_exec(chunk_graph.PlusRef("/chunks/A", False), inp)

    assert a == _exec("/chunks/A", False)
    assert b == inp


def test_a_param_wire_still_asks_which_param() -> None:
    out = chunk_graph.PortRef("/chunks/A", "/chunks/A.outputs:tx", "float", False)
    plus = chunk_graph.PlusRef("/chunks/B", True)

    assert chunk_graph.plus_as_exec(out, plus) == (out, plus)


def test_a_plus_of_the_same_direction_or_node_is_left_alone() -> None:
    same_direction = (_exec("/chunks/A", True), chunk_graph.PlusRef("/chunks/B", True))
    same_node = (_exec("/chunks/A", False), chunk_graph.PlusRef("/chunks/A", True))
    both_plus = (chunk_graph.PlusRef("/chunks/A", False), chunk_graph.PlusRef("/chunks/B", True))

    for ends in (same_direction, same_node, both_plus):
        assert chunk_graph.plus_as_exec(*ends) == ends
