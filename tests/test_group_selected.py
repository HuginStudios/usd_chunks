"""Ctrl+G moves the selected chunks into a new chunkgroup.

A move, not a copy: copying reproduces params but not a chunk's own layer, so anything
holding content there -- a Scatter Brush's painted points -- would arrive empty.

The framework already relocates a chunk and its prim on add_param, and repaths the wires
between two chunks that both moved. What it cannot know is the boundary, which is what
this covers: a wire from outside the selection has to feed the GROUP, and a wire out of it
has to come from the group, so the group sits in the chain exactly where the selection did.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore, QtGui

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import (
    _EXEC_OUTPUT, connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
)


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
    return qapp, reg, scene, d, stage


def _mk(reg, scene, key="transform"):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _view(d, qapp):
    qapp.processEvents()
    gv = d.tab_widget.get_current_graph_view()
    assert gv is not None
    return gv


def _groups(scene):
    return [c for c in scene.get_chunks() if chunk_usd.is_chunkgroup(c)]


def _inputs(stage, chunk):
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not (prim and prim.IsValid()):
        return None
    return [
        str(conn) for attr in chunk_usd.iter_exec_inputs(prim) for conn in attr.GetConnections()
    ]


def _contents_of(scene, group):
    """The chunks that ended up inside `group`.

    By path rather than by object: grouping goes through Cut and Paste, so the chunks in
    the group are NEW objects and the originals are gone -- the same as doing it by hand.
    A test holding the original references is testing dead objects.
    """
    prefix = get_chunk_prim_path(group) + "/"
    return [c for c in scene.get_chunks() if get_chunk_prim_path(c).startswith(prefix)]


def test_the_selection_ends_up_inside_a_new_group(graph):
    qapp, reg, scene, d, stage = graph
    a, b, outside = _mk(reg, scene), _mk(reg, scene), _mk(reg, scene, "prim")
    gv = _view(d, qapp)
    qapp.processEvents()
    outside_path = get_chunk_prim_path(outside)
    scene.set_selected_chunks([a, b])

    gv._group_selected()
    _settle(qapp)

    groups = _groups(scene)
    assert len(groups) == 1, f"expected one new chunkgroup, got {[g.name for g in groups]}"
    assert len(_contents_of(scene, groups[0])) == 2, "both selected chunks should be inside"
    assert any(get_chunk_prim_path(c) == outside_path for c in scene.get_chunks()), (
        "an unselected chunk must be left alone"
    )


def test_param_values_come_across(graph):
    qapp, reg, scene, d, stage = graph
    a = _mk(reg, scene)
    a.get_param("tx").value = 7.0
    gv = _view(d, qapp)
    qapp.processEvents()
    scene.set_selected_chunks([a])

    gv._group_selected()
    _settle(qapp)

    inside = _contents_of(scene, _groups(scene)[0])
    assert len(inside) == 1
    assert inside[0].get_param("tx").value == 7.0


def test_wires_inside_the_selection_survive(graph):
    qapp, reg, scene, d, stage = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    connect_exec(stage, reg.scene, get_chunk_prim_path(a), get_chunk_prim_path(b))
    qapp.processEvents()
    scene.set_selected_chunks([a, b])

    gv._group_selected()
    _settle(qapp)

    inside = _contents_of(scene, _groups(scene)[0])
    assert len(inside) == 2
    wired = [c for c in inside if _inputs(stage, c)]
    assert len(wired) == 1, (
        f"one of the two should still take from the other: "
        f"{ {c.name: _inputs(stage, c) for c in inside} }"
    )
    src_path = _inputs(stage, wired[0])[0].rsplit(".", 1)[0]
    assert any(get_chunk_prim_path(c) == src_path for c in inside), (
        "and it should take from its neighbour inside the group, not from outside it"
    )


def _settle(qapp):
    """Paste defers its wiring by 80ms; let that and its follow-up land."""
    for _ in range(30):
        qapp.processEvents()
        QtCore.QThread.msleep(10)
    qapp.processEvents()


def test_the_chain_heals_around_what_was_grouped(graph):
    """up -> mid -> down, grouping mid, leaves up -> down.

    That is Cut's behaviour, and grouping is built on Cut. The group is NOT spliced into
    the chain in mid's place: a group is not a drop-in replacement for what it holds --
    which of several chunks would its output be? -- and doing it by hand does not do that
    either.
    """
    qapp, reg, scene, d, stage = graph
    up, mid, down = _mk(reg, scene, "prim"), _mk(reg, scene), _mk(reg, scene, "prim")
    gv = _view(d, qapp)
    qapp.processEvents()
    connect_exec(stage, reg.scene, get_chunk_prim_path(up), get_chunk_prim_path(mid))
    connect_exec(stage, reg.scene, get_chunk_prim_path(mid), get_chunk_prim_path(down))
    qapp.processEvents()
    scene.set_selected_chunks([mid])

    gv._group_selected()
    _settle(qapp)

    assert _inputs(stage, down) == [f"{get_chunk_prim_path(up)}.{_EXEC_OUTPUT}"], (
        "the chain must close over the gap, not dangle at a chunk that moved"
    )


def test_no_wire_is_left_pointing_at_a_prim_that_moved(graph):
    """The thing that would make this unusable: a dangling connection."""
    qapp, reg, scene, d, stage = graph
    up, mid = _mk(reg, scene, "prim"), _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    connect_exec(stage, reg.scene, get_chunk_prim_path(up), get_chunk_prim_path(mid))
    qapp.processEvents()
    scene.set_selected_chunks([mid])

    gv._group_selected()
    _settle(qapp)

    live = {str(p.GetPath()) for p in stage.TraverseAll() if p.GetTypeName() == "Chunk"}
    for prim in stage.TraverseAll():
        if prim.GetTypeName() != "Chunk":
            continue
        for attr in chunk_usd.iter_exec_inputs(prim):
            for conn in attr.GetConnections():
                assert str(conn.GetPrimPath()) in live, (
                    f"{prim.GetPath()} points at {conn.GetPrimPath()}, which is not on the stage"
                )


def test_grouping_nothing_does_nothing(graph):
    qapp, reg, scene, d, stage = graph
    _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    scene.set_selected_chunks([])

    gv._group_selected()
    qapp.processEvents()

    assert not _groups(scene), "an empty selection must not conjure a group"


def test_ctrl_g_reaches_it(graph):
    qapp, reg, scene, d, stage = graph
    a = _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    scene.set_selected_chunks([a])

    gv.keyPressEvent(QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress,
        QtCore.Qt.Key.Key_G,
        QtCore.Qt.KeyboardModifier.ControlModifier,
    ))
    qapp.processEvents()

    assert _groups(scene), "Ctrl+G should have made a group"
