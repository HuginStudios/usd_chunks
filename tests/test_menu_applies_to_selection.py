"""A right-click action applies to every selected chunk, not just the clicked one.

Selecting six chunks and choosing Remove removed one. Same for Pin. The menu was built
around the chunk under the cursor and never asked what else was selected.

Right-clicking a node OUTSIDE the selection still acts on that node alone -- the click is
a new target, not an addition to an old one, which is how every other app behaves.
"""
from __future__ import annotations

import pytest
from PySide6 import QtCore
from pxr import Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_qt import ChunkWidgetDocker
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


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
    return qapp, reg, scene, d


def _mk(reg, scene, key="transform"):
    mod = reg.get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _view(docker, qapp):
    qapp.processEvents()
    gv = docker.tab_widget.get_current_graph_view()
    assert gv is not None
    return gv


def _select(qapp, scene, *chunks):
    """Exactly these, and nothing else.

    Through the scene rather than by setting SELECTED directly: the scene owns the
    selection and deselects what it replaces.

    Drained first, on purpose. Building the graph queues an auto-created view node that
    selects itself when it lands, so a selection made before that runs is taken back --
    a real race the app has too, not a test artifact.
    """
    qapp.processEvents()
    scene.set_selected_chunks(list(chunks))


def _open_menu_now(gv, chunk, qapp):
    """Right-click without draining first, so nothing queued can steal the selection."""
    node = gv.scene()._nodes[chunk_usd.get_chunk_prim_path(chunk)]
    gv._show_chunk_menu(node, QtCore.QPoint(0, 0))
    qapp.processEvents()


def _open_menu(gv, chunk, qapp):
    """Right-click `chunk`'s node, the way the graph does."""
    qapp.processEvents()
    node = gv.scene()._nodes[chunk_usd.get_chunk_prim_path(chunk)]
    gv._show_chunk_menu(node, QtCore.QPoint(0, 0))
    qapp.processEvents()


def _pick(text_startswith):
    """Choose the menu entry whose label starts with text, ignoring any count suffix."""
    def _exec(self, menu, _global_pos):
        for act in menu.actions():
            if act.text().startswith(text_startswith):
                return act
        return None
    return _exec


# Which chunks an action targets


def test_selection_is_the_target_when_the_clicked_chunk_is_in_it(graph):
    qapp, reg, scene, d = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    _select(qapp, scene, a, b)

    targets = gv.selected_chunks(a)

    assert set(targets) == {a, b}
    assert targets[0] is a, "the clicked chunk leads, so it decides direction for a toggle"


def test_clicking_outside_the_selection_targets_only_that_chunk(graph):
    """A right-click on an unselected node is a new target, not an addition."""
    qapp, reg, scene, d = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    _select(qapp, scene, a)

    assert gv.selected_chunks(b) == [b]


# The actions themselves


def test_pin_applies_to_the_whole_selection(graph, monkeypatch):
    qapp, reg, scene, d = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    _select(qapp, scene, a, b)

    monkeypatch.setattr(type(gv._view_state), "_exec_menu", _pick("Pin to Params"))
    _open_menu_now(gv, a, qapp)

    assert chunk_usd.is_chunk_pinned(a)
    assert chunk_usd.is_chunk_pinned(b), "the unclicked half of the selection was left behind"


def test_remove_deletes_the_whole_selection(graph, monkeypatch):
    qapp, reg, scene, d = graph
    a, b, spared = _mk(reg, scene), _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    _select(qapp, scene, a, b)

    monkeypatch.setattr(type(gv._view_state), "_exec_menu", _pick("remove"))
    _open_menu_now(gv, a, qapp)

    live = set(scene.get_chunks())
    assert a not in live and b not in live, "both selected chunks should be gone"
    assert spared in live, "an unselected chunk must survive"


def test_an_unselected_chunk_is_removed_alone(graph, monkeypatch):
    qapp, reg, scene, d = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    _select(qapp, scene, a)

    monkeypatch.setattr(type(gv._view_state), "_exec_menu", _pick("remove"))
    _open_menu_now(gv, b, qapp)

    live = set(scene.get_chunks())
    assert b not in live
    assert a in live, "the selection must not be swept up by a click outside it"


def test_the_label_says_how_many_it_will_hit(graph, monkeypatch):
    """An action about to hit six chunks should not look like one that hits one."""
    qapp, reg, scene, d = graph
    a, b = _mk(reg, scene), _mk(reg, scene)
    gv = _view(d, qapp)
    _select(qapp, scene, a, b)

    seen = []

    def _capture(self, menu, _pos):
        seen.extend(act.text() for act in menu.actions())
        return None

    monkeypatch.setattr(type(gv._view_state), "_exec_menu", _capture)
    _open_menu_now(gv, a, qapp)

    assert any("(2 chunks)" in t for t in seen), seen
    assert any(t == "Rename…" for t in seen), (
        "Rename stays singular -- two chunks cannot share one name"
    )
