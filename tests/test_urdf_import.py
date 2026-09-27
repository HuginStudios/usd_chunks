"""URDF in, UsdPhysics out -- and the RBD Solver reads back what URDF said.

The round trip is the point. This chunk authors the exact attributes
`rbd_solver.read_articulations` parses, so a robot can be imported and simulated without a
translation step in between -- and that makes the mapping *testable against an oracle*
rather than against my own assertions: whatever the URDF declared, the solver must see.

Two traps this exists to catch, both of which produce a plausible wrong answer rather than
a failure:

- **Radians and degrees.** URDF limits are radians, USD's are degrees. Getting it wrong
  compiles a 90 degree limit as 1.6 degrees, which reads as a seized joint.
- **The axis token.** `physics:axis` is X, Y or Z, so an axis like (0, -1, 0) -- entirely
  ordinary in a URDF -- cannot be written directly. It has to live in the joint's local
  rotation, which is how the solver reads it back.
"""

from __future__ import annotations

import importlib
import math
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks import chunk_stage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chunks"))
urdf_import = importlib.import_module("urdf_import")
rbd_solver = importlib.import_module("rbd_solver")


TWO_LINK = """
<robot name="arm">
  <material name="3d_printed"><color rgba="1.0 0.82 0.12 1.0"/></material>
  <link name="base">
    <inertial><mass value="2.5"/><origin xyz="0 0 0.1"/>
      <inertia ixx="0.01" iyy="0.02" izz="0.03"/></inertial>
    <collision><geometry><box size="0.2 0.2 0.1"/></geometry></collision>
    <visual><geometry><box size="0.2 0.2 0.1"/></geometry>
      <material name="3d_printed"/></visual>
  </link>
  <link name="upper">
    <inertial><mass value="1.25"/></inertial>
    <collision><geometry><cylinder radius="0.03" length="0.4"/></geometry></collision>
  </link>
  <joint name="shoulder" type="revolute">
    <parent link="base"/>
    <child link="upper"/>
    <origin xyz="0 0 0.15" rpy="0 0 0"/>
    <axis xyz="0 -1 0"/>
    <limit lower="-1.5707963" upper="1.5707963" effort="10" velocity="2"/>
    <dynamics damping="0.7" friction="0.05"/>
  </joint>
</robot>
"""


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(layer.identifier)
    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)
    return stage, sender


def _import(scene, urdf_text, tmp_path, **params):
    stage, sender = scene
    source = tmp_path / "robot.urdf"
    source.write_text(urdf_text, encoding="utf-8")

    node = urdf_import.main()
    node._config = ChunkConfig(module="urdf_import")
    node._module = urdf_import
    node.name = "Import"
    sender.add_chunk(node)
    node.get_param("file_path").value = str(source)
    node.get_param("primpath_location").value = "/World/robot"
    for name, value in params.items():
        node.get_param(name).value = value
    node.execute()
    return node


class TestParse:
    def test_it_finds_the_root_link(self):
        robot = urdf_import.parse(TWO_LINK)
        assert robot["roots"] == ["base"]
        assert robot["name"] == "arm"

    def test_several_unattached_mechanisms_are_all_roots(self):
        """Legal in URDF, and importing only the first would silently drop the rest."""
        robot = urdf_import.parse(
            '<robot name="r"><link name="a"/><link name="b"/></robot>'
        )
        assert sorted(robot["roots"]) == ["a", "b"]

    def test_children_are_recorded_against_their_parent(self):
        robot = urdf_import.parse(TWO_LINK)
        assert [child for _, child in robot["children"]["base"]] == ["upper"]


def _reference_rpy(roll, pitch, yaw, vector):
    """Rz . Ry . Rx applied to a vector, written out longhand.

    Deliberately independent of USD: the point is to have something to check `_origin`
    against that cannot share a mistake with it.
    """
    cx, sx = math.cos(roll), math.sin(roll)
    cy, sy = math.cos(pitch), math.sin(pitch)
    cz, sz = math.cos(yaw), math.sin(yaw)
    x, y, z = vector
    # Rx
    x1, y1, z1 = x, cx * y - sx * z, sx * y + cx * z
    # Ry
    x2, y2, z2 = cy * x1 + sy * z1, y1, -sy * x1 + cy * z1
    # Rz
    return (cz * x2 - sz * y2, sz * x2 + cz * y2, z2)


BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE robot [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<robot name="&c;"><link name="base"/></robot>
"""


class TestUntrustedInput:
    """A URDF is a download. It gets parsed before anyone has looked at it."""

    def test_a_doctype_is_refused(self):
        """ElementTree expands entities, so a few lines can exhaust memory.

        Measured on the three-level version below: 210 bytes of source expands to a
        kilobyte, and every further level multiplies that again. A URDF has no legitimate
        use for a DOCTYPE, so refusing one costs nothing.
        """
        bomb = BILLION_LAUGHS
        with pytest.raises(ValueError, match="DOCTYPE"):
            urdf_import.parse(bomb)

    def test_ordinary_urdf_still_parses(self):
        """The guard must not be a scheme that rejects real files."""
        assert urdf_import.parse(TWO_LINK)["roots"] == ["base"]

    def test_the_urdf_is_not_held_open(self, scene, tmp_path):
        """Windows keeps a lock on an unclosed handle, so the file cannot be replaced.

        Honest about its own limits: on CPython this passes with or without the fix,
        because refcounting closes the temporary as soon as read() returns. It is here to
        state the requirement and to catch a regression on a runtime that collects less
        promptly -- not as evidence the bare open() was harmless. It was not: nothing in
        the language guarantees that timing, and the import read every mesh that way too.
        """
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        source = tmp_path / "robot.urdf"
        replacement = tmp_path / "other.urdf"
        replacement.write_text(TWO_LINK, encoding="utf-8")
        os.replace(replacement, source)  # raises PermissionError if still held


class TestOrigin:
    @pytest.mark.parametrize(
        "rpy",
        [
            (0.0, 0.0, 1.5707963),
            (math.pi, math.pi / 2, 0.0),
            (-math.pi / 2, 0.0, math.pi / 2),
            (0.3, -0.7, 1.1),
        ],
    )
    def test_rpy_is_fixed_axis_x_then_y_then_z(self, rpy):
        """A single-axis rpy composes the same in either order, so it proves nothing.

        The importer originally multiplied the three rotations the way the matrix form
        reads, Rz * Ry * Rx, which is backwards for Gf.Rotation -- and a test that only
        turned about yaw passed anyway. Two robots were validated before a URDF with
        two-axis origins put every leg link in a plausible-looking wrong place.
        """
        element = ET.fromstring(
            f'<j><origin xyz="1 2 3" rpy="{rpy[0]} {rpy[1]} {rpy[2]}"/></j>'
        )
        position, rotation = urdf_import._origin(element)
        assert tuple(position) == (1.0, 2.0, 3.0)

        probe = Gf.Vec3d(0.074, -0.02, 0.035)
        expected = _reference_rpy(*rpy, probe)
        turned = rotation.TransformDir(probe)
        for got, want in zip(turned, expected):
            assert got == pytest.approx(want, abs=1e-7)

    def test_a_missing_origin_is_identity(self):
        position, rotation = urdf_import._origin(ET.fromstring("<j/>"))
        assert tuple(position) == (0.0, 0.0, 0.0)
        assert rotation.GetAngle() == pytest.approx(0.0)


class TestAxisFrame:
    def test_it_rotates_the_canonical_axis_onto_the_urdf_one(self):
        rotation = urdf_import._axis_frame([0, -1, 0])
        turned = rotation.TransformDir(urdf_import._CANONICAL_AXIS)
        assert tuple(round(v, 6) for v in turned) == (0.0, -1.0, 0.0)

    def test_a_zero_axis_is_identity_rather_than_a_crash(self):
        assert urdf_import._axis_frame([0, 0, 0]).GetAngle() == pytest.approx(0.0)


TWISTED = """<robot name="twisted">
  <link name="base"/>
  <link name="middle"/>
  <link name="tip"/>
  <joint name="first" type="revolute">
    <parent link="base"/>
    <child link="middle"/>
    <origin xyz="0.019 0 -0.046" rpy="-1.5707963 0 1.5707963"/>
    <axis xyz="0 0 1"/>
    <limit lower="-0.5" upper="0.5"/>
  </joint>
  <joint name="second" type="revolute">
    <parent link="middle"/>
    <child link="tip"/>
    <origin xyz="0.074 0 0.035" rpy="3.1415927 1.5707963 0"/>
    <axis xyz="0 -1 0"/>
    <limit lower="-0.5" upper="0.5"/>
  </joint>
</robot>
"""


#: A branching mechanism whose joint origins turn about TWO axes at once.
#:
#: That is the whole point of it. A single-axis rpy composes the same whichever way the
#: rotations are multiplied, so the SO-101 and the Panda both validated an implementation
#: that was wrong -- only the Open Duck Mini, which has two-axis origins and branches,
#: exposed it. This is the smallest thing with that property, and it needs no meshes, so
#: the check can live in the suite instead of in a PR description.
TWO_AXIS = """<robot name="twoaxis">
  <link name="base">
    <inertial><mass value="1"/><origin xyz="0 0 0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial>
  </link>
  <link name="left_upper">
    <inertial><mass value="1"/><origin xyz="0 0 0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial>
  </link>
  <link name="left_lower">
    <inertial><mass value="1"/><origin xyz="0 0 0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial>
  </link>
  <link name="right_upper">
    <inertial><mass value="1"/><origin xyz="0 0 0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial>
  </link>
  <joint name="left_hip" type="revolute">
    <parent link="base"/><child link="left_upper"/>
    <origin xyz="0.019 0.035 -0.046" rpy="-1.5707963 0 1.5707963"/>
    <axis xyz="0 0 1"/><limit lower="-0.6" upper="0.6" effort="10" velocity="1"/>
  </joint>
  <joint name="left_knee" type="revolute">
    <parent link="left_upper"/><child link="left_lower"/>
    <origin xyz="0.074 0 0.035" rpy="3.1415927 1.5707963 0"/>
    <axis xyz="0 -1 0"/><limit lower="-0.6" upper="0.6" effort="10" velocity="1"/>
  </joint>
  <joint name="right_hip" type="revolute">
    <parent link="base"/><child link="right_upper"/>
    <origin xyz="0.019 -0.035 -0.046" rpy="0.3 -0.7 1.1"/>
    <axis xyz="0 0 1"/><limit lower="-0.6" upper="0.6" effort="10" velocity="1"/>
  </joint>
</robot>
"""

#: The real robots this was validated against are not in the repo -- they are downloads,
#: and the meshes are tens of megabytes. To re-run the full check, fetch:
#:
#:   SO-101   github.com/TheRobotStudio/SO-ARM100 (so101_new_calib.urdf)
#:   Panda    github.com/frankaemika/franka_ros   (panda.urdf + collision STLs)
#:   Duck     github.com/apirrone/Open_Duck_Mini  branch v2,
#:            mini_bdx/robots/open_duck_mini_v2/robot.urdf
#:
#: then point URDF_ORACLE_ASSETS at a directory holding them and the test below picks
#: every .urdf up. The synthetic case above is what actually guards the bug in CI.
_ASSET_DIR = os.environ.get("URDF_ORACLE_ASSETS")

mujoco = pytest.importorskip("mujoco", reason="the FK oracle needs MuJoCo")


def _mujoco_positions(urdf_path):
    """Where MuJoCo puts each body, parsing the same URDF independently."""
    model = mujoco.MjModel.from_xml_path(str(urdf_path))
    data = mujoco.MjData(model)
    mujoco.mj_resetData(model, data)
    mujoco.mj_forward(model, data)
    out = {}
    for index in range(model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, index)
        if name:
            out[name] = tuple(float(v) for v in data.xpos[index])
    return out


class TestAgainstMuJoCo:
    """The mapping checked against something that had no hand in producing it.

    Every other test here asserts what this code was written to do. MuJoCo parses the same
    URDF and computes its own forward kinematics, so agreeing with it is evidence rather
    than restatement -- and it is the only kind of test that would have caught the rpy
    composition being backwards.
    """

    def test_every_link_is_where_mujoco_puts_it(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_AXIS, tmp_path)
        reference = _mujoco_positions(tmp_path / "robot.urdf")

        cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        checked = 0
        for name, expected in reference.items():
            prim = stage.GetPrimAtPath(f"/World/robot/{name}")
            if not prim.IsValid():
                continue
            ours = cache.GetLocalToWorldTransform(prim).ExtractTranslation()
            for got, want in zip(ours, expected):
                assert got == pytest.approx(want, abs=1e-6), f"{name} is misplaced"
            checked += 1
        # Three of the four links: MuJoCo folds a URDF's root into its worldbody, so
        # `base` is not a body it can be asked about. The same is true of the SO-101's
        # base_link and the Panda's, which is why they were skipped there too.
        assert checked == 3, "the oracle compared the wrong number of links"

    @pytest.mark.skipif(not _ASSET_DIR, reason="URDF_ORACLE_ASSETS is not set")
    def test_real_robots_match_mujoco(self, scene, tmp_path):
        """The same check against the robots as downloaded, when they are available."""
        stage, _ = scene
        sources = sorted(Path(_ASSET_DIR).rglob("*.urdf"))
        assert sources, f"no .urdf under {_ASSET_DIR}"
        for source in sources:
            reference = _mujoco_positions(source)
            _import(scene, source.read_text(encoding="utf-8"), tmp_path,
                    mesh_root=str(source.parent))
            cache = UsdGeom.XformCache(Usd.TimeCode.Default())
            for name, expected in reference.items():
                prim = stage.GetPrimAtPath(f"/World/robot/{name}")
                if not prim.IsValid():
                    continue
                ours = cache.GetLocalToWorldTransform(prim).ExtractTranslation()
                for got, want in zip(ours, expected):
                    assert got == pytest.approx(want, abs=1e-5), f"{source.name}: {name}"


class TestJointFrames:
    """Both bodies of a joint have to agree on where the joint is.

    Every other test here reads one side: link placement, or the axis and limits the solver
    parses back. A `physics:localRot0` composed in the wrong order passes all of them and
    still describes a joint whose two halves sit in different places -- which is what shipped
    until a robot with two-axis origins was measured against MuJoCo.
    """

    def _frame(self, stage, joint, index):
        body = joint.GetRelationship(f"physics:body{index}").GetTargets()[0]
        world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(
            stage.GetPrimAtPath(body)
        )
        local = Gf.Matrix4d(1.0)
        local.SetRotateOnly(
            Gf.Rotation(Gf.Quatd(joint.GetAttribute(f"physics:localRot{index}").Get()))
        )
        local.SetTranslateOnly(Gf.Vec3d(*joint.GetAttribute(f"physics:localPos{index}").Get()))
        return local * world

    @pytest.mark.parametrize("joint_path", ["middle/joint_first", "tip/joint_second"])
    def test_both_bodies_place_the_joint_in_the_same_frame(self, scene, tmp_path, joint_path):
        stage, _ = scene
        _import(scene, TWISTED, tmp_path)
        joint = stage.GetPrimAtPath(f"/World/robot/{joint_path}")
        assert joint.IsValid()

        in_parent = self._frame(stage, joint, 0)
        in_child = self._frame(stage, joint, 1)
        for row_a, row_b in zip(in_parent, in_child):
            for a, b in zip(row_a, row_b):
                assert a == pytest.approx(b, abs=1e-6)


class TestAuthoring:
    def test_links_become_rigid_bodies_under_the_target(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)

        base = stage.GetPrimAtPath("/World/robot/base")
        upper = stage.GetPrimAtPath("/World/robot/upper")
        assert base.IsValid() and upper.IsValid(), "both links should be authored"
        assert base.HasAPI(UsdPhysics.RigidBodyAPI)
        assert upper.HasAPI(UsdPhysics.RigidBodyAPI)

    def test_the_root_link_carries_the_articulation(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        assert stage.GetPrimAtPath("/World/robot/base").HasAPI(UsdPhysics.ArticulationRootAPI)

    def test_mass_and_inertia_survive(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        mass_api = UsdPhysics.MassAPI(stage.GetPrimAtPath("/World/robot/base"))
        assert mass_api.GetMassAttr().Get() == pytest.approx(2.5)
        assert tuple(mass_api.GetCenterOfMassAttr().Get()) == pytest.approx((0, 0, 0.1))

    def test_collision_geometry_is_guide_purpose(self, scene, tmp_path):
        """Present for physics, out of the way of a render -- the appliance drop lost an
        afternoon to colliders that were invisible to a default-purpose bbox query."""
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        collider = stage.GetPrimAtPath("/World/robot/base/collision_0")
        assert collider.IsValid()
        assert UsdGeom.Imageable(collider).GetPurposeAttr().Get() == UsdGeom.Tokens.guide
        assert collider.HasAPI(UsdPhysics.CollisionAPI)

    def test_the_approximation_is_what_was_asked_for(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path, collider_approximation="boundingCube")
        collider = stage.GetPrimAtPath("/World/robot/base/collision_0")
        assert UsdPhysics.MeshCollisionAPI(collider).GetApproximationAttr().Get() == "boundingCube"

    def test_the_joint_is_typed_and_wired_to_both_bodies(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        joint = stage.GetPrimAtPath("/World/robot/upper/joint_shoulder")
        assert joint.GetTypeName() == "PhysicsRevoluteJoint"
        assert [str(t) for t in joint.GetRelationship("physics:body0").GetTargets()] == [
            "/World/robot/base"
        ]
        assert [str(t) for t in joint.GetRelationship("physics:body1").GetTargets()] == [
            "/World/robot/upper"
        ]

    def test_scale_is_applied_to_lengths(self, scene, tmp_path):
        """URDF is always metres. A centimetre stage wants 100, and getting it wrong gives
        a plausible robot of the wrong size."""
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path, scale=100.0)
        upper = stage.GetPrimAtPath("/World/robot/upper")
        translate = UsdGeom.Xformable(upper).GetOrderedXformOps()[0].Get()
        assert translate[2] == pytest.approx(15.0)


class TestRoundTrip:
    """URDF -> USD -> the RBD Solver. Whatever the URDF declared, the solver must see.

    This is the oracle the other tests lack: it checks the mapping against the source of
    truth rather than against what I decided to author.
    """

    def _joint(self, scene, tmp_path, urdf_text=TWO_LINK, **params):
        stage, _ = scene
        _import(scene, urdf_text, tmp_path, **params)
        links = rbd_solver.read_articulations(stage.GetPrimAtPath("/World/robot"))
        assert links, "the solver found no articulation at all"
        joints = []

        def walk(link):
            if getattr(link, "joint", None) is not None:
                joints.append(link.joint)
            for child in getattr(link, "children", []):
                walk(child)

        for root in links:
            walk(root)
        assert joints, "the solver found links but no joints"
        return joints[0]

    def test_the_solver_reads_the_axis_the_urdf_declared(self, scene, tmp_path):
        """(0, -1, 0) cannot be written as a token, so it has to survive in localRot1."""
        joint = self._joint(scene, tmp_path)
        assert [round(v, 6) for v in joint.axis] == [0.0, -1.0, 0.0]

    def test_the_solver_reads_the_limits_in_radians(self, scene, tmp_path):
        """URDF radians -> USD degrees -> solver radians. A missed conversion turns a 90
        degree limit into 1.6 degrees and the joint looks seized."""
        joint = self._joint(scene, tmp_path)
        assert joint.lower == pytest.approx(-math.pi / 2, abs=1e-4)
        assert joint.upper == pytest.approx(math.pi / 2, abs=1e-4)

    def test_damping_survives(self, scene, tmp_path):
        joint = self._joint(scene, tmp_path)
        assert joint.damping == pytest.approx(0.7)

    def test_a_continuous_joint_has_no_limits(self, scene, tmp_path):
        spinning = TWO_LINK.replace('type="revolute"', 'type="continuous"')
        joint = self._joint(scene, tmp_path, spinning)
        assert joint.lower is None and joint.upper is None

    def test_a_fixed_joint_reads_as_fixed(self, scene, tmp_path):
        welded = TWO_LINK.replace('type="revolute"', 'type="fixed"')
        joint = self._joint(scene, tmp_path, welded)
        assert joint.kind == "fixed"

    def test_a_prismatic_joint_reads_as_prismatic(self, scene, tmp_path):
        sliding = TWO_LINK.replace('type="revolute"', 'type="prismatic"')
        joint = self._joint(scene, tmp_path, sliding)
        assert joint.kind == "prismatic"
        # Prismatic limits are a length, so they stay in stage units rather than becoming
        # degrees -- the one place the revolute conversion must not apply.
        assert joint.lower == pytest.approx(-1.5707963, abs=1e-4)


class TestMaterials:
    """URDF declares materials once and refers to them by name from each <visual>.

    Ignoring them gives correct geometry in uniform grey -- a joint diagram rather than a
    robot. Missed on the first pass, and caught by a real SO-101 rather than by these tests,
    which is why the fixture now carries one.
    """

    def test_a_named_material_becomes_a_bound_mtlx_surface(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)

        visual = stage.GetPrimAtPath("/World/robot/base/visual_0")
        assert visual.IsValid()
        bound, _ = UsdShade.MaterialBindingAPI(visual).ComputeBoundMaterial()
        assert bound, "the visual is unbound"
        surface = bound.GetSurfaceOutput("mtlx")
        assert surface and surface.HasConnectedSource()

    def test_the_colour_is_stored_linear(self, scene, tmp_path):
        """A URDF colour is sRGB, base_color is linear. Passing it through unconverted
        lifts the dark channels and the model reads washed out -- the pastel-Porsche bug."""
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        bound, _ = UsdShade.MaterialBindingAPI(
            stage.GetPrimAtPath("/World/robot/base/visual_0")
        ).ComputeBoundMaterial()
        shader = UsdShade.Shader(bound.GetSurfaceOutput("mtlx").GetConnectedSource()[0].GetPrim())
        red, green, blue = shader.GetInput("base_color").Get()
        assert red == pytest.approx(1.0, abs=1e-3)
        assert green == pytest.approx(urdf_import._srgb_to_linear(0.82), abs=1e-4)
        assert green < 0.82, "sRGB was passed through as linear"

    def test_display_color_is_authored_too(self, scene, tmp_path):
        """One attribute, and the robot reads correctly in a viewport with no material
        support -- which is most of them."""
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)
        colour = UsdGeom.Gprim(
            stage.GetPrimAtPath("/World/robot/base/visual_0")
        ).GetDisplayColorAttr().Get()
        assert tuple(colour[0]) == pytest.approx((1.0, 0.82, 0.12), abs=1e-4)
