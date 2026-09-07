"""URDF in, UsdPhysics out — and the RBD Solver reads back what URDF said.

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
import sys
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks.plugins import chunk_usd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
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
    chunk_usd.ChunkStageSync().initialize(stage, sender)
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


class TestOrigin:
    def test_rpy_composes_z_then_y_then_x(self):
        """URDF rpy is fixed-axis roll-pitch-yaw, which composes as Rz*Ry*Rx. Backwards
        gives a robot whose links are all in plausible-looking wrong places."""
        element = Sdf.Layer  # placeholder, replaced below
        import xml.etree.ElementTree as ET

        node = ET.fromstring('<j><origin xyz="1 2 3" rpy="0 0 1.5707963"/></j>')
        position, rotation = urdf_import._origin(node)
        assert tuple(position) == (1.0, 2.0, 3.0)
        # A 90 degree yaw takes +X to +Y.
        turned = rotation.TransformDir(Gf.Vec3d(1, 0, 0))
        assert turned[0] == pytest.approx(0.0, abs=1e-6)
        assert turned[1] == pytest.approx(1.0, abs=1e-6)
        assert element is Sdf.Layer  # keep the import honest

    def test_a_missing_origin_is_identity(self):
        import xml.etree.ElementTree as ET

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


class TestAuthoring:
    def test_links_become_rigid_bodies_under_the_target(self, scene, tmp_path):
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path)

        base = stage.GetPrimAtPath("/World/robot/base")
        upper = stage.GetPrimAtPath("/World/robot/base/upper")
        assert base.IsValid() and upper.IsValid(), "the joint tree should nest the links"
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
        joint = stage.GetPrimAtPath("/World/robot/base/upper/joint_shoulder")
        assert joint.GetTypeName() == "PhysicsRevoluteJoint"
        assert [str(t) for t in joint.GetRelationship("physics:body0").GetTargets()] == [
            "/World/robot/base"
        ]
        assert [str(t) for t in joint.GetRelationship("physics:body1").GetTargets()] == [
            "/World/robot/base/upper"
        ]

    def test_scale_is_applied_to_lengths(self, scene, tmp_path):
        """URDF is always metres. A centimetre stage wants 100, and getting it wrong gives
        a plausible robot of the wrong size."""
        stage, _ = scene
        _import(scene, TWO_LINK, tmp_path, scale=100.0)
        upper = stage.GetPrimAtPath("/World/robot/base/upper")
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
