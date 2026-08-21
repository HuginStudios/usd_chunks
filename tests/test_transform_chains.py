"""Several transform-ish chunks on one prim: who wins, and do the numbers stay right.

Asked directly: "what happens if you have a translate + translate + offset translate etc,
does the gizmo still do the right thing? do we still get the right values?"

The short answer, pinned below:

- Transform is ABSOLUTE. Two of them on one prim is last-one-wins, not additive, because
  each authors xformOpOrder = [xformOp:transform] and USD resolves that attribute as a
  whole -- the strongest opinion decides, there is no merging. Use Offset Transform to add.
- Offset Transform is RELATIVE. It reads the placement in effect and adds to it.
- resolve_gizmos returns handles strongest-first, because chunks_for_prim walks
  GetPrimStack(), which is strength-ordered. So a host with no chunk selected driving
  handles[0] drives the chunk whose result is actually visible.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd

transform_mod = importlib.import_module("scripts.transform")
offset_mod = importlib.import_module("scripts.offset_transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/cube")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)
    offset_mod._last_applied.clear()
    return stage, sender, scene_layer


def _mk(sender, module, name):
    chunk = module.main()
    is_transform = module is transform_mod
    chunk._config = ChunkConfig(module="transform" if is_transform else "offset_transform")
    chunk._module = module
    chunk.name = name
    sender.add_chunk(chunk)
    chunk.get_param("primpath_location" if is_transform else "prim_path").value = "/cube"
    return chunk


def _translation(stage):
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()


def _bake(stage, layer, translation):
    """A placement already on the prim as a single matrix op -- what a Transform chunk
    leaves behind, and what a baked asset export looks like.

    Into the SCENE sublayer, never the session layer: the session layer outranks every
    chunk, so a base authored there is not a base at all -- the offset computes correctly
    and is then overridden, which reads as the fix not working.
    """
    with Usd.EditContext(stage, layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*translation))
        )


# ---------------------------------------------------------------------------
# offset on top of a matrix placement
# ---------------------------------------------------------------------------

def test_an_offset_adds_to_a_matrix_placement(scene) -> None:
    """The bug found while reviewing chains. Offset Transform built its base by reading
    translate/rotate/scale ops, so a single `transform` matrix op -- what a Transform chunk
    authors, and what a baked asset carries -- was invisible to it. The base read as
    identity and the offset was applied to nothing instead of to the placement in effect.
    """
    stage, sender, scene_layer = scene
    _bake(stage, scene_layer, (0.0, 5.0, 0.0))

    offset = _mk(sender, offset_mod, "Off")
    offset.get_param("ty").value = 2.0

    assert _translation(stage) == Gf.Vec3d(0.0, 7.0, 0.0)


def test_an_offset_on_separate_ops_still_works(scene) -> None:
    """The spelling that always worked, kept so the matrix branch cannot break it."""
    stage, sender, scene_layer = scene
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTranslateOp().Set(
            Gf.Vec3d(0.0, 5.0, 0.0)
        )

    offset = _mk(sender, offset_mod, "Off")
    offset.get_param("ty").value = 2.0

    assert _translation(stage) == Gf.Vec3d(0.0, 7.0, 0.0)


def test_an_offset_scales_from_the_matrix_base(scene) -> None:
    stage, sender, scene_layer = scene
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).AddTransformOp().Set(
            Gf.Matrix4d(1.0).SetScale(Gf.Vec3d(2.0, 2.0, 2.0))
        )

    offset = _mk(sender, offset_mod, "Off")
    offset.get_param("sx").value = 3.0

    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/cube")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert matrix.GetRow3(0).GetLength() == pytest.approx(6.0), "2 x 3, not 3 and not 2"


# ---------------------------------------------------------------------------
# two Transforms on one prim
# ---------------------------------------------------------------------------

def test_two_transforms_are_last_one_wins_not_additive(scene) -> None:
    """Not a bug, and worth pinning so nobody "fixes" it into addition later.

    Transform means "set the transform". Each one authors xformOpOrder, and USD resolves
    that attribute as a whole -- the strongest opinion decides, there is no merging of op
    lists across layers. Adding is what Offset Transform is for.
    """
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")

    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    assert _translation(stage) in (Gf.Vec3d(0, 5, 0), Gf.Vec3d(0, 3, 0)), (
        "one of them wins outright; 8 would mean they had started stacking"
    )


def test_handles_come_back_strongest_first(scene) -> None:
    """What makes a host with no chunk selected still do the right thing: it drives
    handles[0], and that is the chunk whose result is actually on screen.

    chunks_for_prim walks GetPrimStack(), which USD returns strength-ordered.
    """
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")
    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))
    winner = {Gf.Vec3d(0, 5, 0): "T1", Gf.Vec3d(0, 3, 0): "T2"}[_translation(stage)]

    assert str(handles[0].chunk_prim).rsplit("/", 1)[-1] == winner


def test_dragging_moves_the_prim_by_what_was_dragged(scene) -> None:
    """End to end on a chain: whatever the ordering, driving handles[0] has to put the prim
    where the drag asked. A drag that writes to the losing chunk moves nothing on screen."""
    stage, sender, scene_layer = scene
    first = _mk(sender, transform_mod, "T1")
    second = _mk(sender, transform_mod, "T2")
    first.get_param("ty").value = 5.0
    second.get_param("ty").value = 3.0
    first.execute()
    second.execute()

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))
    chunk_gizmo.apply_bindings(
        handles[0].chunk_prim, handles[0].name,
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 9.0, 0.0)),
    )
    for c in (first, second):
        c.execute()

    assert _translation(stage) == Gf.Vec3d(0.0, 9.0, 0.0)


# ---------------------------------------------------------------------------
# mixed
# ---------------------------------------------------------------------------

def test_a_transform_and_an_offset_both_offer_handles(scene) -> None:
    stage, sender, scene_layer = scene
    _mk(sender, transform_mod, "T1")
    _mk(sender, offset_mod, "Off")

    handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath("/cube"))

    assert len(handles) == 2
    assert {d.type for d in handles} == {"transform"}


def test_neither_a_transform_nor_an_offset_triggers_auto_create(scene, monkeypatch) -> None:
    """Both own the prim's placement, so neither may get a Transform stacked on it."""
    stage, sender, scene_layer = scene
    registry = chunk_registry.ChunkRegistry()

    class _Stub:
        loaded = True
        creates = 0

        def create(self):
            _Stub.creates += 1
            c = transform_mod.main()
            c._config = ChunkConfig(module="transform")
            c._module = transform_mod
            return [c]

        load = create

    stub = _Stub()
    monkeypatch.setitem(registry.registered_modules, "transform", stub)
    monkeypatch.setattr(chunk_usd, "_create_node_pos", None, raising=False)

    _mk(sender, offset_mod, "Off")
    chunk_gizmo.ensure_transform_chunk(stage.GetPrimAtPath("/cube"))

    assert stub.creates == 0
