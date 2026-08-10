"""Tests for chunk_usd.save_scene -- the "nice save flow": close today, reopen tomorrow
exactly as it was. One UsdUtils.FlattenLayerStack export of the whole composed stage,
chunk prims and content together -- no hand-rolled selective copying, which is what an
earlier version of this did and which kept turning up new composition-arc edge cases
(instance proxies, then a referenced asset's own children having no direct spec in a
hand-copied layer) one at a time.
"""

from __future__ import annotations

from pxr import Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import (
    _CHUNK_PRIM_TYPENAME,
    initialize as _chunk_usd_init,
    load_chunks,
    save_scene,
)
from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender


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


def _build_stage_with_content_and_a_chunk() -> "tuple[Usd.Stage, SceneSender]":
    """A mesh (real scene content, unrelated to any chunk) plus one chunk prim."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])

    scene = _make_scene(stage)
    c = chunk.Chunk("testChunk")
    c._config = ChunkConfig(module="prim")
    prim_type = chunk.Menu("prim_type", menu_items=["Xform", "Scope", "Class"])
    prim_type.value = "Scope"
    c.add_param(prim_type)
    scene.add_chunk(c)

    return stage, scene


def test_save_scene_writes_one_file_with_both_chunks_and_content(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)
    assert out.exists()

    reopened = Usd.Stage.Open(str(out))
    assert reopened.GetPrimAtPath("/apple").IsValid()
    assert any(p.GetTypeName() == _CHUNK_PRIM_TYPENAME for p in reopened.TraverseAll())


def test_save_scene_preserves_stage_units_and_up_axis(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)

    reopened = Usd.Stage.Open(str(out))
    assert UsdGeom.GetStageMetersPerUnit(reopened) == 0.01
    assert UsdGeom.GetStageUpAxis(reopened) == UsdGeom.Tokens.z


def test_save_scene_reopen_reloads_as_a_real_chunk_scene(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)

    reopened = Usd.Stage.Open(str(out))

    # Minimal setup (not _make_scene's chunk_usd.initialize, which would auto-load the
    # already-composed chunk prim on its own) plus one explicit load_chunks call --
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


def test_save_scene_handles_referenced_content(tmp_path) -> None:
    """Regression: reported live -- referencing in an external asset (e.g. via the
    Reference chunk) and running Save Scene crashed with "Cannot copy unknown spec at
    </bowl_apple/food_apple_01>" under the earlier hand-rolled-copy implementation.
    UsdUtils.FlattenLayerStack resolves the whole composition (references included)
    correctly as a single native operation, so this needs no special-casing at all."""
    src = tmp_path / "kitchen_scene.usda"
    src_stage = Usd.Stage.CreateNew(str(src))
    UsdGeom.Xform.Define(src_stage, "/bowl_apple")
    child = UsdGeom.Mesh.Define(src_stage, "/bowl_apple/food_apple_01")
    child.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    src_stage.GetRootLayer().Save()

    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/bowl_apple", "Xform")
    prim.GetReferences().AddReference(str(src), "/bowl_apple")
    scene = _make_scene(stage)

    out = tmp_path / "my_scene.usda"
    save_scene(scene, out)  # must not raise

    reopened = Usd.Stage.Open(str(out))
    assert reopened.GetPrimAtPath("/bowl_apple/food_apple_01").IsValid()
