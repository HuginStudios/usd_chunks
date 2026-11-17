"""chunk_stage.input_consumers: which chunks have a typed input wired to this one.

USD keeps a connection only on the consumer, so the answer comes from an index of the
stage. These check it stays right as wires, names and chunks change, and that it is
built once rather than on every ask -- a MaterialX Shader asks once per hop to its
Material on every cook, and walking every chunk prim each time made a cook cost the
size of the network.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_topology
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.graph import engine as chunk_graph

from chunk_stubs import attach_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _Graph:
    """Chunks a, b, c, each with a float Slider `p`."""

    def __init__(self):
        self.stage = Usd.Stage.CreateInMemory()
        self.scene = _Scene(self.stage)
        chunk_registry.ChunkRegistry().initialize(self.scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(self.stage)
        chunk_stage.ChunkStageSync().initialize(self.stage, self.scene)
        self.a, self.b, self.c = (self.add(name) for name in ("a", "b", "c"))

    def add(self, name):
        c = chunk.Chunk(name)
        attach_stub_module(c)
        c.add_param(chunk.Slider("p", default_value=0.0))
        self.scene.add_chunk(c)
        return c

    def chunks(self):
        return {chunk_stage.get_chunk_prim_path(c): c for c in self.scene.get_chunks()}

    def port(self, c, is_input):
        path = chunk_stage.get_chunk_prim_path(c)
        prefix = "inputs" if is_input else "outputs"
        return chunk_graph.PortRef(path, f"{path}.{prefix}:p", chunk_graph.PORT_FLOAT, is_input,
                                   param=c.get_param("p"))

    def connect(self, src, dst):
        assert chunk_graph.connect(self.stage, self.port(src, False), self.port(dst, True), self.chunks())

    def disconnect(self, dst):
        prims = {p: self.stage.GetPrimAtPath(p) for p in self.chunks()}
        chunk_graph.disconnect(self.stage, self.port(dst, True), prims, self.chunks(), heal=False)


@pytest.fixture()
def graph():
    return _Graph()


def consumers(c):
    """The names of the chunks wired to c, sorted: the order is the scene's, not asserted here."""
    return sorted(x.name for x in chunk_stage.input_consumers(c))


def test_lists_the_chunks_wired_to_it(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.a, graph.c)

    assert consumers(graph.a) == ["b", "c"]
    assert consumers(graph.b) == []


def test_follows_a_cut_and_a_rewire(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.a, graph.c)
    assert consumers(graph.a) == ["b", "c"]

    graph.disconnect(graph.b)
    assert consumers(graph.a) == ["c"]

    # An input holds one wire: c taking b replaces a.
    graph.connect(graph.b, graph.c)
    assert consumers(graph.a) == []
    assert consumers(graph.b) == ["c"]


def test_follows_a_rename_at_either_end(graph):
    graph.connect(graph.a, graph.b)
    assert consumers(graph.a) == [graph.b.name]

    graph.b.name = "b2"
    assert consumers(graph.a) == [graph.b.name]

    graph.a.name = "a2"
    assert consumers(graph.a) == [graph.b.name]


def test_follows_a_delete(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.a, graph.c)
    assert consumers(graph.a) == ["b", "c"]

    graph.scene.remove_chunk(graph.c.name)
    assert consumers(graph.a) == [graph.b.name]


def test_an_exec_wire_is_not_a_consumer(graph):
    graph.scene.connect(graph.a, graph.b)

    assert consumers(graph.a) == []


def test_index_is_built_once_and_kept_up_to_date(graph, monkeypatch):
    builds = []
    real = chunk_topology._index_input_consumers
    monkeypatch.setattr(chunk_topology, "_index_input_consumers", lambda stage: builds.append(1) or real(stage))
    graph.connect(graph.a, graph.b)
    assert consumers(graph.a) == [graph.b.name]

    # A param value, a new chunk (and the layer it brings) and a new wire each change the
    # stage, and none of them needs the whole stage read again.
    graph.a.get_param("p").value = 2.0
    d = graph.add("d")
    graph.connect(graph.a, d)

    assert consumers(graph.a) == ["b", "d"]
    assert len(builds) == 1
