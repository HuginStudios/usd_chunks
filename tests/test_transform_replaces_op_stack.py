"""A Transform chunk sets a prim's transform; it does not stack onto the prim's own ops.

Reported live: dragging a parent prim (`/food_apple_01_4k`, not its mesh) went haywire --
"as soon as I start to drag the y axis, it applies a -90 to the rotate x and applies 100 on
scale x,y and z".

Those are the asset's real values. An exported asset carries separate translate + rotateXYZ
+ scale ops, AddTransformOp appends, so the composed order became

    [translate, rotateXYZ, scale, transform]

and the chunk's matrix was applied ON TOP of the prim's own placement instead of instead of
it. A scale of 100 composed to 10000.

Invisible on a bare cube, which has no ops at all -- which is why every earlier test passed.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage

transform_mod = importlib.import_module("chunks.transforms.transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _StubModule:
    loaded = True
    source_hash = None
    # Registered under "transform", so this is where the chunks it creates find their hooks
    # (ChunkBase.get_chunk_module): the real transform module's.
    module = transform_mod

    def create(self):
        c = transform_mod.main()
        c._config = ChunkConfig(module="transform")
        return [c]

    load = create


@pytest.fixture
def asset(monkeypatch):
    """A prim shaped like a real exported asset: three separate ops, not one matrix."""
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        xform = UsdGeom.Xform.Define(stage, "/food_apple")
        xform.AddTranslateOp().Set(Gf.Vec3d(2.0, 0.0, 0.0))
        xform.AddRotateXYZOp().Set(Gf.Vec3f(-90.0, 0.0, 0.0))
        xform.AddScaleOp().Set(Gf.Vec3f(100.0, 100.0, 100.0))
        UsdGeom.Cube.Define(stage, "/food_apple/mesh")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)

    monkeypatch.setitem(registry.registered_modules, "transform", _StubModule())
    monkeypatch.setattr(chunk_stage, "_params_tab_create", False, raising=False)
    monkeypatch.setattr(chunk_stage, "_loft_new_chunks", None, raising=False)
    monkeypatch.setattr(chunk_stage, "_create_node_pos", None, raising=False)
    return stage, scene_layer


def _prim(stage):
    return stage.GetPrimAtPath("/food_apple")


def _local(stage):
    matrix = UsdGeom.Xformable(_prim(stage)).GetLocalTransformation(Usd.TimeCode.Default())
    return matrix[0] if isinstance(matrix, tuple) else matrix


def _scale_of(matrix):
    return matrix.GetRow3(0).GetLength()


def test_auto_creating_on_an_asset_does_not_move_it(asset) -> None:
    """The headline bug. Seed then cook has to be a no-op, whatever ops the prim had."""
    stage, _layer = asset
    before = _local(stage)

    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()

    after = _local(stage)
    assert _scale_of(after) == pytest.approx(_scale_of(before)), "scale was applied twice"
    assert Gf.IsClose(after.ExtractTranslation(), before.ExtractTranslation(), 1e-6)


def test_the_chunk_appends_its_own_op(asset) -> None:
    """The asset's ops stay, in order, and this chunk's op goes on the end.

    This used to replace the stack outright, which is why a new chunk had to be seeded with
    the prim's current placement. Appending composes instead, so nothing needs seeding.
    """
    stage, _layer = asset
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    created.execute()

    order = list(UsdGeom.Xformable(_prim(stage)).GetXformOpOrderAttr().Get() or [])

    assert order[:3] == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
    assert order[3] == chunk_utils.xform_op_name(created)
    assert len(order) == 4


def test_the_assets_own_ops_are_left_alone(asset) -> None:
    """The order is overridden in the chunk's layer, never in the asset's."""
    stage, scene_layer = asset
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()

    spec = scene_layer.GetPrimAtPath(Sdf.Path("/food_apple"))
    authored = list(spec.properties["xformOpOrder"].default)

    assert authored == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]


def test_deleting_the_chunk_gives_the_asset_back(asset) -> None:
    stage, _layer = asset
    before = _local(stage)
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    created.execute()

    created.request_delete()

    after = _local(stage)
    assert _scale_of(after) == pytest.approx(_scale_of(before))
    assert list(UsdGeom.Xformable(_prim(stage)).GetXformOpOrderAttr().Get()) == [
        "xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale",
    ], "the chunk's op should have gone with it, leaving the asset's own"


def test_dragging_moves_it_by_exactly_what_was_dragged(asset) -> None:
    """Absolute, and once. Stacked, a drag to y=5 landed somewhere else entirely."""
    stage, _layer = asset
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)

    matrix = Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 5.0, 0.0))
    chunk_gizmo.apply_bindings(desc.chunk_prim, desc.name, matrix)
    created.execute()

    assert Gf.IsClose(_local(stage).ExtractTranslation(), Gf.Vec3d(0.0, 5.0, 0.0), 1e-6)


def test_a_prim_with_no_ops_still_works(asset) -> None:
    """The case that always passed, kept so the fix cannot regress it."""
    stage, _layer = asset
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Cube.Define(stage, "/plain")
    plain = stage.GetPrimAtPath("/plain")

    desc = chunk_gizmo.ensure_transform_chunk(plain)
    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(1.0, 2.0, 3.0))
    )
    chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()

    matrix = UsdGeom.Xformable(plain).GetLocalTransformation(Usd.TimeCode.Default())
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    assert Gf.IsClose(matrix.ExtractTranslation(), Gf.Vec3d(1.0, 2.0, 3.0), 1e-6)


# ---------------------------------------------------------------------------
# leftover state from deleting and re-adding chunks
# ---------------------------------------------------------------------------

def _dangling_order(stage, layer, path="/food_apple"):
    """xformOpOrder naming an op that no layer defines with a type.

    Ordinary leftover state, not a contrived one: the order and the attribute are authored
    into different layers and go away independently, so deleting and re-adding transform
    chunks reaches it. Reported live from exactly that sequence.
    """
    with Usd.EditContext(stage, layer):
        stage.GetPrimAtPath(Sdf.Path(path)).CreateAttribute(
            "xformOpOrder", Sdf.ValueTypeNames.TokenArray, custom=False
        ).Set(["xformOp:transform"])


def test_a_dangling_op_order_does_not_crash_the_cook(asset) -> None:
    """Reported live, mid-drag, once per mouse-move:

        Error in UsdStage::_GetAttributeValueType: 'Empty typeName for
        </food_apple_01_4k.xformOp:transform>'

    The two USD versions in use fail differently and both fail: 25.05 hands back an untyped
    op so Set() raises the above, while 25.08 skips the op entirely, so AddTransformOp()
    raises instead because the name IS already in xformOpOrder. Authoring the attribute
    outright sidesteps both.
    """
    stage, scene_layer = asset
    _dangling_order(stage, scene_layer)

    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, 4.0, 0.0))
    )
    chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()

    assert Gf.IsClose(_local(stage).ExtractTranslation(), Gf.Vec3d(0.0, 4.0, 0.0), 1e-6)


def test_the_attribute_gets_its_type_from_the_chunks_own_layer(asset) -> None:
    """What makes the composed op well-formed again whatever another layer left behind."""
    stage, scene_layer = asset
    _dangling_order(stage, scene_layer)
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    created.execute()

    op_name = chunk_utils.xform_op_name(created)
    assert _prim(stage).GetAttribute(op_name).GetTypeName() == Sdf.ValueTypeNames.Matrix4d
    spec = created.get_edit_context().GetAttributeAtPath(Sdf.Path(f"/food_apple.{op_name}"))
    assert spec is not None and spec.typeName == Sdf.ValueTypeNames.Matrix4d
