"""
Group node tests — behavior 2.2.

Tests verify that GroupNode works correctly: grouping, param promotion,
value flow through execution, serialization/reload, and export as chunk module.

Each test builds up a GroupNode manually via the public NodeGraphQt API
(create_node / expand_group_node / collapse_group_node) rather than going
through the widget's keyboard shortcuts, which are not available in headless
tests.
"""

import json
import pytest

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
