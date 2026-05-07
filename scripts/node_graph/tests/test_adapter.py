"""
ChunkAdapter tests — behavior 2.4.

Tests verify ChunkAdapter param filtering, value setting, and reference handling
without a live usdview scene (scene is None → graceful no-ops).
"""
import pytest


# ── Behavior 1: graceful no-scene handling ────────────────────────────────────

def test_adapter_with_no_scene_has_no_chunk(setup_registry):
    """ChunkAdapter returns chunk=None when the scene has no matching chunk."""
    from node_graph.chunk_adapter import ChunkAdapter

    adapter = ChunkAdapter("NonExistent__xyz", auto_create=False)
    assert adapter.chunk is None


def test_get_params_returns_empty_for_missing_chunk(setup_registry):
    """get_params() returns [] when no chunk is bound."""
    from node_graph.chunk_adapter import ChunkAdapter

    adapter = ChunkAdapter("NonExistent__xyz")
    assert adapter.get_params() == []


def test_set_param_is_noop_for_missing_chunk(setup_registry):
    """set_param() is a no-op when no chunk is bound (no exception raised)."""
    from node_graph.chunk_adapter import ChunkAdapter

    adapter = ChunkAdapter("NonExistent__xyz")
    adapter.set_param("some_param", 42)  # must not raise


def test_press_button_is_noop_for_missing_chunk(setup_registry):
    """press_button() is a no-op when no chunk is bound."""
    from node_graph.chunk_adapter import ChunkAdapter

    adapter = ChunkAdapter("NonExistent__xyz")
    adapter.press_button()  # must not raise


# ── Behavior 2: get_params excludes non-drivable types ───────────────────────

def test_get_params_excludes_button_params(node_graph, stage, setup_registry):
    """get_params() never returns Button params — they are not drivable via ports."""
    from node_graph.chunk_adapter import ChunkAdapter
    from usd_chunks.chunk import Button

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    my_chunk = node._ensure_chunk()
    assert my_chunk is not None

    adapter = ChunkAdapter(my_chunk.name, chunk=my_chunk)
    params = adapter.get_params()
    assert not any(isinstance(p, Button) for p in params), (
        f"Button found in get_params: {params}"
    )


def test_get_params_includes_lineedit(node_graph, stage, setup_registry):
    """get_params() includes standard value params like LineEdit."""
    from node_graph.chunk_adapter import ChunkAdapter

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    my_chunk = node._ensure_chunk()
    assert my_chunk is not None

    adapter = ChunkAdapter(my_chunk.name, chunk=my_chunk)
    param_names = [p.name for p in adapter.get_params() if hasattr(p, 'name')]
    assert "prim_path" in param_names, f"Expected prim_path in {param_names}"


# ── Behavior 3: set_param value handling ─────────────────────────────────────

def test_set_param_updates_chunk_value(node_graph, stage, setup_registry):
    """set_param() writes a concrete value into the named chunk param."""
    from node_graph.chunk_adapter import ChunkAdapter

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    my_chunk = node._ensure_chunk()
    assert my_chunk is not None

    adapter = ChunkAdapter(my_chunk.name, chunk=my_chunk)
    adapter.set_param("prim_path", "/AdapterTest")

    param = my_chunk.get_param("prim_path")
    assert param is not None
    assert str(param.value) == "/AdapterTest"


def test_set_param_with_at_reference_installs_binding(node_graph, stage, setup_registry):
    """set_param('@...') installs a value_reference, not a concrete value."""
    from node_graph.chunk_adapter import ChunkAdapter

    ng, CubeNodeCls = node_graph
    node = ng.create_node(CubeNodeCls.type_)
    my_chunk = node._ensure_chunk()
    assert my_chunk is not None

    adapter = ChunkAdapter(my_chunk.name, chunk=my_chunk)
    param = my_chunk.get_param("prim_path")
    if param is None or not hasattr(param, 'value_reference'):
        pytest.skip("prim_path param has no value_reference support")

    adapter.set_param("prim_path", "@SomeNode.output")
    assert param.value_reference == "@SomeNode.output"


# ── Behavior 4: list_available ────────────────────────────────────────────────

def test_list_available_returns_list(setup_registry):
    """list_available() always returns a list (empty or populated)."""
    from node_graph.chunk_adapter import ChunkAdapter

    result = ChunkAdapter.list_available()
    assert isinstance(result, list)
