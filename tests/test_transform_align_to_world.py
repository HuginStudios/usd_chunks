"""Align To World: cancel the rotation a prim inherits, so typed angles mean world angles.

A Transform composes on top of what the prim already carries, so on an asset with a baked
-90 a typed 50 comes out at -40 -- tilting the opposite way to what was asked for. Measured
end to end against a real MuJoCo run: the apple rolled off the far side of the bowl,
correctly, down a slope facing the wrong way.

ROTATION ONLY, which is the lesson from three failed attempts. Scale and translation are
inherited on purpose; cancelling those too shrank a bowl at 140x scale to a speck.
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
    """Shaped like the real one: the rotation AND the scale live on an ancestor, and the
    prim the chunk targets carries no ops of its own.

    Both details matter. Put the rotation on the target and a local-only implementation
    passes; leave the scale off and cancelling everything looks correct."""
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        plate = UsdGeom.Xform.Define(stage, "/plate")
        UsdGeom.Xformable(plate).AddRotateXYZOp().Set(Gf.Vec3f(-90.0, 0.0, 0.0))
        UsdGeom.Xformable(plate).AddScaleOp().Set(Gf.Vec3f(140.0, 140.0, 140.0))
        UsdGeom.Xform.Define(stage, "/plate/bowl")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)

    node = transform_mod.main()
    node._config = ChunkConfig(module="transform")
    node._module = transform_mod
    sender.add_chunk(node)
    node.get_param("primpath_location").value = "/plate/bowl"
    node.execute()
    return stage, node


def _world(stage):
    return UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(
        stage.GetPrimAtPath("/plate/bowl")
    )


def _world_rot_x(stage):
    rot = _world(stage).RemoveScaleShear().ExtractRotation()
    return rot.GetAngle() * (1.0 if rot.GetAxis()[0] >= 0 else -1.0)


def _world_scale(stage):
    return _world(stage).GetRow3(0).GetLength()


def _press(node):
    transform_mod.align_to_world(node.get_param("align_to_world"), None, None)


def test_without_pressing_it_a_typed_angle_is_relative(asset) -> None:
    """The behaviour being corrected, asserted so the reason for the button is on record."""
    stage, node = asset
    node.get_param("rx").value = 50.0
    node.execute()

    assert _world_rot_x(stage) == pytest.approx(-40.0, abs=1e-3)


def test_a_typed_angle_reads_as_world_after_pressing(asset) -> None:
    stage, node = asset
    _press(node)
    node.get_param("rx").value = 50.0
    node.execute()

    assert _world_rot_x(stage) == pytest.approx(50.0, abs=1e-3)


def test_the_scale_is_left_alone(asset) -> None:
    """Cancelling the ancestors wholesale took their 140x with it and shrank the bowl to a
    speck. Scale is inherited on purpose."""
    stage, node = asset
    before = _world_scale(stage)
    _press(node)
    node.execute()

    assert _world_scale(stage) == pytest.approx(before)
    assert _world_scale(stage) == pytest.approx(140.0)


def test_pressing_twice_changes_nothing(asset) -> None:
    stage, node = asset
    _press(node)
    node.execute()
    once = _world_rot_x(stage)

    _press(node)
    node.execute()

    assert _world_rot_x(stage) == pytest.approx(once, abs=1e-3)


def test_the_visible_sliders_are_untouched(asset) -> None:
    """The offset goes somewhere hidden, so typing over a slider cannot throw it away --
    which is exactly what made the first version useless."""
    _stage, node = asset
    node.get_param("rx").value = 12.0

    _press(node)

    assert node.get_param("rx").value == 12.0


def test_it_does_nothing_without_a_target(asset) -> None:
    _stage, node = asset
    node.get_param("primpath_location").value = ""

    _press(node)   # must not raise
