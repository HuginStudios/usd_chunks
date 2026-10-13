"""A node rebuilds its ports when its body changes shape, not on every state change.

Each add rebuilt ports three times: twice on the new node (straight away, so paste can wire
to them, and again once its body laid out) and once on the node it took the selection from --
every state change re-applies the collapsed state, and an expanded node's _set_collapsed
started a rebuild whether or not it had just expanded.
"""
from __future__ import annotations

import time

import pytest
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt import nodegraph
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.graph import engine as chunk_graph


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def graph(qapp, monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    rebuilds: list[str] = []
    original = nodegraph.ChunkNodeItem._rebuild_typed_ports

    def counted(self):
        rebuilds.append(str(self.prim.GetPath()))
        return original(self)

    monkeypatch.setattr(nodegraph.ChunkNodeItem, "_rebuild_typed_ports", counted)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    yield qapp, reg, scene, docker, rebuilds
    scene.clear_chunk_selection()
    chunk_graph.node_clipboard.clear()
    chunk_graph.edge_clipboard.clear()


def _settle(qapp, seconds=0.5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()


def _add(graph, module="transform"):
    qapp, reg, scene, docker, _ = graph
    mod = reg.get_chunk_module(module)
    made = mod.create() if mod.loaded else mod.load()
    for c in made:
        scene.add_chunk(c)
    path = get_chunk_prim_path(made[0])
    end = time.monotonic() + 10.0
    while time.monotonic() < end:
        view = docker.tab_widget.get_current_graph_view()
        node = view.scene()._nodes.get(path) if view is not None else None
        if node is not None and node._proxy is not None:
            break
        qapp.processEvents()
    _settle(qapp)
    return made[0], path


def test_adding_a_node_leaves_the_previous_ones_alone(graph) -> None:
    """The new node takes the selection; the one that loses it must not rebuild."""
    rebuilds = graph[4]
    _, first = _add(graph)
    rebuilds.clear()
    _add(graph)
    assert first not in rebuilds, rebuilds


def test_selection_does_not_rebuild_ports(graph) -> None:
    qapp, rebuilds = graph[0], graph[4]
    first, _ = _add(graph)
    second, _ = _add(graph)
    rebuilds.clear()

    for c, other in ((first, second), (second, first)):
        c.set_state(ChunkFlags.State.SELECTED)
        other.unset_state(ChunkFlags.State.SELECTED)
        _settle(qapp, 0.3)

    assert rebuilds == []


def test_collapsing_still_rebuilds_ports(graph) -> None:
    qapp, rebuilds = graph[0], graph[4]
    c, path = _add(graph)
    rebuilds.clear()
    c.set_state(ChunkFlags.State.COLLAPSED)
    _settle(qapp, 0.3)
    assert path in rebuilds
