"""The mouse's back/forward buttons retrace where you have been.

Dive into a chunkgroup, press back, and you are out again -- the way a browser works.
Tabs already are the navigation model here, so the history is a list of the contexts
that have been current, and back/forward walk it.
"""
from __future__ import annotations


import pytest
from pxr import Usd
from PySide6 import QtCore, QtGui

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_qt_nodegraph import ChunkGraphView
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def docker(qapp):
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
    return qapp, reg, scene, d


def _mk(reg, scene, key):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _dive(docker, qapp, group):
    """Enter a group the way double-clicking its node does."""
    docker._on_dive_into(group)
    qapp.processEvents()


def _current(docker):
    ctx = docker._current_context()
    return ctx.name if ctx is not None else None


def test_back_returns_to_where_you_came_from(docker):
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    group = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()
    before = _current(d)

    _dive(d, qapp, group)
    assert _current(d) == group.name, "the dive must actually change context"

    assert d.navigate_back() is True
    qapp.processEvents()
    assert _current(d) == before


def test_forward_returns_to_where_back_came_from(docker):
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    group = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()

    _dive(d, qapp, group)
    d.navigate_back()
    qapp.processEvents()

    assert d.navigate_forward() is True
    qapp.processEvents()
    assert _current(d) == group.name


def test_back_at_the_beginning_does_nothing(docker):
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    qapp.processEvents()

    while d.navigate_back():
        qapp.processEvents()

    assert d.navigate_back() is False, "walking off the start must report it, not wrap"


def test_forward_is_discarded_by_going_somewhere_new(docker):
    """Same as a browser: a new move drops the branch you had walked back out of."""
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    first = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()
    _dive(d, qapp, first)
    d.navigate_back()
    qapp.processEvents()

    second = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()
    _dive(d, qapp, second)

    assert d.navigate_forward() is False
    assert _current(d) == second.name


def test_retracing_does_not_itself_get_recorded(docker):
    """Going back must not append a step, or you could never get past where you started."""
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    group = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()
    _dive(d, qapp, group)

    depth = len(d._nav_history)
    d.navigate_back()
    qapp.processEvents()
    d.navigate_forward()
    qapp.processEvents()

    assert len(d._nav_history) == depth


def test_a_deleted_group_is_stepped_over(docker):
    """History holds weak refs; a group deleted while still in it must not strand back."""
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    group = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()
    _dive(d, qapp, group)

    group.request_delete()
    qapp.processEvents()

    d.navigate_back()
    qapp.processEvents()
    assert _current(d) != group.name


def test_the_side_buttons_reach_the_history(docker):
    """The graph view is what receives the press; the panel owns the history."""
    qapp, reg, scene, d = docker
    _mk(reg, scene, "transform")
    group = _mk(reg, scene, "chunkgroup")
    qapp.processEvents()
    _dive(d, qapp, group)

    gv = d.tab_widget.get_current_graph_view()
    assert isinstance(gv, ChunkGraphView)
    assert gv._owning_docker() is d, "the view must find the panel that owns the history"

    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QPointF(10, 10),
        QtCore.Qt.MouseButton.BackButton,
        QtCore.Qt.MouseButton.BackButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    gv.mousePressEvent(event)
    qapp.processEvents()

    assert event.isAccepted()
    assert _current(d) != group.name, "the back button must have left the group"
