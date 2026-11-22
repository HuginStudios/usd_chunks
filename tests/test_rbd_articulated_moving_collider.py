"""An animated collider moves in a solve that has a robot in it, as it does without one.

A paddle (an animated collider) sweeps across the table and shoves a box ahead of it. With a robot in the same
RBD Solver -- the articulated solve -- the paddle's animation was dropped: its kinematic body was built where it
started and never moved, so the box stayed put while the paddle in the viewport swept through it.

(A kinematic collider is re-posed every frame, not driven: it carries no velocity, so a box resting on a
moving one is not dragged along by friction. A conveyor belt is a driven joint, not an animated collider.)
"""
from __future__ import annotations

import importlib.util

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import Scene

from test_rbd_grasp import FRAMES, _gripper_scene

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")


def _paddle(stage):
    """A 1 x 10 x 4 cm paddle sweeping along +X from just behind the box (its sides at x = +-0.02) to x = 0.10."""
    paddle = UsdGeom.Cube.Define(stage, "/paddle")
    paddle.CreateSizeAttr(1.0)
    move = paddle.AddTranslateOp()
    move.Set(Gf.Vec3d(-0.04, 0.0, 0.02), Usd.TimeCode(1))
    move.Set(Gf.Vec3d(0.10, 0.0, 0.02), Usd.TimeCode(FRAMES))
    paddle.AddScaleOp().Set(Gf.Vec3f(0.01, 0.1, 0.04))


@pytest.mark.parametrize("bodies", ["/box", "/rig /box"], ids=["alone", "with a robot"])
def test_an_animated_paddle_shoves_the_box(bodies) -> None:
    with Scene.new() as scene:
        stage = scene.get_stage()
        _gripper_scene(stage, rigs=(("/rig", 0.4),))                 # the robot well clear of the paddle
        _paddle(stage)
        rbd = scene.create.rbd_solver(prim_path=bodies, collision_path="/table\n/paddle",
                                      frame_count=FRAMES, start_frame=1, friction=1.0, substeps=8)
        rbd.get_param("run_sim").click()
        end = UsdGeom.XformCache(Usd.TimeCode(FRAMES)).GetLocalToWorldTransform(stage.GetPrimAtPath("/box")).ExtractTranslation()
        # the paddle ends at 0.10, so the box's middle ends near 0.10 + 0.005 + 0.02
        assert end[0] > 0.10, f"the box stayed at x {end[0]:.3f}: the paddle did not move"
