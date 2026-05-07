"""
Base helper tests — behavior 2.5.

Tests verify _get_promotions, _save_promotions, _get_node_port_types, and the
TRIGGER sentinel — the small utility functions that multiple modules depend on.
"""

import json
import pytest

from NodeGraphQt.nodes.group_node import GroupNode
from node_graph.nodes._base import (
    _get_promotions,
    _get_promoted_outputs,
    _save_promotions,
    _get_node_port_types,
    TRIGGER,
    _TriggerSignal,
    UsdNodeBase,
    PORT_COLORS,
)


# ── Behavior 1: GroupNode promotion helpers ───────────────────────────────────


def test_get_promotions_returns_empty_for_fresh_group(node_graph):
    """A newly-created GroupNode has no promotions."""
    ng, _CubeNodeCls = node_graph
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name="FreshGroup")
    assert _get_promotions(g) == {}


def test_save_and_reload_promotions_round_trips(node_graph):
    """_save_promotions persists data that _get_promotions can restore."""
    ng, _CubeNodeCls = node_graph
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name="RoundTripGroup")

    promos = _get_promotions(g)
    promos["my_param"] = {
        "inner_node_id": "abc123",
        "inner_node_name": "InnerCube",
        "inner_param": "prim_path",
        "default_value": "/Saved",
    }
    _save_promotions(g)

    # Clear the cached attribute so the next call reads from model.
    del g._promotions

    reloaded = _get_promotions(g)
    assert "my_param" in reloaded
    assert reloaded["my_param"]["default_value"] == "/Saved"


def test_save_promotions_writes_valid_json(node_graph):
    """_save_promotions stores valid JSON in model.custom_properties."""
    ng, _CubeNodeCls = node_graph
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name="JsonGroup")

    promos = _get_promotions(g)
    promos["p"] = {"default_value": 42}
    _save_promotions(g)

    raw = g.model.custom_properties.get("promoted_params", "{}")
    parsed = json.loads(raw)
    assert parsed["p"]["default_value"] == 42


def test_get_promoted_outputs_returns_empty_for_fresh_group(node_graph):
    """_get_promoted_outputs returns {} for a new GroupNode."""
    ng, _CubeNodeCls = node_graph
    g = ng.create_node("nodeGraphQt.nodes.GroupNode", name="OutGroup")
    assert _get_promoted_outputs(g) == {}


# ── Behavior 2: port-type introspection ──────────────────────────────────────


def test_get_node_port_types_returns_dict_with_expected_keys(node_graph):
    """_get_node_port_types returns a dict with 'inputs' and 'outputs' sets."""
    from node_graph.nodes._input import PrimPathNode

    result = _get_node_port_types(PrimPathNode)
    assert "inputs" in result
    assert "outputs" in result
    assert isinstance(result["inputs"], set)
    assert isinstance(result["outputs"], set)


def test_get_node_port_types_always_includes_trigger_in_inputs(node_graph):
    """All UsdNodeBase nodes accept a trigger input (hidden but always present)."""
    from node_graph.nodes._input import FloatNode

    result = _get_node_port_types(FloatNode)
    assert "trigger" in result["inputs"]


def test_get_node_port_types_is_cached(node_graph):
    """Calling _get_node_port_types twice returns the same dict object (cache hit)."""
    from node_graph.nodes._input import StringNode
    import node_graph.nodes._base as _base_mod

    _base_mod._PORT_TYPE_CACHE.pop(StringNode, None)  # clear to ensure fresh call
    first = _get_node_port_types(StringNode)
    second = _get_node_port_types(StringNode)
    assert first is second


# ── Behavior 3: TRIGGER sentinel ─────────────────────────────────────────────


def test_trigger_sentinel_is_instance_of_trigger_signal():
    """TRIGGER is a _TriggerSignal, used to distinguish trigger from data ports."""
    assert isinstance(TRIGGER, _TriggerSignal)


def test_trigger_sentinel_has_readable_repr():
    """TRIGGER repr is informative for debugging."""
    assert "trigger" in repr(TRIGGER).lower()


# ── Behavior 4: PORT_COLORS completeness ─────────────────────────────────────


def test_port_colors_contains_required_types():
    """PORT_COLORS must define at minimum: prim, string, bool, float, trigger, layer."""
    required = {"prim", "string", "bool", "float", "trigger", "layer"}
    assert required.issubset(PORT_COLORS.keys()), (
        f"Missing port types: {required - PORT_COLORS.keys()}"
    )


def test_port_colors_values_are_rgb_triples():
    """Each PORT_COLORS value is a 3-tuple of ints in [0, 255]."""
    for name, color in PORT_COLORS.items():
        assert len(color) == 3, f"PORT_COLORS[{name!r}] is not a 3-tuple"
        assert all(0 <= c <= 255 for c in color), (
            f"PORT_COLORS[{name!r}] has out-of-range value: {color}"
        )
