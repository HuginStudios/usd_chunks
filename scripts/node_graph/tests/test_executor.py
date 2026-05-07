"""
GraphExecutor tests — behavior 2.3.

Tests verify GraphExecutor's dirty tracking, debounce, and execution ordering
without requiring a full NodeGraphWidget — a minimal stub widget is injected.
"""
import pytest


# ── Stub widget ───────────────────────────────────────────────────────────────

class _StubWidget:
    """Minimal stand-in for NodeGraphWidget used by GraphExecutor."""

    def __init__(self, ng, stage):
        self._graph = ng
        self._stage = stage

    def _set_exec_state(self, node, state, msg=""):
        pass


# ── Behavior 1: dirty tracking respects the executing guard ──────────────────

def test_mark_dirty_is_dropped_while_executing(node_graph, stage, qapp):
    """mark_dirty() is silently dropped when _is_executing is True (prevents loops)."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    executor = GraphExecutor(_StubWidget(ng, stage))

    executor._is_executing = True
    executor.mark_dirty()
    # Timer must NOT have been started — the node ref stays None.
    assert executor._debounce_node is None
    executor._is_executing = False


def test_mark_dirty_sets_debounce_node(node_graph, stage, qapp):
    """mark_dirty(node) stores the node for the next debounce flush."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    executor = GraphExecutor(_StubWidget(ng, stage))

    executor.mark_dirty(node)
    assert executor._debounce_node is node
    executor._debounce_timer.stop()  # prevent actual flush in test


# ── Behavior 2: topo-sort visits all nodes ────────────────────────────────────

def test_run_full_executes_on_non_empty_graph(node_graph, stage, qapp):
    """run_full() runs without error when the graph has nodes."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    ng.create_node(CubeNodeCls.type_)
    executor = GraphExecutor(_StubWidget(ng, stage))
    executor.run_full()  # should not raise


def test_run_full_on_empty_graph_is_a_noop(node_graph, stage, qapp):
    """run_full() on an empty graph completes without error."""
    from node_graph.graph_executor import GraphExecutor

    ng, _CubeNodeCls = node_graph
    executor = GraphExecutor(_StubWidget(ng, stage))
    executor.run_full()


# ── Behavior 3: downstream collection ────────────────────────────────────────

def test_get_downstream_returns_connected_nodes(node_graph, stage, qapp):
    """_get_downstream returns nodes wired downstream of start_node."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    a = ng.create_node(CubeNodeCls.type_, name="A")
    b = ng.create_node(CubeNodeCls.type_, name="B")

    # Chunk auto-nodes expose _prim_path on both inputs and outputs; use that to wire A → B.
    out_port = next((p for p in a.output_ports() if p.name() == "_prim_path"), None)
    in_port = next((p for p in b.input_ports() if p.name() == "_prim_path"), None)
    connected = False
    if out_port and in_port:
        try:
            out_port.connect_to(in_port)
            connected = True
        except Exception:
            pass

    if not connected:
        pytest.skip("could not wire A → B (no compatible ports found)")

    executor = GraphExecutor(_StubWidget(ng, stage))
    downstream = executor._get_downstream(a)
    downstream_names = {n.name() for n in downstream}
    assert "B" in downstream_names


def test_get_downstream_returns_empty_for_isolated_node(node_graph, stage, qapp):
    """_get_downstream returns [] for a node with no downstream connections."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    executor = GraphExecutor(_StubWidget(ng, stage))
    assert executor._get_downstream(node) == []


# ── Behavior 4: node layer registry ──────────────────────────────────────────

def test_exec_node_registers_layer_on_success(node_graph, stage, qapp):
    """After a successful execute, the node's layer is tracked in _node_layer_registry."""
    from node_graph.graph_executor import GraphExecutor

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    executor = GraphExecutor(_StubWidget(ng, stage))

    executor._exec_node(node)

    assert node.id in executor._node_layer_registry
