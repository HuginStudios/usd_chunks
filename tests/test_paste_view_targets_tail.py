"""A view created for a paste points at the end of the pasted chain, not its start.

Reported: paste several wired chunks and the view sometimes lands on the first one.

Paste defers its wiring by 80ms -- the pasted nodes' ports do not exist yet. Until that
lands every pasted chunk still looks like an unwired exec tail, so the generic auto-view
took tails[0], which is the first node in whatever order the scene yields. Hence
"sometimes": it is a race with a timer.

Drag-and-drop import already solved this by holding the generic pass off and then
choosing among just the imported nodes. Paste now does the same.
"""
from __future__ import annotations

import time

import pytest
from pxr import Sdf, Usd
from PySide6 import QtCore

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import (
    connect_exec, get_chunk_prim_path, initialize as _chunk_usd_init,
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


def _settle(qapp):
    """Let the paste's deferred wiring (80ms) and everything it triggers finish.

    By the clock, not by a count of processEvents() calls: 30 of those finish in well under
    80ms on an idle machine, so whether the deferred wiring had even fired yet depended on
    how busy the machine -- and every earlier test's leftover widgets -- happened to be.
    """
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        qapp.processEvents()
        QtCore.QThread.msleep(10)
    qapp.processEvents()


def _view_targets(scene, stage):
    """What each view chunk is pointed at."""
    out = []
    for c in scene.get_chunks():
        if not chunk_usd.is_view_chunk(c):
            continue
        prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
        if not (prim and prim.IsValid()):
            continue
        for attr in chunk_usd.iter_exec_inputs(prim):
            out.extend(str(conn.GetPrimPath()) for conn in attr.GetConnections())
    return out


def _drop_views(scene, qapp):
    """Clear the context's view, so a paste is what triggers the next one.

    Both the generic and the scoped auto-view refuse to steal an existing view -- so with
    one already in place a paste creates none at all, and a test that leaves it there is
    asserting about a view the paste had no hand in.
    """
    for c in list(scene.get_chunks()):
        if chunk_usd.is_view_chunk(c):
            c.request_delete()
    qapp.processEvents()


def test_the_auto_view_is_held_until_the_pasted_chain_is_wired(graph):
    """The guarantee the fix actually makes.

    Paste defers its wiring, and until that lands every pasted chunk still looks like an
    unwired tail -- so the generic auto-view, which takes tails[0], can pick the first node
    of the chain. Holding it off until the wiring completes is what removes the race.

    Asserted on the hold rather than on where the view lands, because headless the timing
    does not reproduce the race: the deferred wiring happens to win, and the view ends up
    right whether or not anything holds the generic pass off. The hold is the difference.
    """
    qapp, reg, scene, d, stage = graph
    head, tail = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    connect_exec(stage, reg.scene, get_chunk_prim_path(head), get_chunk_prim_path(tail))
    qapp.processEvents()

    scene.set_selected_chunks([head, tail])
    gv._copy_selected_to_clipboard()
    gv._paste_clipboard_at(QtCore.QPointF(600, 600))

    # Deliberately before _settle: this is the window the race lives in.
    assert gv._suppress_default_view is True, (
        "the generic auto-view must be held off while the pasted chain is still unwired, "
        "or it takes the first pasted node as the tail"
    )

    _settle(qapp)


def test_the_pasted_view_points_at_the_tail_not_the_head(graph):
    qapp, reg, scene, d, stage = graph
    head, tail = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    connect_exec(stage, reg.scene, get_chunk_prim_path(head), get_chunk_prim_path(tail))
    qapp.processEvents()

    scene.set_selected_chunks([head, tail])
    gv._copy_selected_to_clipboard()
    _drop_views(scene, qapp)

    pasted = gv._paste_clipboard_at(QtCore.QPointF(600, 600))
    _settle(qapp)
    assert len(pasted) == 2, f"expected both chunks pasted, got {len(pasted)}"

    # Which of the two the wiring actually made the tail, rather than assuming an order.
    pasted_tail = [c for c in pasted if _is_fed_by_another(stage, c, pasted)]
    assert len(pasted_tail) == 1, (
        f"the pasted chain must be wired for this to mean anything: {pasted_tail}"
    )
    pasted_head = [c for c in pasted if c not in pasted_tail]

    targets = _view_targets(scene, stage)
    assert targets, "a view should have been created for the paste"
    assert get_chunk_prim_path(pasted_tail[0]) in targets, (
        f"the view should point at the end of the pasted chain; it points at {targets}"
    )
    assert get_chunk_prim_path(pasted_head[0]) not in targets, (
        "the view points at the head of the pasted chain -- the reported bug"
    )


def _is_fed_by_another(stage, chunk, group):
    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(chunk)))
    if not (prim and prim.IsValid()):
        return False
    others = {get_chunk_prim_path(c) for c in group if c is not chunk}
    for attr in chunk_usd.iter_exec_inputs(prim):
        if any(str(conn.GetPrimPath()) in others for conn in attr.GetConnections()):
            return True
    return False


def test_the_generic_auto_view_is_released_after_a_paste(graph):
    """The hold must not outlive the paste, or the view never auto-creates again."""
    qapp, reg, scene, d, stage = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()
    connect_exec(stage, reg.scene, get_chunk_prim_path(a), get_chunk_prim_path(b))
    qapp.processEvents()

    scene.set_selected_chunks([a, b])
    gv._copy_selected_to_clipboard()
    gv._paste_clipboard_at(QtCore.QPointF(600, 600))
    _settle(qapp)

    assert gv._suppress_default_view is False, (
        "the auto-view hold taken for the paste was never released"
    )


def test_pasting_a_single_chunk_still_works(graph):
    """No edges means no deferred wiring; the paste must not hang on a hold it never took."""
    qapp, reg, scene, d, stage = graph
    a = _mk(reg, scene)
    gv = _view(d, qapp)
    qapp.processEvents()

    scene.set_selected_chunks([a])
    gv._copy_selected_to_clipboard()
    pasted = gv._paste_clipboard_at(QtCore.QPointF(600, 600))
    _settle(qapp)

    assert len(pasted) == 1
    assert gv._suppress_default_view is False
