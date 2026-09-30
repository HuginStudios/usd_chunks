"""A list-group row wired to another chunk's param follows it.

Reported: wire a float into the example chunk's listSlider. The value changed in the
terminal, but the slider in the UI did not move. The wire landed on the template (rows share
its name), and a row never resolved or followed an expression.
"""
from __future__ import annotations

import pytest
from pxr import Usd

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
    """src.p wired into row 1 of dst's two-row list. Row 0 and the template stay unwired."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)

    src, dst = chunk.Chunk("src"), chunk.Chunk("dst")
    for c in (src, dst):
        attach_stub_module(c)
    src.add_param(chunk.Slider("p", default_value=1.0))
    rows = chunk.ListGroup("rows")
    rows.add_param(chunk.Slider("item", default_value=0.0))
    dst.add_param(rows)
    scene.add_chunk(src)
    scene.add_chunk(dst)
    rows.size = 2

    src_path, dst_path = chunk_stage.get_chunk_prim_path(src), chunk_stage.get_chunk_prim_path(dst)
    row = rows.get_indexed_param("item", 1)
    out_port = chunk_graph.PortRef(src_path, f"{src_path}.outputs:p", chunk_graph.PORT_FLOAT, False)
    in_port = chunk_graph.PortRef(dst_path, f"{dst_path}.inputs:item", chunk_graph.PORT_FLOAT, True, param=row)
    chunks = {src_path: src, dst_path: dst}
    chunk_graph.connect(stage, out_port, in_port, chunks)
    return stage, src, dst, rows, in_port, chunks


def test_the_wire_drives_only_the_row_it_was_dropped_on(wired):
    stage, src, dst, rows, in_port, chunks = wired
    row = rows.get_indexed_param("item", 1)

    src.get_param("p").value = 7.5
    assert row.value == 7.5
    assert rows.get_indexed_param("item", 0).value == 0.0
    assert rows.get_param("item").raw_value == 0.0, "the template took the wire"
    # Rows share one name, so no attribute of that name may carry the wire.
    assert not stage.GetPrimAtPath(in_port.node_path).GetAttribute("inputs:item")

    chunk_graph.disconnect(stage, in_port, {}, chunks)
    src.get_param("p").value = 9.0
    assert row.value == 0.0


def test_a_wired_row_is_saved_and_reloaded(wired):
    stage, src, dst, rows, in_port, chunks = wired
    src.get_param("p").value = 5.0
    prim = stage.GetPrimAtPath(in_port.node_path)
    attr = prim.GetAttribute("inputs:rows:item")

    # The array keeps its type and holds what the rows read; the text rides in customData.
    assert list(attr.Get()) == [0.0, 5.0]
    assert list(attr.GetCustomDataByKey(chunk_utils.EXPR_TEXT_KEY)) == ["", "@src.p@"]

    rows.get_indexed_param("item", 0).value = 2.0
    rows.get_indexed_param("item", 1).value = 3.0
    assert not attr.HasCustomDataKey(chunk_utils.EXPR_TEXT_KEY)

    rows.get_indexed_param("item", 1).value = "@src.p@"
    reloaded = chunk.Chunk("dst")
    reloaded_rows = chunk.ListGroup("rows")
    reloaded_rows.add_param(chunk.Slider("item", default_value=0.0))
    reloaded.add_param(reloaded_rows)
    chunk_stage._load_wire_inputs(prim, reloaded)
    assert reloaded_rows.get_indexed_param("item", 0).value == 2.0
    assert reloaded_rows.get_indexed_param("item", 1).raw_value == "@src.p@"


def test_a_row_restored_before_its_source_follows_it_once_loaded(wired):
    """On load a row can be restored before the chunk it names exists; the retry pass picks it up."""
    stage, src, dst, rows, in_port, chunks = wired
    row = rows.get_indexed_param("item", 0)
    row.value = "@late.q@"  # no chunk "late" yet, so nothing to listen to

    late = chunk.Chunk("late")
    attach_stub_module(late)
    late.add_param(chunk.Slider("q", default_value=0.0))
    chunk_registry.ChunkRegistry().scene.add_chunk(late)
    chunk_stage._resolve_pending_expressions([(None, dst)], warn=False)

    late.get_param("q").value = 6.0
    # A read resolves fresh either way; only a row that listens pushes the change to USD.
    assert stage.GetPrimAtPath(in_port.node_path).GetAttribute("inputs:rows:item").Get()[0] == 6.0

