"""Duplicate Prim's destination path survives a save and reopen.

Reported: duplicate FlowerPotA_5, get FlowerPotA_5_copy1, wire a Transform at that copy,
group them, save, reopen -- and the Transform does nothing, because the duplicate is now
at FlowerPotA_5_copy. The trailing "1" is gone.

The save is not at fault: the .usda holds _copy1, and holds the duplicated prim too --
saving bakes the result into the file. That baked prim is what breaks the reopen. execute()
ended with a second pass that re-generated the destination whenever a prim already stood at
it, which reads as collision handling but fires hardest on this chunk's own output: on
reopen FlowerPotA_5_copy1 genuinely is on the stage, so the check called that a collision
and renamed the destination to the still-free FlowerPotA_5_copy.

Nothing tells the Transform chunk, which goes on pointing at a prim that no longer exists,
silently does nothing, and leaves the copy sitting on top of its original. Reopening the
same file twice moved it again.

A destination that is already set is an instruction, not a suggestion. Only an empty field
gets filled in, and _generate_dest_path already steps around occupied paths, so the
suggestion it makes cannot collide in the first place.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init, save_scene


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return s


def _mk(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


SOURCE = "/Kitchen_set/Props_grp/FlowerPotA_5"
CHOSEN_DEST = SOURCE + "_copy1"


@pytest.fixture
def reopened(qapp, tmp_path):
    """A Duplicate Prim whose destination the user set by hand, saved and reopened."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, SOURCE)
    scene = _scene_for(stage)

    dup = _mk(scene, "core_usd.duplicate_prim")
    dup.get_param("source_prim").set_prims([stage.GetPrimAtPath(SOURCE)])
    qapp.processEvents()

    # What the user ends up with when _copy is already taken. Set explicitly so the test
    # does not depend on how the suggestion happened to be reached.
    dup.get_param("dest_path").value = CHOSEN_DEST
    qapp.processEvents()

    out = tmp_path / "dup.usda"
    save_scene(scene, out)
    saved_text = out.read_text(encoding="utf-8")

    stage2 = Usd.Stage.Open(str(out))
    # The scene file carries the geometry as well as the graph: the source prim, and the
    # duplicate the chunk baked on the way out. Without that baked prim on the stage there
    # is nothing for the old collision check to trip over, and the bug does not reproduce.
    UsdGeom.Xform.Define(stage2, SOURCE)
    UsdGeom.Xform.Define(stage2, CHOSEN_DEST)
    scene2 = _scene_for(stage2)
    qapp.processEvents()
    return scene2, saved_text


def test_the_saved_file_holds_the_chosen_destination(reopened):
    """Pinned so a fix cannot drift to the save side, which was never wrong."""
    _scene2, saved_text = reopened

    assert CHOSEN_DEST in saved_text, "the file should record the destination the user set"


def test_the_destination_comes_back_unchanged(reopened):
    scene2, _saved = reopened

    dups = [c for c in scene2.get_chunks() if c.get_param("dest_path") is not None]
    assert dups, "the Duplicate Prim chunk did not come back at all"

    dest = dups[0].get_param("dest_path").value
    assert dest == CHOSEN_DEST, (
        f"reopening rewrote the destination: {dest!r} instead of {CHOSEN_DEST!r}. Anything "
        "pointed at the old path -- a Transform downstream, say -- now targets a prim that "
        "does not exist."
    )


def test_a_second_reopen_does_not_move_it_again(qapp, tmp_path):
    """The rename compounded: every reopen shifted the destination one more time."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, SOURCE)
    scene = _scene_for(stage)

    dup = _mk(scene, "core_usd.duplicate_prim")
    dup.get_param("source_prim").set_prims([stage.GetPrimAtPath(SOURCE)])
    qapp.processEvents()
    dup.get_param("dest_path").value = CHOSEN_DEST
    qapp.processEvents()

    seen = []
    for i in range(2):
        out = tmp_path / f"round{i}.usda"
        save_scene(scene, out)

        stage = Usd.Stage.Open(str(out))
        UsdGeom.Xform.Define(stage, SOURCE)
        UsdGeom.Xform.Define(stage, CHOSEN_DEST)
        scene = _scene_for(stage)
        qapp.processEvents()

        dup = next(c for c in scene.get_chunks() if c.get_param("dest_path") is not None)
        seen.append(dup.get_param("dest_path").value)

    assert seen == [CHOSEN_DEST, CHOSEN_DEST], f"the destination drifted across reopens: {seen}"


def test_an_empty_destination_is_still_filled_in_and_still_avoids_what_is_there(qapp):
    """The suggestion still works, and still steps around an occupied path."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, SOURCE)
    UsdGeom.Xform.Define(stage, SOURCE + "_copy")   # the obvious name is taken
    scene = _scene_for(stage)

    dup = _mk(scene, "core_usd.duplicate_prim")
    dup.get_param("source_prim").set_prims([stage.GetPrimAtPath(SOURCE)])
    qapp.processEvents()

    dest = dup.get_param("dest_path").value
    assert dest and dest != SOURCE + "_copy", (
        f"an empty destination should be filled with a free path, got {dest!r}"
    )
