"""BackplateLightingNode tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent.parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def backplate_graph(node_graph, qapp):
    """node_graph with BackplateLightingNode registered."""
    from node_graph.nodes._lighting import BackplateLightingNode

    ng, CubeNodeCls = node_graph
    ng.register_node(BackplateLightingNode)
    return ng, BackplateLightingNode


# ── Pure function tests (no Qt needed) ────────────────────────────────────────


def test_parse_sun_json_valid():
    from node_graph.nodes._lighting import _parse_sun_response

    payload = json.dumps(
        {
            "sun_azimuth_deg": 135.0,
            "sun_elevation_deg": 45.0,
            "sun_color_r": 0.9,
            "sun_color_g": 0.85,
            "sun_color_b": 0.7,
            "sun_intensity": 4.2,
            "ambient_intensity": 0.3,
        }
    )
    result = _parse_sun_response(payload)

    assert result["sun_azimuth_deg"] == pytest.approx(135.0)
    assert result["sun_elevation_deg"] == pytest.approx(45.0)
    assert result["sun_color_r"] == pytest.approx(0.9)
    assert result["sun_color_g"] == pytest.approx(0.85)
    assert result["sun_color_b"] == pytest.approx(0.7)
    assert result["sun_intensity"] == pytest.approx(4.2)
    assert result["ambient_intensity"] == pytest.approx(0.3)


def test_parse_sun_json_malformed():
    from node_graph.nodes._lighting import _parse_sun_response

    for bad_input in ["not json at all", "partial: {", "", "{bad json"]:
        result = _parse_sun_response(bad_input)
        assert result["sun_elevation_deg"] == pytest.approx(90.0), bad_input
        assert result["sun_azimuth_deg"] == pytest.approx(0.0), bad_input
        assert result["sun_color_r"] == pytest.approx(1.0), bad_input
        assert result["sun_color_g"] == pytest.approx(1.0), bad_input
        assert result["sun_color_b"] == pytest.approx(1.0), bad_input
        assert result["sun_intensity"] == pytest.approx(1.0), bad_input


def test_azimuth_elevation_to_rotation():
    from node_graph.nodes._lighting import _azimuth_elevation_to_rotation

    # Sun directly overhead (elevation=90, azimuth=0)
    rx, ry, rz = _azimuth_elevation_to_rotation(0.0, 90.0)
    assert (rx, ry, rz) == pytest.approx((-90.0, 0.0, 0.0))

    # Sun due east at horizon (elevation=0, azimuth=90)
    rx, ry, rz = _azimuth_elevation_to_rotation(90.0, 0.0)
    assert (rx, ry, rz) == pytest.approx((0.0, 0.0, -90.0))

    # Sun due south at 45° elevation (elevation=45, azimuth=180)
    rx, ry, rz = _azimuth_elevation_to_rotation(180.0, 45.0)
    assert (rx, ry, rz) == pytest.approx((-45.0, 0.0, -180.0))


# ── Node-level tests (need Qt + stage) ───────────────────────────────────────


def test_execute_missing_api_key(backplate_graph, stage):
    ng, BackplateLightingNode = backplate_graph
    node = ng.create_node(BackplateLightingNode.type_)

    with patch.dict(os.environ, {}, clear=True):
        result = node.execute(stage)

    assert result == {"dome_light": None, "sun_light": None}


def test_execute_no_image_input(backplate_graph, stage):
    ng, BackplateLightingNode = backplate_graph
    node = ng.create_node(BackplateLightingNode.type_)

    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}):
        result = node.execute(stage)

    assert result == {"dome_light": None, "sun_light": None}


def test_node_registered_in_all_nodes():
    from node_graph.nodes import ALL_NODES, BackplateLightingNode

    assert BackplateLightingNode in ALL_NODES
