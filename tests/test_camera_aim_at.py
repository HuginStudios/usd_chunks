"""Camera's Aim At: the camera stays pointed at a prim from wherever it is moved to.

Checked from the composed result -- the camera's world -Z against the target's centre --
so it holds however the rotation is spelled. The rotation lands in the Rotate params, so
the sliders and a saved file read what the camera does.
"""
from __future__ import annotations

import logging
import math

import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


def _miss_degrees(stage, camera, target):
    m = UsdGeom.Xformable(stage.GetPrimAtPath(camera)).ComputeLocalToWorldTransform(0)
    eye = m.Transform(Gf.Vec3d(0, 0, 0))
    forward = (m.Transform(Gf.Vec3d(0, 0, -1)) - eye).GetNormalized()
    centre = UsdGeom.BBoxCache(0, ["default"]).ComputeWorldBound(stage.GetPrimAtPath(target)).ComputeCentroid()
    return math.degrees(math.acos(min(1.0, Gf.Dot(forward, (centre - eye).GetNormalized()))))


def _up(stage, camera):
    m = UsdGeom.Xformable(stage.GetPrimAtPath(camera)).ComputeLocalToWorldTransform(0)
    return m.TransformDir(Gf.Vec3d(0, 1, 0))


def _ball_on_pedestal(scene):
    ped = scene.create.cube(name="Pedestal", prim_path="/World/pedestal", width=0.6, height=1.0, depth=0.6)
    ball = scene.create.sphere(ped, name="Ball", prim_path="/World/ball", radius1=0.3)
    lift = scene.create.transform(ball, primpath_location=ball.get_param("prim_path"),
                                  ty="@Pedestal.height@ + @Ball.radius1@")
    return ped, lift


def test_the_camera_faces_the_prim(scene):
    _, lift = _ball_on_pedestal(scene)
    cam = scene.create.camera(lift, primpath_location="/World/camera1", tx=2.0, ty=2.0, tz=4.0,
                              aim_at="/World/ball")
    scene.set_output(cam)
    stage = scene.get_stage()
    assert _miss_degrees(stage, "/World/camera1", "/World/ball") < 1e-3
    assert _up(stage, "/World/camera1")[1] > 0.9  # level, not rolled
    assert cam.get_param("rz").value == pytest.approx(0.0, abs=1e-6)
    assert cam.get_param("ry").value == pytest.approx(math.degrees(math.atan2(2.0, 4.0)), abs=1e-4)


def test_it_stays_aimed_when_the_camera_or_the_prim_moves(scene):
    ped, lift = _ball_on_pedestal(scene)
    cam = scene.create.camera(lift, primpath_location="/World/camera1", tx=2.0, ty=2.0, tz=4.0,
                              aim_at="/World/ball")
    scene.set_output(cam)
    stage = scene.get_stage()
    scene.set(ped, height=2.5)
    assert _miss_degrees(stage, "/World/camera1", "/World/ball") < 1e-3
    scene.set(cam, tx=-3.0, tz=-1.0)
    assert _miss_degrees(stage, "/World/camera1", "/World/ball") < 1e-3


def test_clearing_it_keeps_the_last_facing(scene):
    _, lift = _ball_on_pedestal(scene)
    cam = scene.create.camera(lift, primpath_location="/World/camera1", tx=2.0, ty=2.0, tz=4.0,
                              aim_at="/World/ball")
    scene.set_output(cam)
    aimed = [cam.get_param(n).value for n in ("rx", "ry", "rz")]
    scene.set(cam, aim_at="")
    assert [cam.get_param(n).value for n in ("rx", "ry", "rz")] == pytest.approx(aimed)


def test_it_aims_in_a_z_up_stage(scene):
    UsdGeom.SetStageUpAxis(scene.get_stage(), UsdGeom.Tokens.z)
    box = scene.create.cube(prim_path="/World/box")
    cam = scene.create.camera(box, primpath_location="/World/camera1", tx=4.0, ty=-3.0, tz=2.0,
                              aim_at="/World/box")
    scene.set_output(cam)
    stage = scene.get_stage()
    assert _miss_degrees(stage, "/World/camera1", "/World/box") < 1e-3
    assert _up(stage, "/World/camera1")[2] > 0.5  # level to Z, the stage's up


def test_a_mistyped_target_warns_and_leaves_the_rotation(scene, caplog):
    box = scene.create.cube(prim_path="/World/box")
    with caplog.at_level(logging.WARNING):
        cam = scene.create.camera(box, primpath_location="/World/camera1", tz=4.0, ry=10.0,
                                  aim_at="/World/bx")
        scene.set_output(cam)
    assert cam.get_param("ry").value == pytest.approx(10.0)
    assert any("/World/bx" in r.getMessage() for r in caplog.records)
