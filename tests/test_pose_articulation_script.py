"""Pose Articulation from a script: its sliders, made as it cooks, are set like any param.

Benchmark agents posing an imported URDF looked for sliders by joint name (`elbow`) and
`usdchunks show` could not list them, since they only exist once the articulation is read.
They are named after the link each joint moves; a wrong name now raises listing them.
"""
from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import Scene

URDF = """<robot name="arm">
  <link name="base"><collision><geometry><box size="0.2 0.2 0.1"/></geometry></collision></link>
  <link name="upper"><inertial><mass value="1"/></inertial>
    <collision><geometry><box size="0.05 0.05 0.4"/></geometry></collision></link>
  <joint name="shoulder" type="revolute">
    <parent link="base"/><child link="upper"/><origin xyz="0 0 0.15"/>
    <axis xyz="0 1 0"/><limit lower="-1.6" upper="1.6" effort="1" velocity="1"/>
  </joint>
</robot>
"""


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


def test_a_slider_is_set_at_create_by_the_link_it_moves(urdf) -> None:
    with Scene.new() as scene:
        robot = scene.create.urdf_import(primpath_location="/World/robot", file_path=urdf)
        pose = scene.create.pose_articulation(robot, prim_path="/World/robot", upper=30.0)
        scene.set_output(pose)
        assert _turned(scene.get_stage()) == pytest.approx(30.0, abs=1e-3)


def test_a_joint_name_raises_and_lists_the_sliders(urdf) -> None:
    with Scene.new() as scene:
        robot = scene.create.urdf_import(primpath_location="/World/robot", file_path=urdf)
        pose = scene.create.pose_articulation(robot, prim_path="/World/robot")
        with pytest.raises(KeyError, match="its params are: .*upper"):
            scene.set(pose, shoulder=30.0)
