"""A wire into a number param leaves it a number attribute.

Reported: after a wire into a Swedish House's Width, typing a width raised "Type mismatch
for <...inputs:width>: expected 'string', got 'double'". Wiring made the param's attribute a
String, to hold the expression, and it stayed one after the wire was gone.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

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


@pytest.fixture()
def wired():
    """src.p wired into dst's footprint_grp.width, both Sliders.

    In a group, as House has it: a port is named after the param alone (inputs:width), and
    the param's own storage attribute after its group too (inputs:footprint_grp:width), so
    the attribute the wire makes is not one that binding the param would ever retype.
    """
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)

    src, dst = chunk.Chunk("src"), chunk.Chunk("dst")
    for c in (src, dst):
        attach_stub_module(c)
    src.add_param(chunk.Slider("p", default_value=2.0))
    width = chunk.Slider("width", default_value=5.9)
    footprint = chunk.Group("footprint_grp", "Footprint")
    footprint.add_param(width)
    dst.add_param(footprint)
    scene.add_chunk(src)
    scene.add_chunk(dst)

    src_path, dst_path = chunk_stage.get_chunk_prim_path(src), chunk_stage.get_chunk_prim_path(dst)
    out_port = chunk_graph.PortRef(src_path, f"{src_path}.outputs:p", chunk_graph.PORT_FLOAT, False)
    in_port = chunk_graph.PortRef(dst_path, f"{dst_path}.inputs:width", chunk_graph.PORT_FLOAT, True, param=width)
    chunks = {src_path: src, dst_path: dst}
    chunk_graph.connect(stage, out_port, in_port, chunks)
    return stage, width, in_port, chunks


def _attr(stage, port):
    return stage.GetAttributeAtPath(Sdf.Path(port.attr_path))


def test_a_wire_makes_a_number_attribute(wired):
    stage, width, port, _ = wired

    assert _attr(stage, port).GetTypeName() == Sdf.ValueTypeNames.Float
    assert width.value == 2.0


def _bound(stage, param):
    return stage.GetAttributeAtPath(Sdf.Path(param._param_attr_path))


def test_a_number_typed_after_the_wire_is_gone_is_kept(wired):
    stage, width, port, chunks = wired
    prims = {path: stage.GetPrimAtPath(path) for path in chunks}
    chunk_graph.disconnect(stage, port, prims, chunks, heal=False)

    width.value = 7.5

    assert _bound(stage, width).Get() == pytest.approx(7.5)


def test_a_string_attribute_from_an_older_file_is_retyped_when_a_number_lands(wired):
    """The reported file: the param bound to a String attr the wire made before this fix."""
    stage, width, port, chunks = wired
    prims = {path: stage.GetPrimAtPath(path) for path in chunks}
    chunk_graph.disconnect(stage, port, prims, chunks, heal=False)
    chunk_utils.retype_attr(_bound(stage, width), Sdf.ValueTypeNames.String)

    width.value = 8.25

    attr = _bound(stage, width)
    assert attr.GetTypeName() == Sdf.ValueTypeNames.Float
    assert attr.Get() == pytest.approx(8.25)
