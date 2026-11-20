"""The node graph's Styles menu: a picture per style, put on the prim picked in the viewport."""
from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets
from pxr import UsdGeom

from tests.test_copy_paste import _make_nodes, _pump, graph  # noqa: F401 -- graph is a fixture
from usd_chunks import chunk_stage
from usd_chunks.frontends.qt import styles_menu, window_state


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    """A throwaway settings file, so the Auto-connect box never touches the real one."""
    path = str(tmp_path / "settings.ini")
    monkeypatch.setattr(window_state, "_settings",
                        lambda: QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat))


def _crate(graph):  # noqa: F811
    """A cube mesh with divisions at /crate, the graph's first chunk."""
    qapp = graph[0]
    scene, view, [cube], _ = _make_nodes(graph, module="mesh")
    for name, value in (("prim_path", "/crate"), ("mesh_type", "cube"), ("dimx", 15), ("dimy", 15), ("dimz", 15)):
        cube.get_param(name).value = value
    _pump(qapp, 0.6)
    return scene, view, cube


def _grid(view) -> styles_menu.StyleGrid:
    menu = view._styles_menu
    menu.aboutToShow.emit()  # what opening it does, without a modal popup
    [action] = [a for a in menu.actions() if isinstance(a, QtWidgets.QWidgetAction)]
    return action.defaultWidget()


def test_a_picked_style_goes_on_the_picked_prim_and_on_the_end_of_the_chain(graph):  # noqa: F811
    qapp = graph[0]
    scene, view, cube = _crate(graph)
    stage = scene.get_stage()
    scene.get_selected_prims = lambda: [stage.GetPrimAtPath("/crate")]
    before = set(scene.get_chunks())

    grid = _grid(view)
    buttons = {b.text(): b for b in grid.buttons}
    assert {"Worn Paint", "Dirty Stone", "Rubble", "Hanging Vines"} <= set(buttons)
    assert all(b.isEnabled() for b in buttons.values())
    assert not buttons["Worn Paint"].icon().isNull(), "its picture"
    buttons["Worn Paint"].click()
    _pump(qapp, 3.0)

    [style] = [c for c in scene.get_chunks() if c not in before and c.get_param("file_path") is not None]
    assert style.get_param("primpath_location").value == "/crate"
    prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(style))
    assert [str(p.GetPrimPath()) for p in chunk_stage.exec_input_connections(prim)] == \
        [chunk_stage.get_chunk_prim_path(cube)], "wired after the chunk that makes the crate"
    crate = stage.GetPrimAtPath("/crate")
    assert crate.GetTypeName() == "Mesh"
    assert UsdGeom.PrimvarsAPI(crate).GetPrimvar("wear"), "worn"


def test_with_nothing_picked_the_styles_wait_for_a_pick(graph):  # noqa: F811
    scene, view, _ = _crate(graph)
    scene.get_selected_prims = lambda: []

    grid = _grid(view)

    assert grid.buttons and not any(b.isEnabled() for b in grid.buttons)


def test_rubble_from_the_menu_scatters_its_stones(graph):  # noqa: F811
    qapp = graph[0]
    scene, view, cube = _crate(graph)
    stage = scene.get_stage()
    from tests.test_styles import _grid_steps
    _grid_steps(stage, "/steps")
    scene.get_selected_prims = lambda: [stage.GetPrimAtPath("/steps")]

    {b.text(): b for b in _grid(view).buttons}["Rubble"].click()
    _pump(qapp, 4.0)

    scatter = next(c for c in scene.get_chunks() if c.name == "Scatter")
    stones = UsdGeom.PointInstancer(stage.GetPrimAtPath("/steps/rubble/stones"))
    assert stones and len(stones.GetPositionsAttr().Get() or []) > 10, scatter.get_param("readout").value


def test_a_style_stays_a_node_of_the_graph_it_was_put_in(graph):  # noqa: F811
    """Hosting its network makes the Reference a group; that must not open it as a tab and
    take the view (and the cook) into it, away from the scene's own graph."""
    qapp, _reg, _scene, docker = graph
    scene, view, _ = _crate(graph)
    stage = scene.get_stage()
    tabs = docker.tab_widget.count()
    scene.get_selected_prims = lambda: [stage.GetPrimAtPath("/crate")]

    before = set(scene.get_chunks())
    {b.text(): b for b in _grid(view).buttons}["Worn Paint"].click()
    _pump(qapp, 3.0)
    [host] = [c for c in scene.get_chunks() if c not in before and c.get_param("file_path") is not None]
    assert chunk_stage.is_network_host(host)
    # usdview loads the host again when the arc lands on its prim (a resync), which is what
    # gave it a tab: the same CHUNK_LOADED handler, called as that does.
    docker._loaded_module_callback(None, host)
    _pump(qapp, 0.5)

    assert docker.tab_widget.count() == tabs
    assert docker.tab_widget.get_current_graph_view() is view
