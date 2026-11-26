"""scene.create.<chunk>(None, ...) starts a chain, as leaving the input out does.

A helper that threads a `last` chunk through a chain starts with `last = None`; passing it
on failed deep in wiring ('NoneType' object has no attribute 'get_dag_node').
"""

from __future__ import annotations

from usd_chunks import Scene


def test_none_as_the_input_starts_a_chain() -> None:
    with Scene() as scene:
        first = scene.create.cube(None, prim_path="/a")
        second = scene.create.transform(first, primpath_location="/a", ty=1.0)
        assert scene.get_inputs(first) == []
        assert scene.get_inputs(second) == [first]
        assert scene.get_stage().GetPrimAtPath("/a")


def test_none_among_inputs_is_left_out() -> None:
    with Scene() as scene:
        a = scene.create.cube(prim_path="/a")
        b = scene.create.transform(a, None, primpath_location="/a")
        assert scene.get_inputs(b) == [a]
