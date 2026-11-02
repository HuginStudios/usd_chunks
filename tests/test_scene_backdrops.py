"""Backdrops and sticky notes from a script: the scene API."""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import Scene, chunk_stage
from usd_chunks.graph import backdrops


@pytest.fixture
def scene():
    with Scene.new() as scene:
        yield scene


def test_a_backdrop_frames_the_chunks_it_is_given(scene):
    box = scene.create.cube(prim_path="/geo/box")
    lift = scene.create.transform(box)
    light = scene.create.light()
    prim = scene.add_backdrop([box, lift], title="Geometry", color="green")
    info = backdrops.read(prim)
    assert info.text == "Geometry"
    assert info.color == pytest.approx(backdrops.PALETTE["Green"])
    # Next to the chunks, in their graph.
    assert prim.GetParent().GetPath() == chunk_stage.get_chunk_prim_path(box).rsplit("/", 1)[0]
    assert set(scene.chunks_in_backdrop(prim)) == {box, lift}
    assert light not in scene.chunks_in_backdrop(prim)


def test_a_note_goes_above_a_chunk(scene):
    box = scene.create.cube(prim_path="/geo/box")
    note = scene.add_note("Builds the box", at=box, color=(0.2, 0.4, 0.6))
    info = backdrops.read(note)
    assert info.note and info.text == "Builds the box"
    assert info.color == pytest.approx((0.2, 0.4, 0.6))
    _, box_y, _, _ = scene._node_rect(box)
    assert info.rect[1] + info.rect[3] < box_y


def test_list_change_and_remove(scene):
    box = scene.create.cube(prim_path="/geo/box")
    lift = scene.create.transform(box)
    frame = scene.add_backdrop([box, lift], title="Geo")
    note = scene.add_note("why", at=box)
    assert scene.get_backdrops() == [frame, note]
    assert scene.get_backdrops(notes=False) == [frame]

    scene.set_backdrop(frame, title="Shape", color=(1.0, 0.0, 0.0))
    assert backdrops.read(frame).text == "Shape"
    assert backdrops.read(frame).color == pytest.approx((1.0, 0.0, 0.0))
    scene.set_backdrop(str(frame.GetPath()), around=[lift])
    assert scene.chunks_in_backdrop(frame) == [lift]
    scene.set_backdrop(frame, rect=(0, 0, 300, 200))
    assert backdrops.read(frame).rect == (0, 0, 300, 200)

    assert scene.remove_backdrop(note)
    assert scene.get_backdrops() == [frame]
    assert box in scene.get_chunks()


def test_saved_and_opened_again(scene, tmp_path):
    box = scene.create.cube(prim_path="/geo/box")
    scene.add_backdrop([box], title="Geo", color="Purple")
    scene.add_note("why", at=box)
    path = tmp_path / "net.usda"
    scene.save(str(path))
    file_stage = Usd.Stage.Open(str(path))
    assert sorted(backdrops.read(p).text for p in file_stage.Traverse() if backdrops.is_backdrop(p)) == ["Geo", "why"]


def test_mistakes_say_what_is_wrong(scene):
    box = scene.create.cube(prim_path="/geo/box")
    with pytest.raises(ValueError, match="chunks to frame"):
        scene.add_backdrop()
    with pytest.raises(ValueError, match="unknown colour"):
        scene.add_backdrop([box], color="mauve")
    with pytest.raises(ValueError, match="not a backdrop"):
        scene.set_backdrop(chunk_stage.get_chunk_prim_path(box), title="x")
