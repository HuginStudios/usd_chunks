"""Backdrops and sticky notes as USD prims (usd_chunks/graph/backdrops.py), no Qt."""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdUI

from usd_chunks.graph import backdrops


@pytest.fixture()
def stage():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/chunks", "Scope")
    return stage


def test_a_backdrop_round_trips(stage):
    prim = backdrops.create(stage, "/chunks", (10, 20, 300, 200), text="Lighting", color=(0.1, 0.2, 0.3))
    assert prim.GetTypeName() == "Backdrop"
    assert prim.IsA(UsdUI.Backdrop)
    info = backdrops.read(prim)
    assert info.rect == (10, 20, 300, 200)
    assert info.text == "Lighting"
    assert info.color == pytest.approx((0.1, 0.2, 0.3))
    assert not info.note


def test_position_is_stored_y_up_like_a_node(stage):
    prim = backdrops.create(stage, "/chunks", (10, 20, 300, 200))
    assert tuple(UsdUI.NodeGraphNodeAPI(prim).GetPosAttr().Get()) == (10, -20)


def test_a_note_is_a_flagged_backdrop(stage):
    prim = backdrops.create(stage, "/chunks", (0, 0, 100, 100), note=True, text="why")
    assert prim.GetTypeName() == "Backdrop"
    assert backdrops.is_note(prim)
    assert backdrops.read(prim).note
    assert backdrops.read(prim).color == pytest.approx(backdrops.NOTE_COLOR)


def test_names_do_not_collide(stage):
    stage.DefinePrim("/chunks/backdrop1")
    first = backdrops.create(stage, "/chunks", (0, 0, 100, 100))
    second = backdrops.create(stage, "/chunks", (0, 0, 100, 100))
    note = backdrops.create(stage, "/chunks", (0, 0, 100, 100), note=True)
    assert [p.GetName() for p in (first, second, note)] == ["backdrop2", "backdrop3", "note1"]


def test_list_finds_only_backdrops(stage):
    stage.DefinePrim("/chunks/Transform1", "Chunk")
    a = backdrops.create(stage, "/chunks", (0, 0, 100, 100))
    b = backdrops.create(stage, "/chunks", (0, 0, 100, 100), note=True)
    assert backdrops.list_backdrops(stage, "/chunks") == [a, b]
    assert backdrops.list_backdrops(stage, "/nowhere") == []


def test_chunks_root_is_made_when_missing():
    stage = Usd.Stage.CreateInMemory()
    prim = backdrops.create(stage, "/chunks", (0, 0, 100, 100))
    assert prim and stage.GetPrimAtPath("/chunks").GetTypeName() == "Scope"


def test_any_other_missing_root_makes_nothing():
    stage = Usd.Stage.CreateInMemory()
    assert backdrops.create(stage, "/chunks/Group1", (0, 0, 100, 100)) is None
    assert not stage.GetPrimAtPath("/chunks/Group1")


def test_edits_go_to_the_layer_it_lives_in(stage):
    session = stage.GetSessionLayer()
    prim = backdrops.create(stage, "/chunks", (0, 0, 100, 100), layer=session)
    backdrops.set_rect(prim, (5, 6, 150, 160))
    backdrops.set_color(prim, (1, 0, 0))
    backdrops.set_text(prim, "FX")
    root_spec = stage.GetRootLayer().GetPrimAtPath(prim.GetPath())
    assert root_spec is None or not root_spec.properties
    info = backdrops.read(prim)
    assert info.rect == (5, 6, 150, 160)
    assert info.color == pytest.approx((1, 0, 0))
    assert info.text == "FX"


def test_delete_removes_it_from_every_owned_layer(stage):
    prim = backdrops.create(stage, "/chunks", (0, 0, 100, 100))
    path = prim.GetPath()
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        backdrops.set_text(prim, "an over in another layer")
    assert backdrops.delete(stage.GetPrimAtPath(path))
    assert not stage.GetPrimAtPath(path)
    assert stage.GetPrimAtPath("/chunks")


def test_delete_leaves_a_foreign_file_alone(tmp_path):
    shared = Sdf.Layer.CreateNew(str(tmp_path / "sequence.usda"))
    stage = Usd.Stage.CreateInMemory()
    stage.GetRootLayer().subLayerPaths.append(shared.identifier)
    prim = backdrops.create(stage, "/chunks", (0, 0, 100, 100), layer=shared)
    assert not backdrops.delete(prim)
    assert stage.GetPrimAtPath(prim.GetPath())


def test_node_inside_goes_by_its_title_bar():
    box = (0, 0, 300, 200)
    assert backdrops.node_inside(box, (50, 50, 100, 40))
    # A tall node whose title bar is inside counts, however far its body hangs out.
    assert backdrops.node_inside(box, (50, 150, 100, 40))
    assert not backdrops.node_inside(box, (250, 50, 200, 40))  # middle is outside


def test_a_backdrop_must_be_fully_inside_but_a_note_only_half():
    box = (0, 0, 300, 200)
    assert backdrops.backdrop_inside(box, (10, 10, 100, 100), other_is_note=False)
    assert not backdrops.backdrop_inside(box, (250, 10, 100, 100), other_is_note=False)
    assert backdrops.backdrop_inside(box, (250, 10, 80, 100), other_is_note=True)
    assert not backdrops.backdrop_inside(box, box, other_is_note=False)


def test_fit_around_leaves_room_for_the_title_bar():
    rect = backdrops.fit_around([(100, 100, 50, 50), (300, 200, 50, 50)], padding=10, header=30)
    assert rect == (90, 60, 270, 200)
    assert backdrops.fit_around([]) is None
    # Never smaller than a backdrop can be.
    assert backdrops.fit_around([(0, 0, 1, 1)], padding=0, header=0)[2:] == backdrops.MIN_SIZE
