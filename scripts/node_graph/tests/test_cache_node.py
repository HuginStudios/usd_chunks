"""
CacheNode tests — Houdini-style freeze node.

When locked, CacheNode returns stored values and the executor skips all
upstream nodes entirely (they never enter the execution order).
"""

# ── Behavior 1: unlocked pass-through ────────────────────────────────────────


def test_unlocked_passes_prim_through(qapp, node_graph, stage):
    """Unlocked CacheNode returns the input prim unchanged."""
    from node_graph.nodes._utility import CacheNode

    ng, _ = node_graph
    ng.register_node(CacheNode)
    node = ng.create_node(CacheNode.type_)

    sentinel = object()
    node._get_input_value = lambda name: sentinel if name == "prim" else None

    result = node.execute(stage)
    assert result["prim"] is sentinel


def test_unlocked_passes_data_through(qapp, node_graph, stage):
    """Unlocked CacheNode returns the input data unchanged."""
    from node_graph.nodes._utility import CacheNode

    ng, _ = node_graph
    ng.register_node(CacheNode)
    node = ng.create_node(CacheNode.type_)

    sentinel = {"some": "dict"}
    node._get_input_value = lambda name: sentinel if name == "data" else None

    result = node.execute(stage)
    assert result["data"] is sentinel


# ── Behavior 2: cache stores last non-None value ──────────────────────────────


def test_execute_stores_prim_in_cache(qapp, node_graph, stage):
    """Executing unlocked stores the prim so it's available when locked later."""
    from node_graph.nodes._utility import CacheNode

    ng, _ = node_graph
    ng.register_node(CacheNode)
    node = ng.create_node(CacheNode.type_)

    prim = object()
    node._get_input_value = lambda name: prim if name == "prim" else None
    node.execute(stage)

    assert node._cache["prim"] is prim


# ── Behavior 3: locked freeze ─────────────────────────────────────────────────


def test_locked_returns_cached_prim_ignores_live_input(qapp, node_graph, stage):
    """Locked CacheNode returns the stored prim even when a different prim arrives."""
    from node_graph.nodes._utility import CacheNode

    ng, _ = node_graph
    ng.register_node(CacheNode)
    node = ng.create_node(CacheNode.type_)

    cached_prim = object()
    new_prim = object()

    # First execute while unlocked — fills cache
    node._get_input_value = lambda name: cached_prim if name == "prim" else None
    node.execute(stage)

    # Lock, then execute with a different upstream value
    node.set_property("locked", True)
    node._get_input_value = lambda name: new_prim if name == "prim" else None
    result = node.execute(stage)

    assert result["prim"] is cached_prim
    assert result["prim"] is not new_prim


# ── Behavior 4: clear_cache ───────────────────────────────────────────────────


def test_clear_cache_wipes_stored_values(qapp, node_graph, stage):
    """clear_cache() empties the cache so a locked node returns None."""
    from node_graph.nodes._utility import CacheNode

    ng, _ = node_graph
    ng.register_node(CacheNode)
    node = ng.create_node(CacheNode.type_)

    # Fill cache then lock
    prim = object()
    node._get_input_value = lambda name: prim if name == "prim" else None
    node.execute(stage)
    node.set_property("locked", True)

    node.clear_cache()
    result = node.execute(stage)

    assert result["prim"] is None
    assert result["data"] is None


# ── Behavior 5: executor skips upstream of a locked CacheNode ─────────────────


def test_executor_skips_upstream_when_cache_locked(qapp, node_graph, stage):
    """Locked CacheNode causes GraphExecutor to exclude its upstream from the run order."""
    from node_graph.nodes._utility import CacheNode
    from node_graph.nodes._input import PrimPathNode
    from node_graph.graph_executor import GraphExecutor

    ng, _ = node_graph
    ng.register_node(CacheNode)
    ng.register_node(PrimPathNode)

    upstream = ng.create_node(PrimPathNode.type_, name="Upstream")
    cache = ng.create_node(CacheNode.type_, name="Cache")

    # Wire upstream prim → cache prim
    out_port = upstream.get_output("prim")
    in_port = cache.get_input("prim")
    if out_port and in_port:
        out_port.connect_to(in_port)

    cache.set_property("locked", True)

    executed = []

    class _TrackingWidget:
        _graph = ng
        _stage = stage

        def _set_exec_state(self, node, state, msg=""):
            if state == "running":
                executed.append(node.name())

    executor = GraphExecutor(_TrackingWidget())
    executor.run_full()

    assert "Upstream" not in executed
    assert "Cache" in executed
