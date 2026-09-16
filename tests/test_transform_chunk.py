from __future__ import annotations

from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    _chunk_usd_init(stage, scene)
    return scene


def _make_transform(scene: SceneSender, prim_path: str = "/prim"):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("transform")
    chunks = mod.create() if mod.loaded else mod.load()
    t = chunks[0]
    scene.add_chunk(t)
    t.get_param("primpath_location").value = prim_path
    return t


def test_translate_range_defaults_to_the_original_span_at_centimeter_scale() -> None:
    """Regression baseline: metersPerUnit=0.01 is USD's own default when unauthored, and is
    what scripts/transforms/transform.py's original flat -1000..1000 constant was implicitly tuned for
    (10 meters-equivalent / 0.01 == 1000) -- confirms the unit-aware rescale doesn't change
    behavior for the common case."""
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/prim", "Xform")
    scene = _make_scene(stage)
    t = _make_transform(scene)
    t.execute()

    assert t.get_param("tx").min_value == -1000.0
    assert t.get_param("tx").max_value == 1000.0


def test_translate_range_shrinks_to_match_a_meters_scale_stage() -> None:
    """Regression: reported live -- a meters-scale stage (metersPerUnit=1.0) kept the same
    +-1000 translate range as a centimeters-scale one, so a small nudge (or typing "5")
    could move a several-units-across object many times its own size ("flies away") and
    made fine adjustment practically impossible. The range must scale with the stage's own
    declared unit so a given slider position means a consistent real-world distance."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.DefinePrim("/prim", "Xform")
    scene = _make_scene(stage)
    t = _make_transform(scene)
    t.execute()

    assert t.get_param("tx").min_value == -10.0
    assert t.get_param("tx").max_value == 10.0


def test_translate_still_authors_the_correct_offset_at_meters_scale() -> None:
    """The rescale only touches the slider's min/max -- the authored xformOp:transform must
    still reflect the raw value set, unclamped and untouched by the new range."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.DefinePrim("/prim", "Xform")
    scene = _make_scene(stage)
    t = _make_transform(scene)
    t.execute()

    t.get_param("tx").value = 5.0
    t.execute()

    xformable = UsdGeom.Xformable(stage.GetPrimAtPath("/prim"))
    translation = xformable.GetLocalTransformation().ExtractTranslation()
    assert translation == Gf.Vec3d(5.0, 0.0, 0.0)
