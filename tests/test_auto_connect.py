"""Auto-connect: with the box ticked, each chunk made from the menu goes on the end of the chain."""
from __future__ import annotations

import pytest
from PySide6 import QtCore

from tests.test_copy_paste import _make_nodes, _pump, graph  # noqa: F401 -- graph is a fixture
from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.frontends.qt import window_state


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    """A throwaway settings file, so ticking the box never touches the real one."""
    path = str(tmp_path / "settings.ini")
    monkeypatch.setattr(
        window_state, "_settings",
        lambda: QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat),
    )


def _view(graph):  # noqa: F811
    return graph[3].tab_widget.get_current_graph_view()


def _start(graph, auto: bool):  # noqa: F811
    """The graph's first chunk, added as any is, with the box set to auto."""
    _, _, [first], _ = _make_nodes(graph)
    _pump(graph[0], 0.6)
    _view(graph)._auto_connect_box.setChecked(auto)
    return first


def _make(graph, x: float):  # noqa: F811
    """A Transform from the menu, at x; returns the new chunk."""
    qapp, reg, scene, _docker = graph
    before = set(scene.get_chunks())
    entry = chunk_registry.MenuEntry(module=reg.get_chunk_module("transform"), namespace=("transform",))
    _view(graph).create_chunk_from_entry(entry, QtCore.QPointF(x, 0.0))
    _pump(qapp, 0.6)
    [new] = [c for c in scene.get_chunks() if c not in before and not chunk_stage.is_terminal_chunk(c)]
    return new


def _fed_by(scene, chunk) -> list[str]:
    """The prim paths wired into chunk's exec input."""
    prim = scene.get_stage().GetPrimAtPath(chunk_stage.get_chunk_prim_path(chunk))
    return [str(p.GetPrimPath()) for p in chunk_stage.exec_input_connections(prim)]


def _output(scene):
    return chunk_stage.find_output_chunk(scene.get_stage(), scene, None)


def test_each_new_chunk_goes_on_the_end_of_the_chain(graph):  # noqa: F811
    scene = graph[2]
    first = _start(graph, auto=True)
    second = _make(graph, 300.0)
    third = _make(graph, 600.0)

    assert _fed_by(scene, second) == [chunk_stage.get_chunk_prim_path(first)]
    assert _fed_by(scene, third) == [chunk_stage.get_chunk_prim_path(second)]
    # The Output follows the end, so the whole chain cooks.
    assert _fed_by(scene, _output(scene)) == [chunk_stage.get_chunk_prim_path(third)]
    assert scene.get_selected_chunks() == (third,)


def test_unticked_a_new_chunk_is_not_wired(graph):  # noqa: F811
    scene = graph[2]
    _start(graph, auto=False)
    second = _make(graph, 300.0)

    assert _fed_by(scene, second) == []


def test_a_new_chunk_goes_after_the_selected_one(graph):  # noqa: F811
    scene = graph[2]
    first = _start(graph, auto=True)
    _make(graph, 300.0)

    scene.clear_chunk_selection()
    first.set_selected(True)
    branch = _make(graph, 300.0)

    assert _fed_by(scene, branch) == [chunk_stage.get_chunk_prim_path(first)]


def test_the_box_is_remembered(graph):  # noqa: F811
    _start(graph, auto=True)

    assert window_state.settings().value("graph/autoConnect", False, type=bool) is True
