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


def test_the_group_takes_the_selection_s_place_in_the_chain(graph):
    """up -> mid -> down, grouping mid, becomes up -> group -> down.

    The graph should still read as the same flow with a box drawn round part of it. Cut
    heals the chain closed on its way through, so this is about putting the group back
    into the gap that healing leaves.
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

    group = _groups(scene)[0]
    assert _inputs(stage, group) == [f"{get_chunk_prim_path(up)}.{_EXEC_OUTPUT}"], (
        "what fed the selection must feed the group"
    )
    assert _inputs(stage, down) == [f"{get_chunk_prim_path(group)}.{_EXEC_OUTPUT}"], (
        "and what the selection fed must be fed by the group"
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


def test_ctrl_g_reaches_it_through_the_router(graph, monkeypatch):
    """The path the app actually takes.

    ChunkGraphView.keyPressEvent needs the view to hold real native Qt focus, which
    usdview's MainWindow routinely reclaims -- so Ctrl+C/V/D/X were long ago hardened by
    routing them through the app-level _ProxyKeyRouter instead. Ctrl+G was not, and rode
    that fragile path: bound, tested, and doing nothing at all in the real app.
    """
    qapp, reg, scene, d, stage = graph
    a = _mk(reg, scene)
    gv = _view(d, qapp)

    # The fallback is gated on the cursor being over the view. Reported rather than
    # actually moved: a real setPos is at the mercy of whatever else has moved the mouse,
    # which made this pass alone and fail in a full run.
    centre = gv.mapToGlobal(QtCore.QPoint(gv.width() // 2, gv.height() // 2))
    monkeypatch.setattr(QtGui.QCursor, "pos", staticmethod(lambda: centre))
    qapp.processEvents()
    router = gv._proxy_key_router
    router.set_view_active(True)

    # Selected LAST, with nothing left queued: building the graph schedules an auto-created
    # view node that selects itself when it lands, and a selection made before that runs is
    # taken back -- leaving nothing for Ctrl+G to group.
    scene.set_selected_chunks([a])

    event = QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress,
        QtCore.Qt.Key.Key_G,
        QtCore.Qt.KeyboardModifier.ControlModifier,
    )
    handled = router.eventFilter(gv, event)
    _settle(qapp)

    assert handled is True, "the router must claim Ctrl+G rather than letting it fall through"
    assert _groups(scene), "Ctrl+G should have made a group"


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
