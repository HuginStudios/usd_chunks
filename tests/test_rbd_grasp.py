"""A mechanism and the box it closes on share one RBD solve, and friction alone holds the box.

A carriage on a vertical slide with two fingers that slide together: closed on a box and
lifted, the box rises with them -- no weld, no attach, only the fingers' squeeze. Before, an
articulated asset had a solve of its own and the box another: the fingers passed through it.
"""
from __future__ import annotations

import importlib.util

import pytest
from pxr import Gf, Usd, UsdGeom, UsdPhysics

from usd_chunks import Scene

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")

FRAMES = 80


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


def _slide(stage, path, parent, child, axis, keys, stiffness, max_force):
    joint = UsdPhysics.PrismaticJoint.Define(stage, path)
    if parent is not None:
        joint.CreateBody0Rel().SetTargets([parent.GetPath()])
    joint.CreateBody1Rel().SetTargets([child.GetPath()])
    joint.CreateAxisAttr(axis)
    joint.CreateLowerLimitAttr(-0.2)
    joint.CreateUpperLimitAttr(0.2)
    drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "linear")
    drive.CreateStiffnessAttr(stiffness)
    drive.CreateDampingAttr(0.1 * stiffness)
    drive.CreateMaxForceAttr(max_force)
    target = drive.CreateTargetPositionAttr()
    target.Set(0.0)
    for frame, value in keys:
        target.Set(value, Usd.TimeCode(frame))


def _gripper_scene(stage, rigs=(("/rig", 0.0),), box_x=0.0):
    """Z up, metres. The box's sides are at x = +-0.02; the open fingers' inner faces at +-0.03.

    One rig per (root, x) in `rigs`, every one alike -- the same link names -- and the box
    under the one at box_x."""
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    table = UsdGeom.Cube.Define(stage, "/table")
    table.CreateSizeAttr(0.5)
    table.AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.25))
    box = UsdGeom.Cube.Define(stage, "/box")
    box.CreateSizeAttr(0.04)
    box.AddTranslateOp().Set(Gf.Vec3d(box_x, 0, 0.02))
    for root, x in rigs:
        _rig(stage, root, x)


def _rig(stage, root, x):
    """A carriage on a slide with two fingers, at x."""

    UsdGeom.Xform.Define(stage, root)
    base = _link(stage, f"{root}/base", (x, 0, 0.50), (0.05, 0.05, 0.02))  # clear of the lifted carriage
    carriage = _link(stage, f"{root}/carriage", (x, 0, 0.20), (0.10, 0.03, 0.02))
    left = _link(stage, f"{root}/left", (x - 0.035, 0, 0.05), (0.01, 0.04, 0.08))
    right = _link(stage, f"{root}/right", (x + 0.035, 0, 0.05), (0.01, 0.04, 0.08))
    anchor = UsdPhysics.FixedJoint.Define(stage, f"{root}/base/anchor")
    anchor.CreateBody1Rel().SetTargets([base.GetPath()])
    # Lift 10 cm after the fingers have closed; the fingers close 2 cm each, past the box's
    # sides, so their motors keep squeezing at their force limit.
    _slide(stage, f"{root}/carriage/lift", base, carriage, "Z",
           [(1, 0.0), (30, 0.0), (60, 0.10), (FRAMES, 0.10)], stiffness=2000.0, max_force=200.0)
    for name, link, sign in (("left", left, 1.0), ("right", right, -1.0)):
        _slide(stage, f"{root}/{name}/slide", carriage, link, "X",
               [(1, 0.0), (10, 0.0), (20, 0.02 * sign), (FRAMES, 0.02 * sign)],
               stiffness=500.0, max_force=5.0)


def _box_at(stage, frame):
    cache = UsdGeom.XformCache(Usd.TimeCode(frame))
    return cache.GetLocalToWorldTransform(stage.GetPrimAtPath("/box")).ExtractTranslation()


def test_closed_fingers_lift_the_box_by_friction() -> None:
    with Scene.new() as scene:
        stage = scene.get_stage()
        with Usd.EditContext(stage, stage.GetRootLayer()):
            _gripper_scene(stage)
        rbd = scene.create.rbd_solver(prim_path="/rig /box", collision_path="/table",
                                      frame_count=FRAMES, start_frame=1, mass=0.05,
                                      friction=1.0, substeps=8)
        scene.set_output(rbd)

        stage = scene.get_stage()
        before, after = _box_at(stage, 25), _box_at(stage, FRAMES)
        assert before[2] == pytest.approx(0.02, abs=0.005), "the box sits on the table until the lift"
        assert after[2] == pytest.approx(0.12, abs=0.01), f"the box did not rise with the fingers: {after}"
        assert abs(after[0]) < 0.01 and abs(after[1]) < 0.01, "the box slid out of the grip"


def test_open_fingers_leave_the_box_on_the_table() -> None:
    """The same lift with the fingers never closing: nothing holds the box, so it stays --
    the box rose above because it was gripped, not because something welded it."""
    with Scene.new() as scene:
        stage = scene.get_stage()
        with Usd.EditContext(stage, stage.GetRootLayer()):
            _gripper_scene(stage)
            for name in ("left", "right"):
                attr = stage.GetPrimAtPath(f"/rig/{name}/slide").GetAttribute(
                    "drive:linear:physics:targetPosition")
                for time in attr.GetTimeSamples():
                    attr.Set(0.0, Usd.TimeCode(time))
        rbd = scene.create.rbd_solver(prim_path="/rig /box", collision_path="/table",
                                      frame_count=FRAMES, start_frame=1, mass=0.05,
                                      friction=1.0, substeps=8)
        scene.set_output(rbd)

        assert _box_at(scene.get_stage(), FRAMES)[2] == pytest.approx(0.02, abs=0.005)


def test_a_second_robot_alike_grips_in_the_same_world() -> None:
    """Two rigs with the same link names (two copies of one robot) and the box under the
    second. Each articulated asset had a solve of its own, the box joining only the first:
    the second's fingers passed through it. Now they share one world, as two mechanisms
    sharing a part must."""
    with Scene.new() as scene:
        stage = scene.get_stage()
        with Usd.EditContext(stage, stage.GetRootLayer()):
            _gripper_scene(stage, rigs=(("/rigA", -0.15), ("/rigB", 0.15)), box_x=0.15)
        rbd = scene.create.rbd_solver(prim_path="/rigA /rigB /box", collision_path="/table",
                                      frame_count=FRAMES, start_frame=1, mass=0.05,
                                      friction=1.0, substeps=8)
        scene.set_output(rbd)

        after = _box_at(scene.get_stage(), FRAMES)
        assert after[2] == pytest.approx(0.12, abs=0.01), f"the second rig did not lift the box: {after}"
        assert "simulation failed" not in rbd.get_param("readout").value
