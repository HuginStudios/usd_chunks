"""Small core helpers whose edge cases no test reached.

- engine.node_size: the size a node's prim stores, or the default,
- engine.create_view_for: the cases where it must refuse or reuse rather than make a view,
- chunk_utils.apply_namespace / strip_namespace: a hosted network's paths, there and back,
- chunk_utils.focus_point_of: what "frame selection" aims at.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_utils
from usd_chunks.graph import engine as chunk_graph


# -- node size ---------------------------------------------------------------------------------

def test_an_authored_node_size_reads_back():
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/chunks/n", "Chunk")
    prim.CreateAttribute(chunk_graph.SIZE_ATTR, Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(120, 80))

    assert chunk_graph.node_size(prim) == (120.0, 80.0)


def test_no_node_size_falls_back_to_the_default():
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/chunks/n", "Chunk")

    assert chunk_graph.node_size(prim) == chunk_graph.DEFAULT_NODE_SIZE
    assert chunk_graph.node_size(prim, (1.0, 2.0)) == (1.0, 2.0)


# -- create_view_for refusals ------------------------------------------------------------------

def _view(stage, path, slot, target=None):
    prim = stage.DefinePrim(path, "Chunk")
    prim.CreateAttribute(chunk_graph.CHUNK_TYPE_ATTR, Sdf.ValueTypeNames.Token).Set(chunk_graph.VIEW_MODULE)
    prim.CreateAttribute(chunk_graph.VIEW_SLOT_ATTR, Sdf.ValueTypeNames.Int).Set(slot)
    if target:
        prim.CreateAttribute(chunk_graph.EXEC_INPUT, Sdf.ValueTypeNames.Token).SetConnections(
            [Sdf.Path(f"{target}.{chunk_graph.EXEC_OUTPUT}")])
    return prim


def test_no_view_is_made_for_a_missing_prim():
    stage = Usd.Stage.CreateInMemory()

    assert chunk_graph.create_view_for(stage, None, "/chunks/nothing") is None


def test_no_view_is_made_of_a_view():
    stage = Usd.Stage.CreateInMemory()
    _view(stage, "/chunks/View1", 1)

    assert chunk_graph.create_view_for(stage, None, "/chunks/View1") is None


def test_a_node_already_viewed_gets_its_existing_slot_back():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/chunks/node", "Chunk")
    _view(stage, "/chunks/View2", 2, target="/chunks/node")

    assert chunk_graph.existing_view_slot_for(stage, "/chunks/node") == 2
    assert chunk_graph.create_view_for(stage, None, "/chunks/node") == 2


def test_no_view_is_made_when_the_container_has_no_slot_left():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/chunks/node", "Chunk")
    for slot in chunk_graph.VIEW_SLOTS:
        _view(stage, f"/chunks/View{slot}", slot)

    assert chunk_graph.create_view_for(stage, None, "/chunks/node") is None


def test_setting_a_taken_slot_repoints_its_view():
    """Set View 1 on another node moves View1 there rather than making a second view."""
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/chunks/a", "Chunk")
    stage.DefinePrim("/chunks/b", "Chunk")
    view = _view(stage, "/chunks/View1", 1, target="/chunks/a")

    assert chunk_graph.create_view_for(stage, None, "/chunks/b", slot=1) == 1
    assert view.GetAttribute(chunk_graph.EXEC_INPUT).GetConnections() == [
        Sdf.Path(f"/chunks/b.{chunk_graph.EXEC_OUTPUT}")]


def test_a_node_can_be_set_into_both_slots():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/chunks/a", "Chunk")
    _view(stage, "/chunks/View1", 1, target="/chunks/a")
    view2 = _view(stage, "/chunks/View2", 2, target="/chunks/x")

    assert chunk_graph.create_view_for(stage, None, "/chunks/a", slot=2) == 2
    assert view2.GetAttribute(chunk_graph.EXEC_INPUT).GetConnections() == [
        Sdf.Path(f"/chunks/a.{chunk_graph.EXEC_OUTPUT}")]


def test_a_slot_outside_the_two_is_refused():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/chunks/a", "Chunk")

    assert chunk_graph.create_view_for(stage, None, "/chunks/a", slot=3) is None


def test_switching_views_in_a_missing_context_is_refused():
    stage = Usd.Stage.CreateInMemory()

    changed, status = chunk_graph.activate_view_slot(stage, object(), "/chunks/none", {}, 1)

    assert not changed and "no context" in status


# -- namespaces --------------------------------------------------------------------------------

@pytest.mark.parametrize("paths", ["/A/B", "/A/B, /C", "/"])
def test_strip_undoes_apply(paths):
    applied = chunk_utils.apply_namespace("/Shot", paths)

    assert chunk_utils.strip_namespace("/Shot", applied) == paths


def test_the_namespace_itself_strips_to_the_root():
    assert chunk_utils.strip_namespace("/Shot/", "/Shot") == "/"


def test_a_path_outside_the_namespace_is_left_alone():
    assert chunk_utils.strip_namespace("/Shot", "/Other/X") == "/Other/X"


def test_no_namespace_changes_nothing():
    assert chunk_utils.strip_namespace("", "/A") == "/A"
    assert chunk_utils.apply_namespace("", "/A") == "/A"


# -- focus point -------------------------------------------------------------------------------

def test_the_focus_is_the_centre_of_what_has_extent():
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/Cube")
    UsdGeom.XformCommonAPI(cube).SetTranslate((10, 0, 0))

    assert chunk_utils.focus_point_of(stage, [cube.GetPrim()]) == Gf.Vec3d(10, 0, 0)


def test_a_prim_with_no_extent_counts_from_its_origin():
    stage = Usd.Stage.CreateInMemory()
    xf = UsdGeom.Xform.Define(stage, "/Empty")
    UsdGeom.XformCommonAPI(xf).SetTranslate((0, 4, 0))

    assert chunk_utils.focus_point_of(stage, [xf.GetPrim()]) == Gf.Vec3d(0, 4, 0)


def test_the_focus_averages_and_skips_the_excluded_prim():
    stage = Usd.Stage.CreateInMemory()
    a = UsdGeom.Xform.Define(stage, "/A")
    b = UsdGeom.Xform.Define(stage, "/B")
    cam = UsdGeom.Xform.Define(stage, "/Cam")
    UsdGeom.XformCommonAPI(a).SetTranslate((2, 0, 0))
    UsdGeom.XformCommonAPI(b).SetTranslate((4, 0, 0))
    UsdGeom.XformCommonAPI(cam).SetTranslate((100, 0, 0))

    point = chunk_utils.focus_point_of(stage, [a.GetPrim(), b.GetPrim(), cam.GetPrim()],
                                       exclude=Sdf.Path("/Cam"))

    assert point == Gf.Vec3d(3, 0, 0)


def test_nothing_measurable_has_no_focus():
    stage = Usd.Stage.CreateInMemory()
    scope = stage.DefinePrim("/Scope", "Scope")

    assert chunk_utils.focus_point_of(stage, [scope, stage.GetPrimAtPath("/missing")]) is None
