"""A disabled chunk survives a save and reopen.

Reported: disable a chunk, save, reopen, and it is gone.

The save was never at fault -- the file holds the chunk, with active=false recording
that it was disabled. It is lost on the way back in: a disabled chunk is authored as an
inactive prim, and Usd.Stage.Traverse()'s default predicate skips inactive prims, so
load_chunks never saw it.

The drag-and-drop importer already hit this and traverses with an inactive-inclusive
predicate for exactly this reason (_snapshot_layer_chunks). load_chunks had not.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_stage import (
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
    save_scene,
)


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


def _mk(scene, key="transform"):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


@pytest.fixture
def reopened(qapp, tmp_path):
    """One enabled and one disabled chunk, saved and reopened."""
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)

    kept = _mk(scene)
    kept.get_param("tx").value = 3.0
    off = _mk(scene)
    off.get_param("tx").value = 9.0
    qapp.processEvents()
    off.disable()
    qapp.processEvents()

    out = tmp_path / "disabled.usda"
    save_scene(scene, out)

    saved_stage = Usd.Stage.Open(str(out))   # held: a temporary is collected mid-traverse
    saved_paths = [str(p.GetPath()) for p in saved_stage.TraverseAll()]

    stage2 = Usd.Stage.Open(str(out))
    scene2 = _scene_for(stage2)
    qapp.processEvents()
    return scene2, stage2, saved_paths


def test_the_saved_file_holds_the_disabled_chunk(reopened):
    """Pinned so a fix cannot drift to the save side, which was never wrong."""
    _scene2, _stage2, saved_paths = reopened

    chunk_paths = [p for p in saved_paths if p.count("/") >= 3]
    assert len(chunk_paths) >= 2, f"both chunks should be in the file: {saved_paths}"


def test_a_disabled_chunk_comes_back(reopened):
    scene2, _stage2, _saved = reopened

    names = sorted(c.name for c in scene2.get_chunks())
    assert len(names) == 2, f"the disabled chunk was dropped on load: {names}"


def test_it_comes_back_still_disabled(reopened):
    """Restoring it is only half the job -- it has to come back off, not on."""
    scene2, _stage2, _saved = reopened

    disabled = [c for c in scene2.get_chunks() if c.has_state(ChunkFlags.State.DISABLED)]
    assert len(disabled) == 1, (
        f"expected exactly one disabled chunk, got {[c.name for c in disabled]}"
    )
    assert disabled[0].get_param("tx").value == 9.0, "and with its values intact"


def test_its_prim_is_still_inactive(reopened):
    scene2, stage2, _saved = reopened

    disabled = [c for c in scene2.get_chunks() if c.has_state(ChunkFlags.State.DISABLED)]
    assert disabled, "nothing came back disabled"
    prim = stage2.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(disabled[0])))
    assert prim and prim.IsValid()
    assert not prim.IsActive(), "a disabled chunk's prim must stay inactive"


def test_the_enabled_chunk_is_unaffected(reopened):
    scene2, _stage2, _saved = reopened

    enabled = [c for c in scene2.get_chunks() if not c.has_state(ChunkFlags.State.DISABLED)]
    assert len(enabled) == 1
    assert enabled[0].get_param("tx").value == 3.0
