"""The Qt graph gives a new graph its Output, the way it used to give it a view.

Dropping the first node wires an Output to it and cooks. After that the Output stays
where it was wired: a second node dropped beside the first is not moved in front of it,
and starts blocked until someone wires it in.

The Output is picked like a view: key 1 cooks to it (2 and 3 are the views), and "Set
Output" over the "Set View" entries moves it onto the clicked node.
"""

from __future__ import annotations

import pytest
from pxr import Usd
from PySide6 import QtCore
from PySide6.QtTest import QTest

from qt_wait import pump_until
from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker
from usd_chunks.frontends.qt.widgets import slot_for_key


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _Scene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    yield qapp, reg, scene, stage, docker
    docker.close()
    docker.deleteLater()


def _drop(reg, scene, module="transform"):
    mod = reg.get_chunk_module(module)
    chunks = mod.create() if mod.loaded else mod.load()
    for c in chunks:
        scene.add_chunk(c)
    return chunks[0]


def _outputs(scene):
    return [c for c in scene.get_chunks() if chunk_stage.is_output_chunk(c)]


def _fed_by(stage, output):
    prim = stage.GetPrimAtPath(get_chunk_prim_path(output))
    return [str(p.GetPrimPath()) for p in prim.GetAttribute("inputs:chunk").GetConnections()]


def test_the_first_node_gets_an_output_wired_to_it(graph):
    qapp, reg, scene, stage, _ = graph
    first = _drop(reg, scene)
    assert pump_until(qapp, lambda: _outputs(scene)), "no Output was made for the first node"
    (output,) = _outputs(scene)
    assert _fed_by(stage, output) == [get_chunk_prim_path(first)]
    assert not any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()), "no default view any more"
    assert pump_until(qapp, lambda: not first.is_blocked()), "the first node feeds the Output, so it cooks"


def test_the_output_stays_where_it_was_wired(graph):
    qapp, reg, scene, stage, _ = graph
    first = _drop(reg, scene)
    assert pump_until(qapp, lambda: _outputs(scene))
    second = _drop(reg, scene)
    pump_until(qapp, lambda: False, seconds=0.5)

    (output,) = _outputs(scene)
    assert _fed_by(stage, output) == [get_chunk_prim_path(first)]
    assert second.is_blocked(), "not wired to the Output, so it doesn't run"


def _view_on_second(graph):
    """First node feeding the Output; a second, unwired one with View 2 on it, active."""
    qapp, reg, scene, _, docker = graph
    first = _drop(reg, scene)
    assert pump_until(qapp, lambda: _outputs(scene))
    second = _drop(reg, scene)
    gv = docker.tab_widget.get_current_graph_view()
    gv._on_create_view_for_path(get_chunk_prim_path(second), 1)
    assert pump_until(qapp, lambda: gv._view_state.active_slot == 1)
    assert pump_until(qapp, lambda: first.is_blocked() and not second.is_blocked())
    return first, second, gv


def test_number_keys_pick_the_output_then_the_views():
    keys = (QtCore.Qt.Key.Key_0, QtCore.Qt.Key.Key_1, QtCore.Qt.Key.Key_2, QtCore.Qt.Key.Key_3,
            QtCore.Qt.Key.Key_4, QtCore.Qt.Key.Key_A)
    assert [slot_for_key(k) for k in keys] == [None, 0, 1, 2, None, None]


def test_key_1_cooks_to_the_output_and_2_goes_back_to_the_view(graph):
    qapp = graph[0]
    first, second, gv = _view_on_second(graph)

    QTest.keyClick(gv, QtCore.Qt.Key.Key_1)
    assert pump_until(qapp, lambda: gv._view_state.active_slot == 0)
    assert pump_until(qapp, lambda: not first.is_blocked() and second.is_blocked())

    QTest.keyClick(gv, QtCore.Qt.Key.Key_2)
    assert pump_until(qapp, lambda: gv._view_state.active_slot == 1)

    QTest.keyClick(gv, QtCore.Qt.Key.Key_3)  # no View 3 in this graph: nothing changes
    qapp.processEvents()
    assert gv._view_state.active_slot == 1


def test_views_are_numbered_after_the_output(graph):
    scene = graph[2]
    _view_on_second(graph)
    assert [c.name for c in scene.get_chunks() if chunk_stage.is_view_chunk(c)] == ["View2"]


def test_set_output_sits_over_the_views_and_moves_the_output(graph, monkeypatch):
    qapp, _, scene, stage, _ = graph
    first, second, gv = _view_on_second(graph)
    shown = []

    def _exec(self, menu, _pos):
        shown.extend(a.text() for a in menu.actions() if a.text().startswith("Set "))
        return next(a for a in menu.actions() if a.text() == "Set Output")

    monkeypatch.setattr(type(gv._view_state), "_exec_menu", _exec)
    gv._show_chunk_menu(gv.scene()._nodes[get_chunk_prim_path(second)], QtCore.QPoint(0, 0))

    assert shown == ["Set Output", "Set View 2", "Set View 3"]
    (output,) = _outputs(scene)
    assert pump_until(qapp, lambda: _fed_by(stage, output) == [get_chunk_prim_path(second)])
    assert pump_until(qapp, lambda: gv._view_state.active_slot == 0)
    assert pump_until(qapp, lambda: not second.is_blocked() and first.is_blocked())


def test_a_rebuild_keeps_the_output_once_it_is_picked(graph):
    """Picking the Output forgets the view slot saved before, or the next rebuild would
    bring that view back."""
    qapp = graph[0]
    first, _, gv = _view_on_second(graph)
    QTest.keyClick(gv, QtCore.Qt.Key.Key_1)
    assert pump_until(qapp, lambda: gv._view_state.active_slot == 0)

    gv._restore_active_view()  # what every graph rebuild runs
    pump_until(qapp, lambda: False, seconds=0.3)

    assert gv._view_state.active_slot == 0
    assert not first.is_blocked()
