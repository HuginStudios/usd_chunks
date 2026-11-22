"""RBD Solver's Live Settle: every cook settles at once, so posing upstream lands where
physics lets the robot rest.

Posing a robot (Pose Articulation, inverse kinematics) and only then pressing Run Simulation to see
whether its motors can hold it, or where the table stops it, is a loop of minutes for a
question the solve answers in milliseconds. Live Settle answers it on every cook.
"""
from __future__ import annotations

import importlib.util

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mujoco") is None, reason="needs mujoco")

URDF = """<robot name="arm">
  <link name="base"><inertial><mass value="2"/></inertial>
    <collision><geometry><box size="0.2 0.2 0.1"/></geometry></collision></link>
  <link name="upper"><inertial><mass value="1"/></inertial>
    <collision><origin xyz="0 0 0.2"/><geometry><box size="0.05 0.05 0.4"/></geometry></collision></link>
  <joint name="shoulder" type="revolute">
    <parent link="base"/><child link="upper"/><origin xyz="0 0 0.15"/>
    <axis xyz="0 1 0"/><limit lower="-1.6" upper="1.6" effort="1" velocity="1"/>
    <dynamics damping="0.05"/>
  </joint>
</robot>
"""
JOINT = "/World/robot/upper/joint_shoulder"


@pytest.fixture
def urdf(tmp_path):
    path = tmp_path / "arm.urdf"
    path.write_text(URDF, encoding="utf-8")
    return str(path)


def _turned(stage) -> float:
    """How far `upper` is turned about Y, in degrees."""
    m = UsdGeom.Xformable(stage.GetPrimAtPath("/World/robot/upper")).ComputeLocalToWorldTransform(
        Usd.TimeCode.Default())
    return m.ExtractRotation().Decompose((1, 0, 0), (0, 1, 0), (0, 0, 1))[1]


def _z_up(stage) -> None:
    """URDF is Z-up. Y-up, gravity runs along the shoulder's Y axis and nothing needs holding."""
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)


def _motorise(stage) -> None:
    """A stiffness on the shoulder's drive, which Pose Articulation gives a target: a motor.
    Authored on the root layer, under every chunk's."""
    layer = stage.GetRootLayer()
    spec = Sdf.CreatePrimInLayer(layer, JOINT)
    spec.specifier = Sdf.SpecifierOver
    attr = Sdf.AttributeSpec(spec, "drive:angular:physics:stiffness", Sdf.ValueTypeNames.Float)
    attr.default = 5.0


def _animated(stage) -> bool:
    prim = stage.GetPrimAtPath("/World/robot/upper")
    return any(a.GetNumTimeSamples() for a in prim.GetAttributes())


def test_live_settle_holds_the_pose_and_bakes_no_animation(urdf) -> None:
    with Scene.new() as scene:
        _z_up(scene.get_stage())
        _motorise(scene.get_stage())
        robot = scene.create.urdf_import(primpath_location="/World/robot", file_path=urdf)
        pose = scene.create.pose_articulation(robot, prim_path="/World/robot", upper=30.0)
        rbd = scene.create.rbd_solver(pose, prim_path="/World/robot", live_settle=True)
        scene.set_output(rbd)

        stage = scene.get_stage()
        assert _turned(stage) == pytest.approx(30.0, abs=2.0)
        assert not _animated(stage), "a live settle writes the resting pose, not frames"


def test_posing_again_settles_again(urdf) -> None:
    """The point of it: move the pose upstream and the solver follows, no Run needed."""
    with Scene.new() as scene:
        _z_up(scene.get_stage())
        _motorise(scene.get_stage())
        robot = scene.create.urdf_import(primpath_location="/World/robot", file_path=urdf)
        pose = scene.create.pose_articulation(robot, prim_path="/World/robot", upper=30.0)
        rbd = scene.create.rbd_solver(pose, prim_path="/World/robot", live_settle=True)
        scene.set_output(rbd)

        scene.set(pose, upper=-20.0)

        assert _turned(scene.get_stage()) == pytest.approx(-20.0, abs=2.0)


def test_a_new_pose_reuses_the_model_and_settles_as_a_fresh_one_would(urdf) -> None:
    """Rebuilding the model -- colliders, hulls, compile -- was most of every move. A new
    pose keeps it, measuring the targets from the ones it was built at, and lands where a
    model built afresh in that pose does."""
    with Scene.new() as scene:
        _z_up(scene.get_stage())
        _motorise(scene.get_stage())
        robot = scene.create.urdf_import(primpath_location="/World/robot", file_path=urdf)
        pose = scene.create.pose_articulation(robot, prim_path="/World/robot", upper=30.0)
        rbd = scene.create.rbd_solver(pose, prim_path="/World/robot", live_settle=True)
        scene.set_output(rbd)
        (kept,) = rbd._rbd_live_models.values()
        model = kept["entry"]["model"]

        scene.set(pose, upper=-20.0)
        assert kept["entry"]["model"] is model, "the model was rebuilt for a new pose"
        cached = _turned(scene.get_stage())

        rbd._rbd_live_models = {}  # forget it: this cook builds afresh
        rbd.execute()
        assert cached == pytest.approx(_turned(scene.get_stage()), abs=0.01)


def test_without_motors_the_arm_is_not_held(urdf) -> None:
    """The same settle with no stiffness: the pose is not held, so the motor is what held it
    above -- not the settle simply leaving the pose alone."""
    with Scene.new() as scene:
        _z_up(scene.get_stage())
        robot = scene.create.urdf_import(primpath_location="/World/robot", file_path=urdf)
        pose = scene.create.pose_articulation(robot, prim_path="/World/robot", upper=30.0)
        rbd = scene.create.rbd_solver(pose, prim_path="/World/robot", live_settle=True)
        scene.set_output(rbd)

        assert abs(_turned(scene.get_stage()) - 30.0) > 5.0
