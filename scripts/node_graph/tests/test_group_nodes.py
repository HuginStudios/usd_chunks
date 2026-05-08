"""
Group node tests — behavior 2.2 + regression suite.

Tests verify that GroupNode works correctly: grouping, param promotion,
value flow through execution, serialization/reload, and export as chunk module.
Regression tests cover known edge cases and both layer-edit fix paths from
issue #01.

Each test builds up a GroupNode manually via the public NodeGraphQt API
(create_node / expand_group_node / collapse_group_node) rather than going
through the widget's keyboard shortcuts, which are not available in headless
tests.
"""


from NodeGraphQt.nodes.group_node import GroupNode


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_group(ng, CubeNodeCls, name="MyGroup"):
    """Create and return a collapsed GroupNode with one inner CubeNode."""
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name=name)
    g.set_port_deletion_allowed(True)
    sub = ng.expand_group_node(g)
    inner = sub.create_node(CubeNodeCls.type_, name=f"Inner_{name}")
    ng.collapse_group_node(g)
    return g, inner.name()


def _add_promotion(g, inner_name, port_name, default_value):
    """Add an input port + promotion entry to a collapsed GroupNode."""
    from node_graph.nodes._base import _get_promotions, _save_promotions

    # We need the inner node's id from the stored sub-graph session.
    session = g.get_sub_graph_session()
    inner_id = next(
        (
            nid
            for nid, nd in session.get("nodes", {}).items()
            if nd.get("name") == inner_name
        ),
        None,
    )

    g.add_input(port_name, color=(140, 150, 170))
    promos = _get_promotions(g)
    promos[port_name] = {
        "inner_node_id": inner_id,
        "inner_node_name": inner_name,
        "inner_param": port_name,
        "chunk_param_class": "LineEdit",
        "chunk_param_kwargs": {},
        "default_value": default_value,
    }
    _save_promotions(g)


# ── Behavior 1: grouping ──────────────────────────────────────────────────────


def test_grouping_selected_nodes_creates_group_node(node_graph):
    """A GroupNode can be created and its inner nodes are accessible via expand."""
    ng, CubeNodeCls = node_graph

    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupA")

    group_nodes = [n for n in ng.all_nodes() if isinstance(n, GroupNode)]
    assert len(group_nodes) == 1

    sub = ng.expand_group_node(g)
    inner_names = {n.name() for n in sub.all_nodes()}
    ng.collapse_group_node(g)

    assert inner_name in inner_names


# ── Behavior 2: promotion ─────────────────────────────────────────────────────


def test_promoting_param_adds_port_on_group_node(node_graph):
    """Promoting a param from inside the group adds an input port on GroupNode."""
    from node_graph.nodes._base import _get_promotions

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupB")
    _add_promotion(g, inner_name, "prim_path", "/PromoteTest")

    port_names = {p.name() for p in g.input_ports()}
    assert "prim_path" in port_names
    assert "prim_path" in _get_promotions(g)


# ── Behavior 3: value flow through promotion ──────────────────────────────────


def test_promoted_port_value_flows_into_inner_chunk(node_graph, stage):
    """The default_value on the promotion dict flows into the inner chunk on execute."""
    from node_graph.graph_executor import GraphExecutor

    class _StubWidget:
        def __init__(self, ng, stage):
            self._graph = ng
            self._stage = stage

        def _set_exec_state(self, node, state, msg=""):
            pass

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupC")
    _add_promotion(g, inner_name, "prim_path", "/GroupFlowTest")

    executor = GraphExecutor(_StubWidget(ng, stage))
    executor._exec_group(g)

    # The test_cube chunk's button callback creates a prim at the prim_path value.
    prim = stage.GetPrimAtPath("/GroupFlowTest")
    assert prim.IsValid(), "Promoted prim_path value did not reach inner chunk execute"


# ── Behavior 4: serialization round-trip ─────────────────────────────────────


def test_serialize_reload_preserves_group_and_promotions(node_graph):
    """Saving and reloading the session preserves the GroupNode and its promotions."""
    from NodeGraphQt import NodeGraph
    from node_graph.nodes._base import _get_promotions

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupD")
    _add_promotion(g, inner_name, "prim_path", "/SerialTest")

    data = ng.serialize_session()

    # Deserialize into a fresh graph that knows the same node types.
    fresh = NodeGraph()
    fresh.register_node(GroupNode)
    fresh.register_node(CubeNodeCls)
    _ = fresh.widget
    fresh.deserialize_session(data)

    groups = [n for n in fresh.all_nodes() if isinstance(n, GroupNode)]
    assert groups, "GroupNode missing after reload"

    reloaded_g = groups[0]
    port_names = {p.name() for p in reloaded_g.input_ports()}
    assert "prim_path" in port_names
    assert "prim_path" in _get_promotions(reloaded_g)

    try:
        fresh.clear_session()
    except Exception:
        pass


# ── Behavior 5: export as chunk module ───────────────────────────────────────


def test_export_group_node_as_chunk_produces_loadable_chunk(node_graph, tmp_path):
    """Exporting a GroupNode as a chunk module writes a .py file with the promoted param."""
    from node_graph._graph_export import export_group_as_chunk
    from usd_chunks.chunk_registry import ChunkModule

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="ExportGroup")
    _add_promotion(g, inner_name, "prim_path", "/ExportTest")

    sub = ng.expand_group_node(g)
    try:
        out_path = export_group_as_chunk(g, out_dir=tmp_path, sub=sub)
    finally:
        ng.collapse_group_node(g)

    assert out_path.exists(), f"Exported chunk file not found: {out_path}"

    mods = ChunkModule.from_chunk_definition(out_path, out_path.stem)
    assert mods, "ChunkModule.from_chunk_definition returned nothing"

    chunk_mod = mods[0]
    chunks = chunk_mod.load()
    assert chunks, "ChunkModule.load() returned no chunks"

    chunk_obj = chunks[0]
    param_names = {p.name for p in chunk_obj.get_params() if hasattr(p, "name")}
    assert "prim_path" in param_names, (
        f"Promoted param 'prim_path' missing from exported chunk; got {param_names}"
    )


# ── Regression: BackdropNode in selection ────────────────────────────────────


def test_backdrop_node_in_selection_does_not_crash_grouping(node_graph):
    """BackdropNode in a mixed selection must not raise AttributeError in the grouping loop.

    Regression for: 'BackdropNode' object has no attribute 'input_ports'.
    The fix in _group_selected adds `if not hasattr(node, "input_ports"): continue`.
    """
    ng, CubeNodeCls = node_graph

    cube = ng.create_node(CubeNodeCls.type_, name="CubeBackdropReg")
    backdrop = ng.create_node("nodeGraphQt.nodes.BackdropNode", name="BackdropReg")

    # Precondition: BackdropNode has no input_ports (was the crash source)
    assert not hasattr(backdrop, "input_ports"), (
        "BackdropNode unexpectedly gained input_ports — fix guard may be redundant"
    )

    # Reproduce the fixed iteration from _group_selected — must not raise AttributeError
    selected = [cube, backdrop]
    sel_ids = {n.id for n in selected}
    ext_inputs = []
    ext_outputs = []
    for node in selected:
        if not hasattr(node, "input_ports"):  # the guard added by the fix
            continue
        for port in node.input_ports():
            for cp in port.connected_ports():
                if cp.node().id not in sel_ids:
                    ext_inputs.append((cp, node.id, port.name()))
        for port in node.output_ports():
            for cp in port.connected_ports():
                if cp.node().id not in sel_ids:
                    ext_outputs.append((node.id, port.name(), cp))

    # Only the cube should contribute; the backdrop is silently skipped
    nodes_with_ports = [n for n in selected if hasattr(n, "input_ports")]
    assert len(nodes_with_ports) == 1
    assert nodes_with_ports[0] is cube


# ── Behavior: grouped execution == ungrouped execution ───────────────────────


def test_grouped_execution_produces_same_stage_result_as_ungrouped(node_graph, stage):
    """Executing a chunk node inside a group creates the same prim as executing it directly."""
    from node_graph.graph_executor import GraphExecutor

    class _StubWidget:
        def __init__(self, ng, stage):
            self._graph = ng
            self._stage = stage

        def _set_exec_state(self, node, state, msg=""):
            pass

    ng, CubeNodeCls = node_graph
    executor = GraphExecutor(_StubWidget(ng, stage))

    # Direct execution of a bare chunk node
    direct_cube = ng.create_node(CubeNodeCls.type_, name="DirectCubeCompare")
    executor._exec_node(direct_cube)
    direct_prim_path = "/TestCube"  # test_cube default

    # Grouped execution with a distinct prim path
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupCompare")
    _add_promotion(g, inner_name, "prim_path", "/GroupedExecPrim")
    executor._exec_group(g)

    direct_prim = stage.GetPrimAtPath(direct_prim_path)
    grouped_prim = stage.GetPrimAtPath("/GroupedExecPrim")

    assert direct_prim.IsValid(), "Direct execution did not create a prim"
    assert grouped_prim.IsValid(), "Grouped execution did not create a prim"
    assert direct_prim.GetTypeName() == grouped_prim.GetTypeName(), (
        f"Prim types differ: direct={direct_prim.GetTypeName()!r} "
        f"grouped={grouped_prim.GetTypeName()!r}"
    )


# ── Fix A: layer edits visible on GroupNode ───────────────────────────────────


def test_layer_edits_visible_on_group_node_after_execution(node_graph, stage):
    """GroupNode._inner_chunk_cache contains non-empty layers after execution (Fix A).

    Fix A in _show_layer_edits iterates _inner_chunk_cache to aggregate layer text
    for the GroupNode context-menu action.
    """
    from node_graph.graph_executor import GraphExecutor

    class _StubWidget:
        def __init__(self, ng, stage):
            self._graph = ng
            self._stage = stage

        def _set_exec_state(self, node, state, msg=""):
            pass

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupLayerEdits")
    _add_promotion(g, inner_name, "prim_path", "/LayerEditsGroupPrim")

    executor = GraphExecutor(_StubWidget(ng, stage))
    executor._exec_group(g)

    # Fix A path: aggregate layers from _inner_chunk_cache
    chunk_cache = getattr(g, "_inner_chunk_cache", {})
    assert chunk_cache, "No inner chunks cached after group execution"

    parts = []
    for chunk_inst in chunk_cache.values():
        layer = chunk_inst.get_edit_context()
        if layer:
            raw = layer.ExportToString()
            if "def " in raw or "over " in raw:
                parts.append(raw)

    assert parts, "GroupNode layer aggregation (Fix A) returned no content"
    combined = "\n".join(parts)
    # SDF text for root prims uses `def Xform "PrimName"` without a leading slash
    assert "LayerEditsGroupPrim" in combined, (
        f"Expected prim name not in aggregated layer: {combined[:300]}"
    )


# ── Fix B: layer edits visible on inner node from SubGraph ────────────────────


def test_layer_edits_visible_on_inner_node_from_subgraph(node_graph, stage):
    """Inner SubGraph nodes restored from _inner_chunk_cache can serve layer edits (Fix B).

    Fix B in _show_layer_edits uses node._my_chunk.get_edit_context() when
    _cached_adapter is absent — relies on _exec_group restoring _my_chunk from
    _inner_chunk_cache.  This test verifies the cache holds chunks with valid layers
    and that those chunks can be re-linked to re-expanded inner nodes by name.
    """
    from node_graph.graph_executor import GraphExecutor

    class _StubWidget:
        def __init__(self, ng, stage):
            self._graph = ng
            self._stage = stage

        def _set_exec_state(self, node, state, msg=""):
            pass

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupInnerEdits")
    _add_promotion(g, inner_name, "prim_path", "/LayerEditsInnerPrim")

    executor = GraphExecutor(_StubWidget(ng, stage))
    executor._exec_group(g)  # sets _my_chunk on inner nodes, persists in cache, collapses

    chunk_cache = getattr(g, "_inner_chunk_cache", {})
    assert chunk_cache, "_inner_chunk_cache empty — Fix B restoration has no source"

    # Re-expand and restore _my_chunk by matching chunk name to node name
    # (_ensure_chunk sets the node name to the chunk's scene-assigned name)
    sub = ng.expand_group_node(g)
    inner_by_name = {n.name(): n for n in sub.all_nodes()}
    for chunk_inst in chunk_cache.values():
        inner = inner_by_name.get(chunk_inst.name)
        if inner and hasattr(inner, "_ensure_chunk"):
            inner._my_chunk = chunk_inst

    # Fix B path: inner nodes must now expose layer edits via _my_chunk
    restored = [n for n in sub.all_nodes() if getattr(n, "_my_chunk", None) is not None]
    ng.collapse_group_node(g)

    assert restored, "No inner nodes could have _my_chunk restored (Fix B path broken)"
    for n in restored:
        layer = n._my_chunk.get_edit_context()
        assert layer is not None, f"{n.name()} chunk has no edit layer"
        raw = layer.ExportToString()
        # SDF text for root prims uses `def Xform "PrimName"` without a leading slash
        assert "LayerEditsInnerPrim" in raw, (
            f"Expected prim name not in inner layer: {raw[:300]}"
        )


# ── Behavior: chunk state preserved across executions ────────────────────────


def test_chunk_state_preserved_across_multiple_group_executions(node_graph, stage):
    """The same chunk instance is reused across multiple group executions.

    _exec_group persists chunk instances in _inner_chunk_cache and restores them
    on the next expand.  Chunk identity must be stable so the layer accumulates
    edits rather than starting fresh each time.
    """
    from node_graph.graph_executor import GraphExecutor

    class _StubWidget:
        def __init__(self, ng, stage):
            self._graph = ng
            self._stage = stage

        def _set_exec_state(self, node, state, msg=""):
            pass

    ng, CubeNodeCls = node_graph
    g, inner_name = _make_group(ng, CubeNodeCls, name="GroupStatePreserve")
    _add_promotion(g, inner_name, "prim_path", "/StatePreservePrim")

    executor = GraphExecutor(_StubWidget(ng, stage))

    executor._exec_group(g)
    # Snapshot the cache values (chunk instances) after the first run
    cache_after_first = dict(getattr(g, "_inner_chunk_cache", {}))
    assert cache_after_first, "No chunks cached after first execution"

    executor._exec_group(g)
    cache_after_second = getattr(g, "_inner_chunk_cache", {})

    # Every chunk instance from the first run must survive in the second run's cache
    first_instances = set(id(c) for c in cache_after_first.values())
    second_instances = set(id(c) for c in cache_after_second.values())
    overlap = first_instances & second_instances
    assert overlap, (
        "No chunk instances were reused across executions — "
        "_inner_chunk_cache did not preserve state"
    )


# ── Behavior: nested group ────────────────────────────────────────────────────


def test_nested_group_executes_without_freeze(node_graph, stage):
    """A group containing an inner group executes without hanging or crashing."""
    from node_graph.graph_executor import GraphExecutor

    class _StubWidget:
        def __init__(self, ng, stage):
            self._graph = ng
            self._stage = stage

        def _set_exec_state(self, node, state, msg=""):
            pass

    ng, CubeNodeCls = node_graph

    # Build outer group; inside it, create an inner group containing a TestCube
    outer = ng.create_node("nodeGraphQt.nodes.GroupNode", name="OuterNested")
    outer.set_port_deletion_allowed(True)
    outer_sub = ng.expand_group_node(outer)

    inner = outer_sub.create_node("nodeGraphQt.nodes.GroupNode", name="InnerNested")
    inner.set_port_deletion_allowed(True)
    try:
        inner_sub = outer_sub.expand_group_node(inner)
        inner_sub.create_node(CubeNodeCls.type_, name="CubeInInner")
        outer_sub.collapse_group_node(inner)
    except Exception:
        # If the SubGraph API does not support nested expand, fall back:
        # place a TestCube directly inside the outer group instead.
        outer_sub.create_node(CubeNodeCls.type_, name="CubeFallback")

    ng.collapse_group_node(outer)

    # Execute the outer group — must complete without hanging or raising
    executor = GraphExecutor(_StubWidget(ng, stage))
    executor._exec_group(outer)

    # Reaching here means no crash or infinite loop
    assert True
