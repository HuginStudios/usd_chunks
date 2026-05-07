"""
GraphExecutor integration tests — error propagation, run_from scoping, group promoted values.
"""
import pytest


class _StubWidget:
    def __init__(self, ng, stage):
        self._graph = ng
        self._stage = stage

    def _set_exec_state(self, node, state, msg=""):
        pass


# ── 1. Error propagation ─────────────────────────────────────────────────────

def test_error_propagation_skips_downstream(node_graph, stage, qapp):
    """When _exec_node returns False, downstream nodes are skipped with empty cache."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    a = ng.create_node(CubeNodeCls.type_, name="ErrA")
    b = ng.create_node(CubeNodeCls.type_, name="ErrB")

    out_port = next((p for p in a.output_ports() if p.name() == "_prim_path"), None)
    in_port = next((p for p in b.input_ports() if p.name() == "_prim_path"), None)
    if not (out_port and in_port):
        pytest.skip("cannot wire A→B")
    out_port.connect_to(in_port)

    # Force A to error by making execute raise
    orig_execute = a.execute
    a.execute = lambda stage: (_ for _ in ()).throw(RuntimeError("forced"))

    executor = GraphExecutor(_StubWidget(ng, stage))
    executor.run_full()

    assert b._output_cache == {}
    a.execute = orig_execute


# ── 2. run_from subtree scoping ──────────────────────────────────────────────

def test_run_from_does_not_execute_unrelated_nodes(node_graph, stage, qapp):
    """run_from(B) does not re-execute node A when A is not upstream of B."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    a = ng.create_node(CubeNodeCls.type_, name="IsoA")
    b = ng.create_node(CubeNodeCls.type_, name="IsoB")

    executed = []
    orig_a = a.execute
    orig_b = b.execute

    def track_a(stage):
        executed.append("A")
        return orig_a(stage)

    def track_b(stage):
        executed.append("B")
        return orig_b(stage)

    a.execute = track_a
    b.execute = track_b

    executor = GraphExecutor(_StubWidget(ng, stage))
    executor.run_from(b)

    assert "A" not in executed
    assert "B" in executed

    a.execute = orig_a
    b.execute = orig_b


# ── 3. Group promoted value flow ─────────────────────────────────────────────

def test_promoted_default_value_applied_in_exec_group(node_graph, stage, qapp):
    """_exec_group applies promoted default_value to inner node's property."""
    from node_graph.graph_executor import GraphExecutor
    from .test_group_nodes import _make_group, _add_promotion

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="PromoVal")
    _add_promotion(g, inner_name, "prim_path", "/PromoValTest")

    executor = GraphExecutor(_StubWidget(ng, stage))
    executor._exec_group(g)

    prim = stage.GetPrimAtPath("/PromoValTest")
    assert prim.IsValid(), "Promoted default_value was not applied to inner node"
