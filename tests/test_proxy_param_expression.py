"""A list-group row wired to another chunk's param follows it.

Reported: connect a float to the example chunk's listSlider. The value updated in the
terminal, but the slider in the UI stayed put. A row is a ProxyParam: it stored the
expression text and never resolved it, and never listened to the param it names.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk, chunk_notice, chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import ChunkStageSync

from chunk_stubs import attach_stub_module
from test_copy_paste_list_rows import _graph_view, graph  # noqa: F401  (graph is a fixture)
from test_copy_paste_list_rows import _mk as _mk_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    sc = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sc, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    ChunkStageSync().initialize(stage, sc)
    yield sc


def _mk(name):
    c = chunk.Chunk(name)
    attach_stub_module(c)
    return c


def _wired(scene):
    src, dst = _mk("src"), _mk("dst")
    src.add_param(chunk.Slider("p", default_value=0.0))
    group = chunk.ListGroup("rows")
    group.add_param(chunk.Slider("item"))
    dst.add_param(group)
    scene.add_chunk(src)
    scene.add_chunk(dst)
    group.size = 1
    row = group.get_indexed_param(group.get_param("item"), 0)
    return src, row


def test_row_resolves_an_expression(scene):
    src, row = _wired(scene)
    src.get_param("p").value = 4.0
    row.value = "@src.p@"
    assert row.value == 4.0


def test_row_follows_its_source_and_tells_the_widget(scene):
    src, row = _wired(scene)
    row.value = "@src.p@"
    seen = []
    row.add_callback(
        lambda notice, sender: seen.append(row.value),
        notice_alias=chunk_notice.ChunkNoticeAlias.ATTRIBUTE_CHANGED,
    )
    src.get_param("p").value = 7.5
    assert row.value == 7.5
    assert 7.5 in seen


def test_row_stops_following_when_set_to_a_plain_value(scene):
    src, row = _wired(scene)
    row.value = "@src.p@"
    row.value = 2.0
    src.get_param("p").value = 9.0
    assert row.value == 2.0


# ---------------------------------------------------------------------------
# Through the node graph
# ---------------------------------------------------------------------------


def _ports(node):
    return {p.attr_path.split(".", 1)[1]: p for p in node._typed_ports}


def _wire_mesh_width_to_row(graph):
    qapp, reg, gscene, d = graph
    example = _mk_module(reg, gscene, "example")
    mesh = _mk_module(reg, gscene, "mesh")
    mesh.get_param("mesh_type").value = "cube"
    qapp.processEvents()
    gv = _graph_view(d, qapp)
    nodes = {n.chunk.name: n for n in gv.scene()._nodes.values()}
    gv._author_connection(_ports(nodes["Mesh"])["outputs:width"], _ports(nodes["HelloWorld"])["inputs:listSlider"])
    qapp.processEvents()
    return example, mesh


def test_a_wire_to_a_row_drives_the_row_not_the_template(graph):
    example, mesh = _wire_mesh_width_to_row(graph)
    row = example.get_param("listGroup").get_params()[0]

    assert row.raw_value == "@Mesh.width@"
    assert example.get_param("listSlider").raw_value == 0.0, "the template took the wire"

    mesh.get_param("width").value = 5.0
    assert row.value == 5.0


def test_a_wired_row_is_saved_as_its_expression(graph):
    example, mesh = _wire_mesh_width_to_row(graph)
    mesh.get_param("width").value = 5.0

    stage = chunk_registry.ChunkRegistry().scene.get_stage()
    prim = next(p for p in stage.Traverse() if p.GetName() == "HelloWorld")
    assert list(prim.GetAttribute("inputs:listGroup:listSlider").Get()) == ["@Mesh.width@"]

    # Back to a plain number: the array changes type rather than failing to write.
    example.get_param("listGroup").get_params()[0].value = 2.0
    assert list(prim.GetAttribute("inputs:listGroup:listSlider").Get()) == [2.0]
