"""A set-down goal closes the loop on a carried box: while it is held, RBD Solver shifts the arm's motors so the box
will land on the goal.

A goal is authored on the robot (``setDown:*``): the goal point, the box, the hand link holding it, the joints to
correct, the frames to correct over and a gain. Each of those frames the solver predicts where the box lands -- where it
sits in the hand now, carried to the arm's pose at release -- and moves the joints a share of the miss. Here a pan arm
holds a box 20 cm out while its drive keeps 0 deg; the goal is 8 deg round from there.
"""
from __future__ import annotations

import importlib.util
import math

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import Scene

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")

FRAMES = 36
GOAL = (0.2 * math.cos(math.radians(8)), 0.2 * math.sin(math.radians(8)), 0.3)     # 28 mm round from the box


def _link(stage, path, at, size):
    prim = UsdGeom.Xform.Define(stage, path)
    prim.AddTranslateOp().Set(Gf.Vec3d(*at))
    UsdPhysics.RigidBodyAPI.Apply(prim.GetPrim())
    UsdPhysics.MassAPI.Apply(prim.GetPrim()).CreateMassAttr(0.1)
    shape = UsdGeom.Cube.Define(stage, path + "/collider")
    shape.CreateSizeAttr(1.0)
    shape.AddScaleOp().Set(Gf.Vec3f(*size))
    UsdPhysics.CollisionAPI.Apply(shape.GetPrim())
    return prim.GetPrim()


def _arm(stage):
    """Z up, metres. A base bolted at 30 cm, an arm on a pan joint about Z (its drive keyed to hold 0 deg), a box fixed
    at its end, 20 cm out along X."""
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/rig")
    base = _link(stage, "/rig/base", (0.0, 0.0, 0.3), (0.04, 0.04, 0.04))
    arm = _link(stage, "/rig/arm", (0.1, 0.0, 0.3), (0.14, 0.02, 0.02))
    box = _link(stage, "/rig/box", (0.2, 0.0, 0.3), (0.03, 0.03, 0.03))
    UsdPhysics.FixedJoint.Define(stage, "/rig/base/anchor").CreateBody1Rel().SetTargets([base.GetPath()])
    pan = UsdPhysics.RevoluteJoint.Define(stage, "/rig/arm/pan")
    pan.CreateBody0Rel().SetTargets([base.GetPath()])
    pan.CreateBody1Rel().SetTargets([arm.GetPath()])
    pan.CreateAxisAttr("Z")
    pan.CreateLocalPos1Attr(Gf.Vec3f(-0.1, 0.0, 0.0))
    drive = UsdPhysics.DriveAPI.Apply(pan.GetPrim(), "angular")
    drive.CreateStiffnessAttr(5.0)
    drive.CreateDampingAttr(0.1)
    target = drive.CreateTargetPositionAttr()
    target.Set(0.0)
    target.Set(0.0, Usd.TimeCode(1))
    pan.GetPrim().CreateAttribute("physxJoint:armature", Sdf.ValueTypeNames.Float).Set(0.01)
    grip = UsdPhysics.FixedJoint.Define(stage, "/rig/box/grip")
    grip.CreateBody0Rel().SetTargets([arm.GetPath()])
    grip.CreateBody1Rel().SetTargets([box.GetPath()])
    grip.CreateLocalPos0Attr(Gf.Vec3f(0.1, 0.0, 0.0))


def _goal(stage):
    """Correct frames 1-24: while the body is held."""
    rig = stage.GetPrimAtPath("/rig")
    rig.CreateAttribute("setDown:goal", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(*GOAL))
    rig.CreateRelationship("setDown:box").SetTargets([Sdf.Path("/rig/box")])
    rig.CreateRelationship("setDown:hand").SetTargets([Sdf.Path("/rig/arm")])
    rig.CreateRelationship("setDown:joints").SetTargets([Sdf.Path("/rig/arm/pan")])
    rig.CreateAttribute("setDown:from", Sdf.ValueTypeNames.Int).Set(1)
    rig.CreateAttribute("setDown:until", Sdf.ValueTypeNames.Int).Set(24)
    rig.CreateAttribute("setDown:gain", Sdf.ValueTypeNames.Float).Set(0.3)


def _box_at(stage, frame):
    return UsdGeom.XformCache(Usd.TimeCode(frame)).GetLocalToWorldTransform(
        stage.GetPrimAtPath("/rig/box")).ExtractTranslation()


def _solve(with_goal: bool):
    scene = Scene.new()
    stage = scene.get_stage()
    with Usd.EditContext(stage, stage.GetRootLayer()):
        _arm(stage)
        if with_goal:
            _goal(stage)
    rbd = scene.create.rbd_solver(prim_path="/rig", frame_count=FRAMES, start_frame=1, substeps=8, gravity=0.0)
    scene.set_output(rbd)
    return scene


def test_a_set_down_goal_brings_the_held_box_over_it_and_keeps_it_there():
    with _solve(with_goal=True) as scene:
        stage = scene.get_stage()
        assert abs(_box_at(stage, 1)[1]) < 0.01                                  # starts where the drive holds it
        end = _box_at(stage, FRAMES)
        assert math.hypot(end[0] - GOAL[0], end[1] - GOAL[1]) < 0.003, end      # landed on it, kept after frame 24


def test_without_a_goal_the_arm_holds_its_drives_target():
    with _solve(with_goal=False) as scene:
        assert abs(_box_at(scene.get_stage(), FRAMES)[1]) < 0.002
