"""A ChunkGroup file referenced into a graph that already has chunks keeps its lofted values.

The inner param of a lofted one is connected up to the group's interface attr, and its value
is the group's. Loaded into a graph with chunks in it already, it was left at its module
default: the referenced Scatter On Terrain placed at Density 0, and a style's Scatter at
Spacing 10 placed nothing.
"""
from __future__ import annotations

import os

import pytest

from tests.test_copy_paste import _make_nodes, _pump, graph  # noqa: F401 -- graph is a fixture
from usd_chunks import chunk_registry

ASSET = os.path.join(os.path.dirname(__file__), "..", "examples", "groups", "scatter_on_terrain.usda")


def _reference(graph, path):  # noqa: F811
    qapp, reg, scene, _docker = graph
    entry = chunk_registry.MenuEntry(reg.get_chunk_module("reference"), ("Reference",),
                                     presets={"file_path": os.path.abspath(path)})
    for chunk in entry.create():
        scene.add_chunk(chunk)
    _pump(qapp, 2.0)
    return next(c for c in scene.get_chunks() if c.name == "Scatter")


@pytest.mark.parametrize("first", [False, True], ids=["empty graph", "after a chunk"])
def test_lofted_values_come_from_the_file(graph, first):  # noqa: F811
    if first:
        _make_nodes(graph, module="mesh")
        _pump(graph[0], 0.6)

    scatter = _reference(graph, ASSET)

    assert scatter.get_param("density").value == pytest.approx(1.0), "Scatter_density in the file, not 0"
    assert scatter.get_param("max_slope").value == pytest.approx(90.0)
