"""Tests for chunk_usd.save_scene -- the "nice save flow" (Dan's request): one action
that writes the editable chunk graph (export_chunks) and a flattened snapshot of what it
currently produces (_export_scene_content) as a bundled pair, the chunkgroup file
sublayering the content file so opening it alone brings both back.
"""

from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom

from usd_chunks.plugins.chunk_usd import (
    _CHUNK_PRIM_TYPENAME,
    _export_scene_content,
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


def test_export_scene_content_excludes_chunk_prims(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    out = tmp_path / "content.usda"
    _export_scene_content(stage, out)
    assert out.exists()

    content_stage = Usd.Stage.Open(str(out))
    assert content_stage.GetPrimAtPath("/apple").IsValid()
    assert not any(
        p.GetTypeName() == _CHUNK_PRIM_TYPENAME for p in content_stage.TraverseAll()
    ), "a chunk bookkeeping prim has no business in the content snapshot"


def test_export_scene_content_copies_stage_units_and_up_axis(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    out = tmp_path / "content.usda"
    _export_scene_content(stage, out)

    content_stage = Usd.Stage.Open(str(out))
    assert UsdGeom.GetStageMetersPerUnit(content_stage) == 0.01
    assert UsdGeom.GetStageUpAxis(content_stage) == UsdGeom.Tokens.z


def test_save_scene_writes_a_bundled_pair(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    chunkgroup_out = tmp_path / "my_scene.usda"
    save_scene(scene, chunkgroup_out)

    content_out = tmp_path / "my_scene_content.usda"
    assert chunkgroup_out.exists()
    assert content_out.exists()

    cg_layer = Sdf.Layer.FindOrOpen(str(chunkgroup_out))
    assert list(cg_layer.subLayerPaths) == ["my_scene_content.usda"], (
        "must be a relative identifier, not content_out's absolute path -- an absolute "
        "sublayer path breaks the moment the pair is moved or shared"
    )


def test_save_scene_reopen_composes_both_the_graph_and_the_content(tmp_path) -> None:
    stage, scene = _build_stage_with_content_and_a_chunk()

    chunkgroup_out = tmp_path / "my_scene.usda"
    save_scene(scene, chunkgroup_out)

    reopened = Usd.Stage.Open(str(chunkgroup_out))
    assert reopened.GetPrimAtPath("/apple").IsValid(), "content snapshot must compose in via the sublayer"
    assert any(
        p.GetTypeName() == _CHUNK_PRIM_TYPENAME for p in reopened.TraverseAll()
    ), "the chunk graph itself must still be there too"

    # And the whole thing still reloads as a real chunk scene, not just raw USD. Minimal
    # setup here (not _make_scene's chunk_usd.initialize, which would auto-load the
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


def test_export_scene_content_skips_children_that_only_exist_via_a_reference(tmp_path) -> None:
    """Regression: reported live -- referencing in a kitchen scene asset (e.g. via the
    Reference chunk) crashed the export with "Cannot copy unknown spec at
    </bowl_apple/food_apple_01>".

    UsdUtils.FlattenLayerStack only flattens SUBLAYER composition; a reference arc on a
    prim is preserved as-is, unresolved, in its output. A referenced asset's own children
    (food_apple_01, brought in by /bowl_apple's reference) therefore have no spec of
    their own in the flattened layer at all -- only the STAGE, which actually resolves
    the reference, reports them as real prims. TraverseAll() walks the stage, so it
    yields them; Sdf.CopySpec needs a direct spec in the layer it's copying FROM and
    raises outright when there isn't one.

    They don't need copying: /bowl_apple itself (which does have a direct spec, carrying
    the reference) is what's copied, and its own reference arc brings the same children
    back on its own once reopened -- exactly like it does live right now."""
    src = tmp_path / "kitchen_scene.usda"
    src_stage = Usd.Stage.CreateNew(str(src))
    UsdGeom.Xform.Define(src_stage, "/bowl_apple")
    child = UsdGeom.Mesh.Define(src_stage, "/bowl_apple/food_apple_01")
    child.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    src_stage.GetRootLayer().Save()

    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/bowl_apple", "Xform")
    prim.GetReferences().AddReference(str(src), "/bowl_apple")

    out = tmp_path / "content.usda"
    _export_scene_content(stage, out)  # must not raise

    content_stage = Usd.Stage.Open(str(out))
    reloaded = content_stage.GetPrimAtPath("/bowl_apple")
    assert reloaded.IsValid()
    assert reloaded.HasAuthoredReferences()
    # The reference itself is what brings this back -- resolves through /bowl_apple's
    # copied-across reference arc, not from anything copied directly at this path.
    assert content_stage.GetPrimAtPath("/bowl_apple/food_apple_01").IsValid()


def test_export_scene_content_includes_muted_layers(tmp_path) -> None:
    """Regression: reported live -- an RBD Solver's baked simulation went missing from a
    saved scene. A chunk not reachable from the active view's cook set has its own layer
    muted (recook_for_view); a muted layer's content isn't composed at all, so
    FlattenLayerStack drops it outright rather than just hiding it. The snapshot must
    capture what every chunk actually produced, not only whatever the active view
    happens to currently show."""
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/baked_thing", "Xform")
    layer = Sdf.Layer.CreateAnonymous("baked_data")
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    with Usd.EditContext(stage, layer):
        prim.CreateAttribute("myBakedValue", Sdf.ValueTypeNames.Float, custom=True).Set(42.0)

    stage.MuteLayer(layer.identifier)
    assert prim.GetAttribute("myBakedValue").Get() is None, "sanity: muting must actually hide it live"

    out = tmp_path / "content.usda"
    _export_scene_content(stage, out)

    content_stage = Usd.Stage.Open(str(out))
    assert content_stage.GetPrimAtPath("/baked_thing").GetAttribute("myBakedValue").Get() == 42.0

    # The live stage's own mute state must be restored, not left permanently unmuted.
    assert stage.IsLayerMuted(layer.identifier)
