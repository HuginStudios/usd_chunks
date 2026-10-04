"""Every connectable param type, wired out of one chunk and into another.

test_typed_param_wire.py and test_list_row_connections.py cover a float Slider and list rows
in depth; this runs the same round trip over every built-in param that gets a port, so a
type that stops carrying values through a wire shows up by name:

- the wire authors a USD connection to the source's inputs: attribute,
- the destination's attribute is typed like the param, not as text,
- the destination reads the source's value and follows it when it moves,
- the expression is saved on the prim and reloads onto a fresh chunk,
- cutting the wire restores the destination's default.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

import pytest
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_stage, chunk_utils
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


@dataclass
class _Case:
    id: str
    make: Callable[[str], Any]
    port_type: str
    first: Any
    second: Any


def _same(a, b):
    if isinstance(a, (tuple, list, Gf.Vec3f, Gf.Vec3d)) or isinstance(b, (tuple, list)):
        return list(a) == pytest.approx(list(b))
    if isinstance(a, float) or isinstance(b, float):
        return a == pytest.approx(b)
    return a == b


CASES = [
    _Case("float_slider", lambda n: chunk.Slider(n, default_value=0.5),
          chunk_graph.PORT_FLOAT, 2.0, 3.5),
    _Case("int_slider", lambda n: chunk.Slider(n, default_value=1, is_int=True),
          chunk_graph.PORT_INT, 4, 7),
    _Case("checkbox", lambda n: chunk.Checkbox(n, default_value=False),
          chunk_graph.PORT_BOOL, True, False),
    _Case("menu", lambda n: chunk.Menu(n, menu_items=["a", "b", "c"], default_value="a"),
          chunk_graph.PORT_STRING, "b", "c"),
    _Case("line_edit", lambda n: chunk.LineEdit(n, default_value="x"),
          chunk_graph.PORT_STRING, "hello", "world"),
    _Case("color", lambda n: chunk.ColorPicker(n, default_value=(0.0, 0.0, 0.0)),
          chunk_graph.PORT_COLOR, (1.0, 0.5, 0.25), (0.1, 0.2, 0.3)),
    _Case("vector", lambda n: chunk.Vector(n, param=chunk.Slider(), length=3),
          chunk_graph.PORT_VECTOR, (1.0, 2.0, 3.0), (4.0, 5.0, 6.0)),
]


class _Wire:
    """`src.p` and `dst.p`, both made by one case, with ports for each end."""

    def __init__(self, case: _Case):
        self.case = case
        self.stage = Usd.Stage.CreateInMemory()
        self.scene = _Scene(self.stage)
        chunk_registry.ChunkRegistry().initialize(self.scene, "pytest")
        chunk_registry.ChunkLayerManager().initialize(self.stage)
        chunk_stage.ChunkStageSync().initialize(self.stage, self.scene)

        self.src, self.dst = chunk.Chunk("src"), chunk.Chunk("dst")
        self.chunks = {}
        for c in (self.src, self.dst):
            attach_stub_module(c)
            c.add_param(case.make("p"))
            self.scene.add_chunk(c)
            self.chunks[self.path(c)] = c
        self.src_p, self.dst_p = self.src.get_param("p"), self.dst.get_param("p")
        self.src_p.value = case.first
        self.default = self.dst_p.default_value

    def path(self, c):
        return chunk_stage.get_chunk_prim_path(c)

    def port(self, c, is_input):
        path = self.path(c)
        prefix = "inputs" if is_input else "outputs"
        return chunk_graph.PortRef(path, f"{path}.{prefix}:p", self.case.port_type, is_input,
                                   param=c.get_param("p"))

    @property
    def in_port(self):
        return self.port(self.dst, True)

    def connect(self):
        return chunk_graph.connect(self.stage, self.port(self.src, False), self.in_port, self.chunks)

    def disconnect(self):
        prims = {p: self.stage.GetPrimAtPath(p) for p in self.chunks}
        return chunk_graph.disconnect(self.stage, self.in_port, prims, self.chunks, heal=False)

    def dst_attr(self):
        return self.stage.GetAttributeAtPath(Sdf.Path(self.in_port.attr_path))


@pytest.fixture(params=CASES, ids=[c.id for c in CASES])
def wire(request):
    return _Wire(request.param)


def test_every_case_gets_the_port_type_it_claims(wire):
    assert chunk_graph.param_port_type(wire.dst_p) == wire.case.port_type
    ports = chunk_graph.iter_ports(wire.stage.GetPrimAtPath(wire.path(wire.dst)), wire.dst)
    names = {(p.attr_path.split(".", 1)[1], p.port_type) for p in ports}
    assert ("inputs:p", wire.case.port_type) in names
    assert ("outputs:p", wire.case.port_type) in names


def test_the_wire_is_a_usd_connection_to_the_sources_input(wire):
    change = wire.connect()

    assert change is not None and not change.expression_error
    assert [str(c) for c in wire.dst_attr().GetConnections()] == [f"{wire.path(wire.src)}.inputs:p"]


def test_the_wired_attribute_is_typed_like_the_param(wire):
    wire.connect()

    expected = chunk_stage.param_sdf_type(wire.dst_p)
    assert expected, "no attribute type for the param: the wire falls back to a String attr"
    assert wire.dst_attr().GetTypeName() == expected


def test_a_wired_graph_read_without_chunks_keeps_the_port_type(wire):
    """A frontend reading a saved file types ports from the attributes alone."""
    wire.connect()
    prim = wire.stage.GetPrimAtPath(wire.path(wire.dst))

    ports = {p.attr_path: p.port_type for p in chunk_graph.iter_ports(prim)}

    assert ports[wire.in_port.attr_path] == wire.case.port_type


def test_the_destination_reads_the_source(wire):
    wire.connect()

    assert wire.dst_p.raw_value == "@src.p@"
    assert _same(wire.dst_p.value, wire.case.first)


def test_the_destination_follows_the_source(wire):
    wire.connect()

    wire.src_p.value = wire.case.second

    assert _same(wire.dst_p.value, wire.case.second)


def test_the_wire_shows_up_as_an_edge(wire):
    wire.connect()
    prims = {p: wire.stage.GetPrimAtPath(p) for p in wire.chunks}

    edges = [e for e in chunk_graph.build_edges(prims, wire.chunks) if not e.is_exec]

    assert [(e.src_attr_path, e.dst_attr_path) for e in edges] == [
        (f"{wire.path(wire.src)}.outputs:p", f"{wire.path(wire.dst)}.inputs:p")
    ]


def test_the_wire_is_saved_and_reloaded(wire):
    wire.connect()
    prim = wire.stage.GetPrimAtPath(wire.path(wire.dst))
    assert chunk_utils.read_expression_text(prim.GetAttribute("inputs:p")) == "@src.p@"

    reloaded = chunk.Chunk("dst")
    reloaded.add_param(wire.case.make("p"))
    chunk_stage._load_wire_inputs(prim, reloaded)

    assert reloaded.get_param("p").raw_value == "@src.p@"


def test_cutting_the_wire_restores_the_default(wire):
    wire.connect()

    wire.disconnect()

    assert not wire.dst_attr().HasAuthoredConnections()
    assert _same(wire.dst_p.value, wire.default)
    wire.src_p.value = wire.case.second
    assert _same(wire.dst_p.value, wire.default), "the cut wire still carries the source"


def test_a_mismatched_type_cannot_be_wired(wire):
    other = next(c for c in CASES if c.port_type != wire.case.port_type)
    src = wire.port(wire.src, False)
    dst = chunk_graph.PortRef(wire.in_port.node_path, wire.in_port.attr_path, other.port_type, True)

    assert chunk_graph.connect(wire.stage, src, dst, wire.chunks) is None
    attr = wire.dst_attr()
    assert not (attr and attr.HasAuthoredConnections())


@pytest.mark.parametrize("make, port_type", [
    (lambda: chunk.TextEdit("t"), chunk_graph.PORT_STRING),
    (lambda: chunk.FileBrowser("f"), chunk_graph.PORT_STRING),
])
def test_text_params_get_string_ports(make, port_type):
    assert chunk_graph.param_port_type(make()) == port_type
