"""Tests for EstimateLightingNode, GeneratePanoramaNode, and their helpers."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent.parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def estimate_node(node_graph, qapp):
    from node_graph.nodes._lighting import EstimateLightingNode

    ng, _ = node_graph
    ng.register_node(EstimateLightingNode)
    node = ng.create_node(EstimateLightingNode.type_)
    return node


@pytest.fixture
def panorama_node(node_graph, qapp):
    from node_graph.nodes._lighting import GeneratePanoramaNode

    ng, _ = node_graph
    ng.register_node(GeneratePanoramaNode)
    node = ng.create_node(GeneratePanoramaNode.type_)
    return node


# ── Pure helper tests (no Qt needed) ─────────────────────────────────────────


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

    for bad in ["not json at all", "partial: {", "", "{bad json"]:
        result = _parse_sun_response(bad)
        assert result["sun_elevation_deg"] == pytest.approx(90.0), bad
        assert result["sun_azimuth_deg"] == pytest.approx(0.0), bad
        assert result["sun_color_r"] == pytest.approx(1.0), bad
        assert result["sun_intensity"] == pytest.approx(1.0), bad


def test_parse_sun_json_with_markdown_fences():
    from node_graph.nodes._lighting import _parse_sun_response

    payload = '```json\n{"sun_azimuth_deg": 90.0, "sun_elevation_deg": 30.0, "sun_color_r": 1.0, "sun_color_g": 0.9, "sun_color_b": 0.8, "sun_intensity": 3.0, "ambient_intensity": 0.5}\n```'
    result = _parse_sun_response(payload)
    assert result["sun_azimuth_deg"] == pytest.approx(90.0)
    assert result["sun_elevation_deg"] == pytest.approx(30.0)


def test_azimuth_elevation_to_rotation():
    from node_graph.nodes._lighting import _azimuth_elevation_to_rotation

    rx, ry, rz = _azimuth_elevation_to_rotation(0.0, 90.0)
    assert (rx, ry, rz) == pytest.approx((-90.0, 0.0, 0.0))

    rx, ry, rz = _azimuth_elevation_to_rotation(90.0, 0.0)
    assert (rx, ry, rz) == pytest.approx((0.0, 0.0, -90.0))

    rx, ry, rz = _azimuth_elevation_to_rotation(180.0, 45.0)
    assert (rx, ry, rz) == pytest.approx((-45.0, 0.0, -180.0))


# ── Registry tests ────────────────────────────────────────────────────────────


def test_estimate_lighting_in_all_nodes():
    from node_graph.nodes import ALL_NODES, EstimateLightingNode

    assert EstimateLightingNode in ALL_NODES


def test_generate_panorama_in_all_nodes():
    from node_graph.nodes import ALL_NODES, GeneratePanoramaNode

    assert GeneratePanoramaNode in ALL_NODES


def test_backplate_lighting_node_removed():
    import node_graph.nodes as ng_nodes

    assert not hasattr(ng_nodes, "BackplateLightingNode"), (
        "BackplateLightingNode was split into EstimateLightingNode + GeneratePanoramaNode "
        "and must not be exported from the nodes package"
    )


# ── EstimateLightingNode behaviour ────────────────────────────────────────────


def test_estimate_does_not_run_on_load(estimate_node, stage):
    """Default locked=True means no API call fires on graph load."""
    assert estimate_node.get_property("locked") is True
    with patch.object(estimate_node, "_call_estimate_sun") as mock_call:
        result = estimate_node.execute(stage)
    mock_call.assert_not_called()
    assert result == estimate_node._empty_result()


def test_estimate_missing_api_key_returns_empty(estimate_node, stage):
    estimate_node.set_property("locked", False)
    with patch.dict(os.environ, {}, clear=True):
        result = estimate_node.execute(stage)

    assert result == {
        k: ""
        for k in (
            "azimuth",
            "elevation",
            "color_r",
            "color_g",
            "color_b",
            "intensity",
            "ambient",
        )
    }
    assert estimate_node.get_property("locked") is False


def test_estimate_no_images_returns_empty(estimate_node, stage):
    estimate_node.set_property("locked", False)
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
        patch.object(estimate_node, "_get_input_value", return_value=None),
    ):
        result = estimate_node.execute(stage)

    assert result["azimuth"] == ""
    assert result["elevation"] == ""


def test_estimate_single_image_calls_api(estimate_node, stage):
    fake_sun = {
        "sun_azimuth_deg": 120.0,
        "sun_elevation_deg": 40.0,
        "sun_color_r": 1.0,
        "sun_color_g": 0.9,
        "sun_color_b": 0.8,
        "sun_intensity": 3.5,
        "ambient_intensity": 0.4,
    }
    estimate_node._cached_result = {}
    estimate_node.set_property("locked", False)

    def _fake_input(port):
        return "/photo_a.jpg" if port == "image_0" else None

    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
        patch.object(estimate_node, "_get_input_value", side_effect=_fake_input),
        patch.object(
            estimate_node, "_call_estimate_sun", return_value=fake_sun
        ) as mock_call,
    ):
        result = estimate_node.execute(stage)

    mock_call.assert_called_once_with("fake-key", ["/photo_a.jpg"])
    assert result["azimuth"] == "120.0"
    assert result["elevation"] == "40.0"
    assert estimate_node.get_property("locked") is True


def test_estimate_multiple_images_all_sent_to_gemini(estimate_node, stage):
    """All connected image ports are collected and passed in a single API call."""
    fake_sun = {
        "sun_azimuth_deg": 200.0,
        "sun_elevation_deg": 25.0,
        "sun_color_r": 0.9,
        "sun_color_g": 0.8,
        "sun_color_b": 0.6,
        "sun_intensity": 2.0,
        "ambient_intensity": 0.6,
    }
    estimate_node._cached_result = {}
    estimate_node.set_property("locked", False)

    wired = {
        "image_0": "/photo_a.jpg",
        "image_1": "/photo_b.jpg",
        "image_2": None,
        "image_3": None,
    }

    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
        patch.object(
            estimate_node, "_get_input_value", side_effect=lambda p: wired.get(p)
        ),
        patch.object(
            estimate_node, "_call_estimate_sun", return_value=fake_sun
        ) as mock_call,
    ):
        result = estimate_node.execute(stage)

    mock_call.assert_called_once_with("fake-key", ["/photo_a.jpg", "/photo_b.jpg"])
    assert result["azimuth"] == "200.0"
    assert estimate_node.get_property("locked") is True


def test_estimate_locked_node_skips_api(estimate_node, stage):
    cached = {
        "azimuth": "90.0",
        "elevation": "45.0",
        "color_r": "1.0",
        "color_g": "1.0",
        "color_b": "1.0",
        "intensity": "2.0",
        "ambient": "0.5",
    }
    estimate_node._cached_result = cached
    estimate_node.set_property("locked", True)

    with patch.object(estimate_node, "_call_estimate_sun") as mock_call:
        result = estimate_node.execute(stage)

    mock_call.assert_not_called()
    assert result == cached


# ── GeneratePanoramaNode behaviour ────────────────────────────────────────────


def test_panorama_does_not_run_on_load(panorama_node, stage):
    """Default locked=True means no API call fires on graph load."""
    assert panorama_node.get_property("locked") is True
    with patch.object(panorama_node, "_generate_panorama") as mock_gen:
        result = panorama_node.execute(stage)
    mock_gen.assert_not_called()
    assert result == {"image_path": ""}


def test_panorama_missing_api_key_returns_empty_path(panorama_node, stage):
    panorama_node.set_property(
        "locked", False
    )  # simulate user pressing "Generate Panorama"
    with patch.dict(os.environ, {}, clear=True):
        result = panorama_node.execute(stage)

    assert result == {"image_path": ""}
    assert panorama_node.get_property("locked") is False


def test_panorama_gather_sun_params_uses_defaults_when_unconnected(
    panorama_node, stage
):
    from node_graph.nodes._lighting import _SUN_DEFAULTS

    with patch.object(panorama_node, "_get_input_value", return_value=None):
        params = panorama_node._gather_sun_params()

    assert params["sun_azimuth_deg"] == pytest.approx(_SUN_DEFAULTS["sun_azimuth_deg"])
    assert params["sun_elevation_deg"] == pytest.approx(
        _SUN_DEFAULTS["sun_elevation_deg"]
    )
    assert params["sun_intensity"] == pytest.approx(_SUN_DEFAULTS["sun_intensity"])


def test_panorama_gather_sun_params_reads_wired_values(panorama_node, stage):
    wired = {
        "azimuth": "135.0",
        "elevation": "30.0",
        "color_r": "0.9",
        "color_g": "0.85",
        "color_b": "0.7",
        "intensity": "4.2",
        "ambient": "0.3",
    }

    with patch.object(
        panorama_node, "_get_input_value", side_effect=lambda n: wired.get(n)
    ):
        params = panorama_node._gather_sun_params()

    assert params["sun_azimuth_deg"] == pytest.approx(135.0)
    assert params["sun_elevation_deg"] == pytest.approx(30.0)
    assert params["sun_intensity"] == pytest.approx(4.2)
    assert params["ambient_intensity"] == pytest.approx(0.3)


def test_panorama_successful_generation_auto_locks(panorama_node, stage):
    fake_path = "/tmp/backplate_panorama_999.png"
    panorama_node._cached_result = {}
    panorama_node.set_property("locked", False)

    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
        patch.object(panorama_node, "_generate_panorama", return_value=fake_path),
    ):
        result = panorama_node.execute(stage)

    assert result == {"image_path": fake_path}
    assert panorama_node.get_property("locked") is True


def test_panorama_locked_node_skips_api(panorama_node, stage):
    cached = {"image_path": "/tmp/cached.png"}
    panorama_node._cached_result = cached
    panorama_node.set_property("locked", True)

    with patch.object(panorama_node, "_generate_panorama") as mock_gen:
        result = panorama_node.execute(stage)

    mock_gen.assert_not_called()
    assert result == cached


def test_panorama_failed_generation_does_not_lock(panorama_node, stage):
    panorama_node._cached_result = {}
    panorama_node.set_property("locked", False)

    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}),
        patch.object(panorama_node, "_generate_panorama", return_value=None),
    ):
        result = panorama_node.execute(stage)

    assert result == {"image_path": ""}
    assert panorama_node.get_property("locked") is False


# ── _apply_panorama_mode ──────────────────────────────────────────────────────


def test_apply_panorama_mode_pad_2_1(panorama_node, tmp_path):
    """A 1920×1080 (16:9) image should be padded to 2160×1080 (2:1) with black side bands."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        pytest.skip("pillow not installed")

    img_path = str(tmp_path / "test_pano.png")
    PILImage.new("RGB", (1920, 1080), (128, 64, 32)).save(img_path)

    panorama_node.set_property("panorama_mode", "pad_2_1")
    panorama_node._apply_panorama_mode(img_path)

    result = PILImage.open(img_path)
    w, h = result.size
    assert h == 1080, "Height should be preserved"
    assert w == 2160, f"Expected 2160 (2 × 1080), got {w}"
    assert w / h == pytest.approx(2.0)

    # Black padding at left edge (first 120 pixels on each side)
    left_pixel = list(result.getpixel((0, 540)))[:3]
    assert left_pixel == [0, 0, 0], (
        f"Left edge should be black padding, got {left_pixel}"
    )


def test_apply_panorama_mode_full_360_is_noop(panorama_node, tmp_path):
    """full_360 mode must not modify the file."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        pytest.skip("pillow not installed")

    img_path = str(tmp_path / "test_pano_360.png")
    PILImage.new("RGB", (1920, 1080), (64, 128, 255)).save(img_path)
    original_size = (1920, 1080)

    panorama_node.set_property("panorama_mode", "full_360")
    panorama_node._apply_panorama_mode(img_path)

    result = PILImage.open(img_path)
    assert result.size == original_size


def test_apply_panorama_mode_already_2_1_is_noop(panorama_node, tmp_path):
    """An image already at 2:1 (w = h×2) should not be re-padded."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        pytest.skip("pillow not installed")

    img_path = str(tmp_path / "test_pano_2_1.png")
    # 2048×1024 is exactly 2:1 (2048 = 1024 × 2)
    PILImage.new("RGB", (2048, 1024), (0, 255, 0)).save(img_path)

    panorama_node.set_property("panorama_mode", "pad_2_1")
    panorama_node._apply_panorama_mode(img_path)

    result = PILImage.open(img_path)
    assert result.size == (2048, 1024)


# ── CreateDistantLightNode rotation ──────────────────────────────────────────


def test_distant_light_applies_azimuth_elevation(node_graph, stage, qapp):
    import uuid
    from node_graph.nodes._lighting import CreateDistantLightNode
    from pxr import UsdGeom

    ng, _ = node_graph
    ng.register_node(CreateDistantLightNode)
    node = ng.create_node(CreateDistantLightNode.type_)

    path = f"/TestSunAzEl_{uuid.uuid4().hex[:8]}"
    node.set_property("path", path)
    node.set_property("azimuth", "90.0")
    node.set_property("elevation", "45.0")
    # Execute twice to confirm the "already exists" bug is fixed
    node.execute(stage)
    node.execute(stage)

    prim = stage.GetPrimAtPath(path)
    assert prim.IsValid()
    ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
    assert any("rotateXYZ" in op.GetOpName() for op in ops)


def test_distant_light_no_rotation_when_empty(node_graph, stage, qapp):
    import uuid
    from node_graph.nodes._lighting import CreateDistantLightNode
    from pxr import UsdGeom

    ng, _ = node_graph
    ng.register_node(CreateDistantLightNode)
    node = ng.create_node(CreateDistantLightNode.type_)

    path = f"/TestSunNoRot_{uuid.uuid4().hex[:8]}"
    node.set_property("path", path)
    node.set_property("azimuth", "")
    node.set_property("elevation", "")
    node.execute(stage)

    prim = stage.GetPrimAtPath(path)
    assert prim.IsValid()
    ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
    assert not any("rotateXYZ" in op.GetOpName() for op in ops)


def test_dome_light_rotation_inputs(node_graph, stage, qapp):
    import uuid
    from node_graph.nodes._lighting import CreateDomeLightNode
    from pxr import UsdGeom

    ng, _ = node_graph
    ng.register_node(CreateDomeLightNode)
    node = ng.create_node(CreateDomeLightNode.type_)

    path = f"/TestDomeRot_{uuid.uuid4().hex[:8]}"
    node.set_property("path", path)
    node.set_property("rotation_y", "90.0")
    # Execute twice — must not raise "already exists"
    node.execute(stage)
    node.execute(stage)

    prim = stage.GetPrimAtPath(path)
    assert prim.IsValid()
    ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
    assert any("rotateXYZ" in op.GetOpName() for op in ops)
