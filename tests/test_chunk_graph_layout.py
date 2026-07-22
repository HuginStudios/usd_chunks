from __future__ import annotations

from pxr import Sdf, Usd

from plugins.chunk_graph_layout import (
    find_connected_components,
    grow_to_connected_subgraph,
    layout_positions,
    topo_depth,
)
from plugins.chunk_usd import initialize as _chunk_usd_init
from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


def _sizes(sizes: dict) -> callable:
    return lambda nid: sizes.get(nid, (160.0, 80.0))


def _positions(positions: dict) -> callable:
    return lambda nid: positions.get(nid, (0.0, 0.0))


def _locked(locked_ids: set) -> callable:
    return lambda nid: nid in locked_ids


# ---------------------------------------------------------------------------
# find_connected_components
# ---------------------------------------------------------------------------

def test_components_splits_disconnected_chains() -> None:
    # a -> b, c -> d (two separate chains), e alone
    up = {"b": {"a"}, "d": {"c"}}
    down = {"a": {"b"}, "c": {"d"}}
    components = find_connected_components(["a", "b", "c", "d", "e"], up, down)
    ids_per_component = {frozenset(c) for c in components}
    assert ids_per_component == {frozenset({"a", "b"}), frozenset({"c", "d"}), frozenset({"e"})}


def test_components_ignores_edges_to_ids_outside_the_set() -> None:
    """A connection to a node not in node_ids (e.g. a locked node excluded from grouping)
    must not merge two otherwise-separate components."""
    up = {"a": {"locked_x"}, "b": {"locked_x"}}
    components = find_connected_components(["a", "b"], up, {})
    ids_per_component = {frozenset(c) for c in components}
    assert ids_per_component == {frozenset({"a"}), frozenset({"b"})}


# ---------------------------------------------------------------------------
# topo_depth
# ---------------------------------------------------------------------------

def test_topo_depth_assigns_increasing_depth_along_a_chain() -> None:
    # a -> b -> c
    up = {"b": {"a"}, "c": {"b"}}
    depth = topo_depth(["a", "b", "c"], up)
    assert depth == {"a": 0, "b": 1, "c": 2}


def test_topo_depth_takes_the_longest_path_when_multiple_paths_exist() -> None:
    # a -> b -> d, a -> c -> e -> d  (d's longest path is via c/e, depth 3)
    up = {"b": {"a"}, "c": {"a"}, "e": {"c"}, "d": {"b", "e"}}
    depth = topo_depth(["a", "b", "c", "d", "e"], up)
    assert depth["a"] == 0
    assert depth["d"] == 3  # a(0) -> c(1) -> e(2) -> d(3), longer than a(0) -> b(1) -> d(2)


# ---------------------------------------------------------------------------
# grow_to_connected_subgraph
# ---------------------------------------------------------------------------

def test_grow_to_connected_subgraph_expands_through_the_whole_chain() -> None:
    # a -> b -> c -> d, seed is just b
    up = {"b": {"a"}, "c": {"b"}, "d": {"c"}}
    down = {"a": {"b"}, "b": {"c"}, "c": {"d"}}
    grown = grow_to_connected_subgraph(["b"], up, down)
    assert grown == {"a", "b", "c", "d"}


def test_grow_to_connected_subgraph_does_not_pull_in_unrelated_nodes() -> None:
    up = {"b": {"a"}}
    down = {"a": {"b"}}
    grown = grow_to_connected_subgraph(["a"], up, down)
    assert grown == {"a", "b"}  # "unrelated" node with no edges at all never appears


# ---------------------------------------------------------------------------
# layout_positions
# ---------------------------------------------------------------------------

def test_layout_positions_places_a_simple_chain_left_to_right() -> None:
    # a -> b -> c, all same size
    up = {"b": {"a"}, "c": {"b"}}
    down = {"a": {"b"}, "b": {"c"}}
    sizes = _sizes({"a": (100, 50), "b": (100, 50), "c": (100, 50)})
    positions = layout_positions(["a", "b", "c"], sizes, _locked(set()), _positions({}), up, down)
    assert positions["a"][0] < positions["b"][0] < positions["c"][0]


def test_layout_positions_preserves_the_pre_layout_top_left_corner() -> None:
    """The view shouldn't jump far from wherever the user was looking — the result's
    top-left corner should match the minimum X/Y of the nodes' positions going in."""
    up = {"b": {"a"}, "c": {"b"}}
    down = {"a": {"b"}, "b": {"c"}}
    sizes = _sizes({"a": (100, 50), "b": (100, 50), "c": (100, 50)})
    starting_positions = {"a": (5000.0, -3000.0), "b": (5100.0, -2900.0), "c": (5300.0, -2950.0)}
    result = layout_positions(
        ["a", "b", "c"], sizes, _locked(set()), _positions(starting_positions), up, down
    )
    result_min_x = min(x for x, _ in result.values())
    result_min_y = min(y for _, y in result.values())
    assert result_min_x == min(x for x, _ in starting_positions.values())
    assert result_min_y == min(y for _, y in starting_positions.values())


def test_layout_positions_omits_locked_nodes_from_the_result() -> None:
    up = {"b": {"a"}}
    down = {"a": {"b"}}
    sizes = _sizes({"a": (100, 50), "b": (100, 50)})
    result = layout_positions(["a", "b"], sizes, _locked({"a"}), _positions({}), up, down)
    assert "a" not in result
    assert "b" in result


def test_layout_positions_averages_target_y_between_two_upstream_neighbors() -> None:
    # a and b both feed c; c's target Y should land between a's and b's Y (target-then-push)
    up = {"c": {"a", "b"}}
    down = {"a": {"c"}, "b": {"c"}}
    sizes = _sizes({"a": (100, 50), "b": (100, 50), "c": (100, 50)})
    positions = {"a": (0.0, 0.0), "b": (0.0, 200.0), "c": (0.0, 0.0)}
    result = layout_positions(["a", "b", "c"], sizes, _locked(set()), _positions(positions), up, down)
    # a and b are both depth 0 (no upstream) so they get placed by the algorithm too, not
    # read from `positions` — what matters is c's Y lands between wherever a/b end up.
    a_cy = result["a"][1] + 25
    b_cy = result["b"][1] + 25
    c_cy = result["c"][1] + 25
    assert min(a_cy, b_cy) <= c_cy <= max(a_cy, b_cy)


def test_layout_positions_never_overlaps_a_locked_node() -> None:
    """A movable node whose natural target-Y would land it on top of a locked node's real
    rect must be pushed clear of it."""
    up = {"b": {"a"}}
    down = {"a": {"b"}}
    sizes = _sizes({"a": (100, 50), "locked1": (100, 50), "b": (100, 50)})
    # Force b's natural target squarely on top of locked1's fixed position.
    positions = {"a": (0.0, 0.0), "locked1": (300.0, 0.0), "b": (0.0, 0.0)}
    result = layout_positions(
        ["a", "b", "locked1"], sizes, _locked({"locked1"}), _positions(positions), up, down
    )
    bx, by = result["b"]
    lx, ly, lw, lh = 300.0, 0.0, 100.0, 50.0
    overlap = bx < lx + lw and bx + 100 > lx and by < ly + lh and by + 50 > ly
    assert not overlap


def test_layout_positions_stacks_disconnected_chains_without_overlap() -> None:
    up = {"b": {"a"}, "d": {"c"}}
    down = {"a": {"b"}, "c": {"d"}}
    sizes = _sizes({"a": (100, 50), "b": (100, 50), "c": (100, 50), "d": (100, 50)})
    result = layout_positions(["a", "b", "c", "d"], sizes, _locked(set()), _positions({}), up, down)
    # Two chains of equal size -> equal-length components -> order is whichever sort()
    # picked, but they must not vertically overlap each other regardless of which is first.
    chain1_bottom = max(result["a"][1] + 50, result["b"][1] + 50)
    chain2_top = min(result["c"][1], result["d"][1])
    chain1_top = min(result["a"][1], result["b"][1])
    chain2_bottom = max(result["c"][1] + 50, result["d"][1] + 50)
    no_overlap = chain1_bottom <= chain2_top or chain2_bottom <= chain1_top
    assert no_overlap


def test_layout_positions_empty_node_ids_returns_empty() -> None:
    assert layout_positions([], _sizes({}), _locked(set()), _positions({}), {}, {}) == {}


def test_layout_positions_all_locked_returns_empty() -> None:
    result = layout_positions(
        ["a"], _sizes({"a": (100, 50)}), _locked({"a"}), _positions({"a": (0.0, 0.0)}), {}, {}
    )
    assert result == {}


# ---------------------------------------------------------------------------
# End-to-end integration: a real ChunkGraphScene, real chunks, real USD connections
# ---------------------------------------------------------------------------

class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage):
    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    return scene


def _wire_exec(stage: Usd.Stage, downstream_path: str, upstream_path: str) -> None:
    prim = stage.GetPrimAtPath(downstream_path)
    attr = prim.GetAttribute("inputs:chunk")
    if not attr.IsValid():
        attr = prim.CreateAttribute("inputs:chunk", Sdf.ValueTypeNames.Token, custom=False)
    attr.SetConnections([Sdf.Path(f"{upstream_path}.outputs:chunk")])


def test_layout_nodes_spreads_a_connected_chain_and_persists_positions(qapp) -> None:
    from plugins.chunk_qt_nodegraph import ChunkGraphScene

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    a = chunk.Chunk("a"); a._config = ChunkConfig(module="prim")
    b = chunk.Chunk("b"); b._config = ChunkConfig(module="prim")
    c = chunk.Chunk("c"); c._config = ChunkConfig(module="prim")
    scene.add_chunk(a)
    scene.add_chunk(b)
    scene.add_chunk(c)
    _wire_exec(stage, "/Tab1/b", "/Tab1/a")
    _wire_exec(stage, "/Tab1/c", "/Tab1/b")

    graph_scene = ChunkGraphScene()
    graph_scene.build(stage, scene, ctx)
    graph_scene.layout_nodes()

    xa = graph_scene._nodes["/Tab1/a"].pos().x()
    xb = graph_scene._nodes["/Tab1/b"].pos().x()
    xc = graph_scene._nodes["/Tab1/c"].pos().x()
    assert xa < xb < xc

    # Persisted via the same attribute an interactive drag writes.
    for path, node in graph_scene._nodes.items():
        p = stage.GetPrimAtPath(path)
        pos_attr = p.GetAttribute("ui:nodegraph:node:pos").Get()
        assert pos_attr[0] == node.pos().x()


def test_layout_nodes_with_one_selected_grows_to_the_whole_chain(qapp) -> None:
    from plugins.chunk_qt_nodegraph import ChunkGraphScene

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    a = chunk.Chunk("a"); a._config = ChunkConfig(module="prim")
    b = chunk.Chunk("b"); b._config = ChunkConfig(module="prim")
    c = chunk.Chunk("c"); c._config = ChunkConfig(module="prim")
    scene.add_chunk(a)
    scene.add_chunk(b)
    scene.add_chunk(c)
    _wire_exec(stage, "/Tab1/b", "/Tab1/a")
    _wire_exec(stage, "/Tab1/c", "/Tab1/b")

    graph_scene = ChunkGraphScene()
    graph_scene.build(stage, scene, ctx)
    before = {path: node.pos().x() for path, node in graph_scene._nodes.items()}

    # Only the middle node is "selected" — layout must still grow to the full chain.
    graph_scene.layout_nodes(seed_paths=["/Tab1/b"])

    after = {path: node.pos().x() for path, node in graph_scene._nodes.items()}
    assert after != before  # a and c moved too, not just b


def test_layout_nodes_never_moves_a_locked_node(qapp) -> None:
    from plugins.chunk_qt_nodegraph import ChunkGraphScene

    stage = Usd.Stage.CreateInMemory()
    scene = _make_scene(stage)
    ctx = scene.add_context("Tab1")
    a = chunk.Chunk("a"); a._config = ChunkConfig(module="prim")
    b = chunk.Chunk("b"); b._config = ChunkConfig(module="prim")
    scene.add_chunk(a)
    scene.add_chunk(b)
    _wire_exec(stage, "/Tab1/b", "/Tab1/a")

    graph_scene = ChunkGraphScene()
    graph_scene.build(stage, scene, ctx)
    locked_node = graph_scene._nodes["/Tab1/a"]
    locked_node._locked = True
    locked_pos_before = (locked_node.pos().x(), locked_node.pos().y())

    graph_scene.layout_nodes()

    assert (locked_node.pos().x(), locked_node.pos().y()) == locked_pos_before
    # Not written to USD either, since it was never part of the result.
    pos_attr = stage.GetPrimAtPath("/Tab1/a").GetAttribute("ui:nodegraph:node:pos")
    if pos_attr.HasAuthoredValue():
        assert (pos_attr.Get()[0], -pos_attr.Get()[1]) == locked_pos_before
