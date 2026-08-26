"""Zeroing the placement a prim already carries.

A Transform composes on top of whatever the prim already has -- its own baked ops, and any
Transform before it -- which is what lets two of them stack. The cost is that its numbers
are relative to the prim's rest pose rather than absolute.

Reported live, and it is the ordinary case rather than an edge one: the bowl asset carries
its own -90 X, so typing 50 gave -40 in world and the bowl tilted the opposite way to what
was asked for. Physics and the viewport agreed with each other throughout -- it was the
numbers that misled.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd

transform_mod = importlib.import_module("scripts.transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def asset():
    """A prim carrying a baked -90, as every exported asset in the test scene does."""
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        bowl = UsdGeom.Xform.Define(stage, "/bowl")
        UsdGeom.Xformable(bowl).AddRotateXYZOp().Set(Gf.Vec3f(-90.0, 0.0, 0.0))
        UsdGeom.Xformable(bowl).AddTranslateOp().Set(Gf.Vec3d(4.0, 0.0, 0.0))

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)

    node = transform_mod.main()
    node._config = ChunkConfig(module="transform")
    node._module = transform_mod
    sender.add_chunk(node)
    node.get_param("primpath_location").value = "/bowl"
    node.execute()
    return stage, node


def _local(stage):
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/bowl")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    return matrix[0] if isinstance(matrix, tuple) else matrix


def _press_zero(node):
    transform_mod.zero_incoming(node.get_param("zero_incoming"), None, None)


def test_the_button_exists(asset) -> None:
    _stage, node = asset
    assert node.get_param("zero_incoming") is not None


def test_zeroing_puts_the_prim_at_identity(asset) -> None:
    _stage, node = asset
    stage = _stage
    assert _local(stage) != Gf.Matrix4d(1.0), "precondition: the asset is placed"

    _press_zero(node)
    node.execute()

    assert Gf.IsClose(_local(stage), Gf.Matrix4d(1.0), 1e-5)


def test_typing_after_zeroing_reads_as_written(asset) -> None:
    """The whole point, and the thing the first version of this test got wrong.

    It read the slider and ADDED 50, which is not what anyone does -- you type 50 into the
    box. The first implementation put the cancellation in the visible sliders, so typing
    over them threw it away and the result was identical to not having pressed the button.
    The test passed because it encoded the workaround instead of the behaviour.
    """
    stage, node = asset
    _press_zero(node)
    node.execute()
    assert node.get_param("rx").value == 0.0, "the sliders must come back to rest"

    node.get_param("rx").value = 50.0        # typed, not added
    node.execute()

    assert chunk_gizmo.decompose(_local(stage))["rotate"][0] == pytest.approx(50.0, abs=1e-4)


def test_typing_a_translation_after_zeroing_reads_as_written(asset) -> None:
    stage, node = asset
    _press_zero(node)
    node.execute()

    node.get_param("tx").value = 3.0
    node.execute()

    assert Gf.IsClose(_local(stage).ExtractTranslation(), Gf.Vec3d(3.0, 0.0, 0.0), 1e-4)


def test_the_sliders_read_at_rest_after_zeroing(asset) -> None:
    """The visible values have to look zeroed, or the number in the box still lies."""
    _stage, node = asset
    _press_zero(node)

    assert [node.get_param(n).value for n in ("tx", "ty", "tz")] == [0.0, 0.0, 0.0]
    assert [node.get_param(n).value for n in ("rx", "ry", "rz")] == [0.0, 0.0, 0.0]
    assert [node.get_param(n).value for n in ("sx", "sy", "sz")] == [1.0, 1.0, 1.0]


def test_zeroing_is_idempotent(asset) -> None:
    """Pressing it twice must not fight itself -- the second press sees a prim already at
    identity, so it has nothing to cancel."""
    stage, node = asset
    _press_zero(node)
    node.execute()
    _press_zero(node)
    node.execute()

    assert Gf.IsClose(_local(stage), Gf.Matrix4d(1.0), 1e-5)


def test_zeroing_leaves_the_asset_untouched(asset) -> None:
    """It cancels the placement, it does not erase it -- deleting the chunk brings it back."""
    stage, node = asset
    before = _local(stage)

    _press_zero(node)
    node.execute()
    node.request_delete()

    assert Gf.IsClose(_local(stage), before, 1e-5)


def test_zeroing_with_no_target_does_nothing(asset) -> None:
    _stage, node = asset
    node.get_param("primpath_location").value = ""

    _press_zero(node)   # must not raise
