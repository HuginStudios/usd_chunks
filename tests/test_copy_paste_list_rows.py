"""Copy/paste keeps a chunk's list-group rows.

Reported for Sublayer: cut one into a chunkgroup and it arrives with an empty list and
no path. It is not specific to Sublayer -- every chunk built on a list group loses its
rows the same way.

The values were captured correctly. They had nowhere to land: a list group's rows are
created at runtime, a freshly pasted chunk starts with none, and the snapshot is a
positional zip over the params that exist. Zero params on the new chunk means zero
values applied.
"""
from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

PATH_A = "C:/scenes/kitchen.usda"
PATH_B = "C:/scenes/props.usda"


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    d = ChunkWidgetDocker()
    d.resize(500, 600)
    d.show()
    qapp.processEvents()
    # The graph view is fetched per test, after a chunk exists: an empty panel has no
    # context yet and therefore no graph to fetch.
    return qapp, reg, scene, d


def _mk(reg, scene, key):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _sublayer_with(reg, scene, qapp, *paths):
    sub = _mk(reg, scene, "sublayer")
    qapp.processEvents()
    imports = sub.get_param("imports")
    imports.size = len(paths)
    for i, path in enumerate(paths):
        imports.get_indexed_param("sublayer_path", i).value = path
    qapp.processEvents()
    return sub


def _paths_of(chunk):
    imports = chunk.get_param("imports")
    return [
        imports.get_indexed_param("sublayer_path", i).value
        for i in range(imports.size)
    ]


def _graph_view(d, qapp):
    qapp.processEvents()
    gv = d.tab_widget.get_current_graph_view()
    assert gv is not None, "no graph view; a chunk must exist first"
    return gv


def _copy_paste(d, scene, qapp, chunk):
    """Through the real clipboard, not the snapshot helpers."""
    gv = _graph_view(d, qapp)
    scene.set_selected_chunks([chunk])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(400, 400))
    qapp.processEvents()
    assert pasted, "nothing was pasted"
    return pasted[0]


def test_a_pasted_sublayer_keeps_its_path(graph):
    qapp, reg, scene, d = graph
    sub = _sublayer_with(reg, scene, qapp, PATH_A)
    assert _paths_of(sub) == [PATH_A], "test setup failed to set the path"

    copy = _copy_paste(d, scene, qapp, sub)

    assert _paths_of(copy) == [PATH_A], (
        "the pasted chunk arrived with an empty list -- its rows were never recreated"
    )


def test_every_row_survives_not_just_the_first(graph):
    qapp, reg, scene, d = graph
    sub = _sublayer_with(reg, scene, qapp, PATH_A, PATH_B)

    copy = _copy_paste(d, scene, qapp, sub)

    assert _paths_of(copy) == [PATH_A, PATH_B]


def test_an_empty_list_stays_empty(graph):
    """Restoring sizes must not invent a row the original never had."""
    qapp, reg, scene, d = graph
    sub = _mk(reg, scene, "sublayer")
    qapp.processEvents()
    assert sub.get_param("imports").size == 0

    copy = _copy_paste(d, scene, qapp, sub)

    assert copy.get_param("imports").size == 0


def test_a_duplicate_keeps_its_rows_too(graph):
    """Duplicate shares the snapshot path with copy/paste; both had the bug."""
    qapp, reg, scene, d = graph
    sub = _sublayer_with(reg, scene, qapp, PATH_A)
    gv = _graph_view(d, qapp)
    scene.set_selected_chunks([sub])

    gv._duplicate_selected()
    qapp.processEvents()

    subs = [c for c in scene.get_chunks() if c.get_param("imports") is not None]
    assert len(subs) == 2, f"expected the original and a duplicate, got {len(subs)}"
    for c in subs:
        assert _paths_of(c) == [PATH_A]
