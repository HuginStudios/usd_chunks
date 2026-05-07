"""
Chunk node tests — behavior 2.1.

Tests verify that chunk nodes work correctly end-to-end without usdview:
discovery → instantiation → execution → USD output.
"""

import pytest
from pxr import Usd


# ── Behavior 1: discovery ─────────────────────────────────────────────────────


def test_test_cube_module_is_registered(setup_registry):
    """test_cube module is registered in ChunkRegistry after setup."""
    from usd_chunks.chunk_registry import ChunkRegistry

    mod = ChunkRegistry().get_chunk_module("test_cube")
    assert mod is not None
    assert mod.module_name == "test_cube"


# ── Behavior 2: instantiation ─────────────────────────────────────────────────


def test_chunk_node_can_be_added_to_graph(node_graph):
    """A chunk node class can be registered and added to the graph."""
    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    assert node is not None


def test_chunk_node_has_expected_ports(node_graph):
    """Chunk node exposes prim_path input port and prim/layer output ports."""
    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    input_names = [p.name() for p in node.input_ports()]
    output_names = [p.name() for p in node.output_ports()]
    assert "_prim_path" in input_names, f"expected _prim_path in {input_names}"
    assert "prim" in output_names
    assert "layer" in output_names


# ── Behavior 3: execution authors USD ────────────────────────────────────────


def test_chunk_node_execute_authors_to_layer(node_graph, stage):
    """Executing a chunk node causes it to author a prim to its layer."""
    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)

    result = node.execute(stage)

    # execute() must return a dict
    assert isinstance(result, dict)
    assert "prim" in result
    assert "layer" in result

    layer = result["layer"]
    assert layer is not None, "chunk node returned no layer"
    assert len(layer.rootPrims) > 0, "chunk layer has no authored prims"


# ── Behavior 4: port value flows into chunk param ─────────────────────────────


def test_chunk_node_port_value_flows_to_param(node_graph, stage):
    """Setting a port property before execute pushes the value into the chunk."""
    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    node.set_property("prim_path", "/PortFlowTest")

    result = node.execute(stage)

    layer = result.get("layer")
    assert layer is not None
    prim_paths = [str(s.path) for s in layer.rootPrims]
    assert "/PortFlowTest" in prim_paths, f"expected /PortFlowTest in {prim_paths}"


# ── Behavior 5: execute is idempotent (re-execute replaces, not duplicates) ───


def test_chunk_node_execute_twice_does_not_duplicate_prims(node_graph, stage):
    """Executing the same node twice doesn't duplicate prims in the layer."""
    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    node.set_property("prim_path", "/IdempotentTest")

    node.execute(stage)
    result = node.execute(stage)

    layer = result.get("layer")
    assert layer is not None
    count = sum(1 for s in layer.rootPrims if str(s.path) == "/IdempotentTest")
    assert count == 1, f"expected 1 prim at /IdempotentTest, got {count}"
