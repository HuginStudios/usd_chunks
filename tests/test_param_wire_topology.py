"""Param wires in combination: fan-out, chains, rewiring, grouped params, renames, slots.

test_param_wire_matrix.py checks one wire per param type; this checks how wires behave
alongside each other and as the nodes at either end change.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
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
    """Three chunks a, b, c, each with a float Slider `p` defaulting to 0."""

    def __init__(self):
        self.stage = Usd.Stage.CreateInMemory()
        self.scene = _Scene(self.stage)
        chunk_registry.ChunkRegistry().initialize(self.scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(self.stage)
        chunk_stage.ChunkStageSync().initialize(self.stage, self.scene)
        self.chunks = {}
        self.a, self.b, self.c = (self.add(name) for name in ("a", "b", "c"))

    def add(self, name, *params):
        c = chunk.Chunk(name)
        attach_stub_module(c)
        c.add_param(chunk.Slider("p", default_value=0.0))
        for param in params:
            c.add_param(param)
        self.scene.add_chunk(c)
        self.chunks[self.path(c)] = c
        return c

    def path(self, c):
        return chunk_stage.get_chunk_prim_path(c)

    def port(self, c, is_input, name="p", param=None):
        path = self.path(c)
        prefix = "inputs" if is_input else "outputs"
        return chunk_graph.PortRef(path, f"{path}.{prefix}:{name}", chunk_graph.PORT_FLOAT, is_input,
                                   param=param if param is not None else c.get_param(name))

    def connect(self, src, dst, dst_name="p", dst_param=None):
        return chunk_graph.connect(self.stage, self.port(src, False),
                                   self.port(dst, True, dst_name, dst_param), self.chunks)

    def prims(self):
        return {p: self.stage.GetPrimAtPath(p) for p in self.chunks}

    def disconnect(self, dst, name="p", param=None):
        return chunk_graph.disconnect(self.stage, self.port(dst, True, name, param), self.prims(),
                                      self.chunks, heal=False)

    def param_edges(self):
        prims = {str(prim.GetPath()): prim for prim in chunk_graph.resolve_graph_nodes(
            self.stage, str(self.stage.GetPrimAtPath(self.path(self.a)).GetPath().GetParentPath()))}
        return sorted((e.src_attr_path, e.dst_attr_path)
                      for e in chunk_graph.build_edges(prims, self.chunks) if not e.is_exec)


@pytest.fixture()
def graph():
    return _Graph()


def p(c):
    return c.get_param("p")


def test_one_output_feeds_two_inputs(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.a, graph.c)

    p(graph.a).value = 3.0

    assert p(graph.b).value == pytest.approx(3.0)
    assert p(graph.c).value == pytest.approx(3.0)
    assert len(graph.param_edges()) == 2


def test_cutting_one_fan_out_wire_leaves_the_other(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.a, graph.c)

    graph.disconnect(graph.b)
    p(graph.a).value = 5.0

    assert p(graph.b).value == pytest.approx(0.0)
    assert p(graph.c).value == pytest.approx(5.0)


def test_a_value_travels_down_a_chain(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.b, graph.c)

    p(graph.a).value = 7.0

    assert p(graph.b).value == pytest.approx(7.0)
    assert p(graph.c).value == pytest.approx(7.0), "the second hop did not hear the first"


def test_cutting_the_middle_of_a_chain_stops_it_there(graph):
    graph.connect(graph.a, graph.b)
    graph.connect(graph.b, graph.c)

    graph.disconnect(graph.b)
    p(graph.a).value = 9.0

    assert p(graph.b).value == pytest.approx(0.0)
    assert p(graph.c).value == pytest.approx(0.0)


def test_rewiring_an_input_replaces_its_source(graph):
    """An input holds one wire: connecting a new source drops the old one."""
    graph.connect(graph.a, graph.c)
    graph.connect(graph.b, graph.c)

    attr = graph.stage.GetAttributeAtPath(Sdf.Path(graph.port(graph.c, True).attr_path))
    assert [str(c) for c in attr.GetConnections()] == [f"{graph.path(graph.b)}.inputs:p"]
    assert p(graph.c).raw_value == "@b.p@"

    p(graph.a).value = 1.0
    p(graph.b).value = 2.0
    assert p(graph.c).value == pytest.approx(2.0), "the replaced source still drives the input"


def test_cutting_a_wire_into_a_grouped_param_restores_its_default(graph):
    """Ports are named after the param alone; disconnect must still find it in its group."""
    width = chunk.Slider("width", default_value=5.0)
    group = chunk.Group("dims", "Dims")
    group.add_param(width)
    d = graph.add("d", group)

    graph.connect(graph.a, d, "width", width)
    p(graph.a).value = 2.0
    assert width.value == pytest.approx(2.0)

    graph.disconnect(d, "width", width)

    assert width.value == pytest.approx(5.0)
    p(graph.a).value = 3.0
    assert width.value == pytest.approx(5.0)


def test_a_wire_survives_renaming_its_source(graph):
    graph.connect(graph.a, graph.b)

    graph.a.name = "renamed"

    assert p(graph.b).raw_value == "@renamed.p@"
    p(graph.a).value = 4.0
    assert p(graph.b).value == pytest.approx(4.0)
    src = f"{graph.path(graph.a)}.outputs:p"
    assert (src, f"{graph.path(graph.b)}.inputs:p") in graph.param_edges(), \
        "the USD connection still names the old prim path"


def test_a_wire_survives_renaming_its_destination(graph):
    graph.connect(graph.a, graph.b)

    graph.b.name = "renamed"
    graph.chunks = {graph.path(c): c for c in (graph.a, graph.b, graph.c)}

    p(graph.a).value = 6.0
    assert p(graph.b).value == pytest.approx(6.0)
    assert (f"{graph.path(graph.a)}.outputs:p", f"{graph.path(graph.b)}.inputs:p") \
        in graph.param_edges()


# -- view slots are per container ----------------------------------------------------------

def _view_prim(stage, path, slot):
    prim = stage.DefinePrim(path)
    prim.CreateAttribute(chunk_graph.CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.String).Set(
        chunk_graph.VIEW_MODULE)
    prim.CreateAttribute(chunk_graph.VIEW_SLOT_ATTR, Sdf.ValueTypeNames.Int).Set(slot)
    return prim


def test_view_slots_are_counted_per_container():
    stage = Usd.Stage.CreateInMemory()
    for slot in chunk_graph.VIEW_SLOTS:
        _view_prim(stage, f"/chunks/tabA/View{slot}", slot)

    assert chunk_graph.free_view_slot(stage, "/chunks/tabA") is None
    assert chunk_graph.free_view_slot(stage, "/chunks/tabB") == 1, \
        "another container's views used up this one's slots"


def test_the_lowest_free_slot_is_reused():
    stage = Usd.Stage.CreateInMemory()
    _view_prim(stage, "/chunks/tab/View2", 2)

    assert chunk_graph.free_view_slot(stage, "/chunks/tab") == 1
