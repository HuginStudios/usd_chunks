"""Regression: reported live -- double-clicking a node's header to expand/collapse it
sometimes left it looking permanently stuck. A debug log of a single double-click showed
two full STATE_CHANGED solves back to back, toggling ChunkFlags.State.COLLAPSED on and then
immediately back off -- net no-op. Only one code path (ChunkNodeItem.mouseDoubleClickEvent)
ever toggles this state, so the two toggles must come from the handler being invoked twice
for what the user experienced as one gesture -- the exact Qt-level cause wasn't reproducible
outside a live session, so the fix debounces the toggle itself rather than the delivery.
"""

from __future__ import annotations

import time

import pytest
from pxr import Usd

from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt import nodegraph


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


class _FakeDoubleClickEvent:
    """Minimal stand-in for a QGraphicsSceneMouseEvent -- mouseDoubleClickEvent only ever
    calls .pos() and .accept() on it."""

    def __init__(self, y: float):
        self._y = y
        self.accepted = False

    def pos(self):
        class _P:
            def __init__(self, y):
                self._y = y

            def y(self):
                return self._y

        return _P(self._y)

    def accept(self):
        self.accepted = True


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()

    return qapp, reg, scene, docker


def test_header_double_click_delivered_twice_toggles_once(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    qapp.processEvents()

    assert not chunk.has_state(ChunkFlags.State.COLLAPSED)

    # Simulate the handler firing twice for one real gesture (the observed live bug).
    node.mouseDoubleClickEvent(_FakeDoubleClickEvent(5.0))
    node.mouseDoubleClickEvent(_FakeDoubleClickEvent(5.0))

    assert chunk.has_state(ChunkFlags.State.COLLAPSED), (
        "a double-delivered double-click must still net out to exactly one toggle, "
        "not cancel itself back to the original state"
    )


def test_a_later_genuine_double_click_still_toggles(graph) -> None:
    qapp, reg, scene, docker = graph
    mod = reg.get_chunk_module("transform")
    chunk = _mk(mod, scene)
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    node = gv.scene()._nodes[get_chunk_prim_path(chunk)]
    qapp.processEvents()

    node.mouseDoubleClickEvent(_FakeDoubleClickEvent(5.0))
    assert chunk.has_state(ChunkFlags.State.COLLAPSED)

    # A real, separate double-click well after the debounce window must still work --
    # the fix must not permanently latch the node.
    time.sleep(nodegraph._COLLAPSE_TOGGLE_DEBOUNCE + 0.05)
    node.mouseDoubleClickEvent(_FakeDoubleClickEvent(5.0))
    assert not chunk.has_state(ChunkFlags.State.COLLAPSED)
