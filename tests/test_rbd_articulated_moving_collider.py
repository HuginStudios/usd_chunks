"""An animated collider moves in an articulated solve, as it does in the rigid one.

A paddle (an animated collider) sweeps across a table and shoves a jointed object ahead of it. The articulated
solve built the paddle's kinematic body where it started and never moved it, so the object stayed put while the
paddle in the viewport swept through it. The rigid path already re-posed animated colliders every frame.
"""
from __future__ import annotations

import importlib.util

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import Scene

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")

FRAMES = 60


def _link(stage, path, at, half):
    """A rigid-body link with one box collider, half extents `half`, at `at`."""
    prim = UsdGeom.Xform.Define(stage, path)
    prim.AddTranslateOp().Set(Gf.Vec3d(*at))
    UsdPhysics.RigidBodyAPI.Apply(prim.GetPrim())
    UsdPhysics.MassAPI.Apply(prim.GetPrim()).CreateMassAttr(0.2)
    shape = UsdGeom.Cube.Define(stage, path + "/collider")
    shape.CreateSizeAttr(2.0)
    shape.AddScaleOp().Set(Gf.Vec3f(*half))
    UsdPhysics.CollisionAPI.Apply(shape.GetPrim())
    return prim.GetPrim()


def _scene(stage):
    """Z up, metres. A table; a free two-link object (a body with a hinged flap) on it, its -X face at x = -0.04;
    a paddle sweeping along +X from just behind that face to x = 0.10."""
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    table = UsdGeom.Cube.Define(stage, "/table")
    table.CreateSizeAttr(1.0)
    table.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, -0.05))
    table.AddScaleOp().Set(Gf.Vec3f(1.0, 1.0, 0.1))
    UsdGeom.Xform.Define(stage, "/thing")
    body = _link(stage, "/thing/body", (0.0, 0.0, 0.04), (0.04, 0.04, 0.04))
    flap = _link(stage, "/thing/flap", (0.0, 0.06, 0.04), (0.04, 0.01, 0.04))
    hinge = stage.DefinePrim("/thing/flap/hinge", "PhysicsRevoluteJoint")
    hinge.CreateRelationship("physics:body0").SetTargets([body.GetPath()])
    hinge.CreateRelationship("physics:body1").SetTargets([flap.GetPath()])
    hinge.CreateAttribute("physics:axis", Sdf.ValueTypeNames.Token).Set("Z")
    hinge.CreateAttribute("physics:localPos0", Sdf.ValueTypeNames.Point3f).Set(Gf.Vec3f(0.0, 0.05, 0.0))
    hinge.CreateAttribute("physics:lowerLimit", Sdf.ValueTypeNames.Float).Set(-5.0)
    hinge.CreateAttribute("physics:upperLimit", Sdf.ValueTypeNames.Float).Set(5.0)
    paddle = UsdGeom.Cube.Define(stage, "/paddle")
    paddle.CreateSizeAttr(1.0)
    move = paddle.AddTranslateOp()
    move.Set(Gf.Vec3d(-0.06, 0.03, 0.04), Usd.TimeCode(1))
    move.Set(Gf.Vec3d(0.10, 0.03, 0.04), Usd.TimeCode(FRAMES))
    paddle.AddScaleOp().Set(Gf.Vec3f(0.01, 0.2, 0.08))


def test_an_animated_paddle_shoves_a_jointed_object() -> None:
    with Scene.new() as scene:
        stage = scene.get_stage()
        _scene(stage)
        rbd = scene.create.rbd_solver(prim_path="/thing", collision_path="/table\n/paddle",
                                      frame_count=FRAMES, start_frame=1, friction=0.5, substeps=8)
        rbd.get_param("run_sim").click()
        end = UsdGeom.XformCache(Usd.TimeCode(FRAMES)).GetLocalToWorldTransform(
            stage.GetPrimAtPath("/thing/body")).ExtractTranslation()
        # the paddle ends at x = 0.10, so the body's middle ends near 0.10 + 0.005 + 0.04
        assert end[0] > 0.08, f"the object stayed at x {end[0]:.3f}: the paddle did not move"
