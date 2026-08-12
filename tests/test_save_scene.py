"""Tests for chunk_usd.save_scene -- the "nice save flow" (Dan's request): export_chunks
plus the stage metadata (up-axis, units) it doesn't carry on its own.

Previously wrote a bundled pair (the chunk graph plus a flattened snapshot of everything
else the stage composed). Dropped once every chunk that authors real content got a
reserved execute() hook: reopening a chunk-only export now correctly regenerates
everything a chunk is responsible for by re-running it. What does NOT round-trip anymore,
by design: an RBD Solver's bake (expected to need an explicit re-run, matching Houdini's
File Cache model) and any content that never went through a chunk in the first place.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import (
    _CHUNK_PRIM_TYPENAME,
    initialize as _chunk_usd_init,
    load_chunks,
    save_scene,
)
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _build_stage_with_a_chunk() -> "tuple[Usd.Stage, SceneSender]":
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    scene = _make_scene(stage)
    mod = chunk_registry.ChunkRegistry().get_chunk_module("prim")
    chunks = mod.create() if mod.loaded else mod.load()
    c = chunks[0]
    prim_type = c.get_param("prim_type")
    prim_type.value = "Scope"
    scene.add_chunk(c)

    return stage, scene


def test_save_scene_writes_a_single_file(tmp_path) -> None:
    stage, scene = _build_stage_with_a_chunk()

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)

    assert out.exists()
    assert not (tmp_path / "my_scene_content.usda").exists(), (
        "single-file save_scene must not write a companion content file"
    )


def test_save_scene_reopen_reloads_as_a_real_chunk_scene(tmp_path) -> None:
    stage, scene = _build_stage_with_a_chunk()

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)

    reopened = Usd.Stage.Open(str(out))
    assert any(
        p.GetTypeName() == _CHUNK_PRIM_TYPENAME for p in reopened.TraverseAll()
    ), "the chunk graph itself must be there"

    # Minimal setup here (not _make_scene's chunk_usd.initialize, which would auto-load
    # the already-composed chunk prim on its own) plus one explicit load_chunks call --
    # mirrors test_export_load_roundtrip's dst side in test_chunk_usd.py, avoiding a
    # double-load of the one real chunk on the reopened stage.
    chunk_registry.ChunkLayerManager().reset()

    class _ReopenedScene(_DummyScene):
        def get_stage(self):
            return reopened

    scene2 = _ReopenedScene()
    chunk_registry.ChunkRegistry().initialize(scene2, "pytest")
    load_chunks(reopened, scene2)
    loaded = [c for c in scene2.get_chunks() if c.get_config() and c.get_config().module == "prim"]
    assert len(loaded) == 1
    assert loaded[0].get_param("prim_type").value == "Scope"


def test_save_scene_chunkgroup_file_itself_keeps_the_stage_up_axis(tmp_path) -> None:
    """Regression: reported live -- a scene authored Z-up came back Y-up after Save Scene
    and reopening. up-axis (and units) does not compose across sublayers the way ordinary
    metadata does: it resolves only from a stage's own root/session layer, falling back to
    USD's global default (Y) rather than falling through into anything underneath. export_
    chunks' own Sdf.CopySpec-based copy only ever copies prim specs, so the exported file's
    root layer has no stage-level opinion at all without save_scene stamping it separately."""
    stage, scene = _build_stage_with_a_chunk()

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)

    reopened = Usd.Stage.Open(str(out))
    assert UsdGeom.GetStageMetersPerUnit(reopened) == 0.01
    assert UsdGeom.GetStageUpAxis(reopened) == UsdGeom.Tokens.z
