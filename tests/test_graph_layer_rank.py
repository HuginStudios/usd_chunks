"""The chunk next to the view wins, across graph layers too.

Chunk layers were ranked only within their own graph layer, and the graph layers stayed in
the order they were made, newest on top. A chunk that builds its own children can end up
with them in a graph layer of its own, so in the kitchen layout four AMD MaterialX Library
graph layers sat above the tab's, and AddMesh -- the last chunk before the view -- was
outranked by all of them.

Tested on the ordering step itself, with graph layers set up by hand: which DAG component a
chunk's layer lands in depends on the order a scene was loaded and cooked in, and that is
not what this is about.
"""
from __future__ import annotations

import types

from pxr import Sdf

from usd_chunks import chunk_stage


def _graphs(target, layout):
    """{name: graph layer} sublayered on target in the given order, each holding its leaves."""
    graphs, leaves = {}, {}
    for name, members in layout:
        g = Sdf.Layer.CreateAnonymous(name)
        for member in members:
            leaves[member] = Sdf.Layer.CreateAnonymous(member)
            g.subLayerPaths.append(leaves[member].identifier)
        target.subLayerPaths.append(g.identifier)
        graphs[name] = g
    return graphs, leaves


def _manager(target, graphs):
    return types.SimpleNamespace(
        graph_layers={(name, target.identifier): g for name, g in graphs.items()},
        _graph_layer_target={(name, target.identifier): target for name in graphs},
    )


def _order(target, graphs):
    by_ident = {g.identifier: name for name, g in graphs.items()}
    return [by_ident.get(i, i) for i in target.subLayerPaths]


def test_the_graph_layer_holding_the_strongest_chunk_goes_first():
    session = Sdf.Layer.CreateAnonymous("session")
    graphs, leaves = _graphs(session, [
        ("amd", ["bind", "reference"]),       # made last, so it sat on top
        ("tab", ["add_mesh", "room"]),
    ])
    rank = {
        leaves["add_mesh"].identifier: (9, 0),  # next to the view
        leaves["room"].identifier: (1, 0),
        leaves["bind"].identifier: (5, 1),
        leaves["reference"].identifier: (5, 1),
    }

    chunk_stage._reorder_graph_layers_in_targets(_manager(session, graphs), rank)

    assert _order(session, graphs) == ["tab", "amd"]


def test_only_graph_layers_with_a_ranked_chunk_move():
    """Anything else on the target keeps its slot -- a user's own sublayer, or a graph
    layer this pass knows nothing about (another context's)."""
    session = Sdf.Layer.CreateAnonymous("session")
    graphs, leaves = _graphs(session, [
        ("weak", ["w"]),
        ("other_context", ["o"]),
        ("strong", ["s"]),
    ])
    user = Sdf.Layer.CreateAnonymous("user")
    session.subLayerPaths.insert(1, user.identifier)
    rank = {leaves["w"].identifier: (0, 0), leaves["s"].identifier: (3, 0)}

    chunk_stage._reorder_graph_layers_in_targets(_manager(session, graphs), rank)

    assert _order(session, graphs) == ["strong", user.identifier, "other_context", "weak"]


def test_nothing_ranked_changes_nothing():
    session = Sdf.Layer.CreateAnonymous("session")
    graphs, _leaves = _graphs(session, [("a", ["x"]), ("b", ["y"])])
    before = list(session.subLayerPaths)

    chunk_stage._reorder_graph_layers_in_targets(_manager(session, graphs), {})

    assert list(session.subLayerPaths) == before
