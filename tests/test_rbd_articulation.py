"""RBD Solver reads a mechanism out of UsdPhysics joints.

A sim-ready asset describes an articulated object as flat sibling links joined by
UsdPhysics joints: the tree lives in the joint relationships, not the prim hierarchy, and
the joint prim sits inside the child link with physics:body0 naming the parent.

Fixtures author real UsdPhysics rather than shipping a third-party asset, so the tests are
deterministic and licence-clean. Each one pins a behaviour that was found the hard way
against a real corpus, and most of them fail silently rather than loudly when broken --
which is why they are worth having.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chunks" / "dynamics"))
import rbd_solver as rbd  # noqa: E402

# Tree building and joint parsing are pure USD and run anywhere. Building a model, and
# anything reading a collider shape, needs mujoco -- an optional dependency of this chunk.
mujoco = pytest.importorskip("mujoco", reason="needs mujoco") if rbd._HAS_MUJOCO else None
needs_mujoco = pytest.mark.skipif(not rbd._HAS_MUJOCO, reason="needs mujoco")


def _link(stage, path, *, collider=True, size=(0.2, 0.2, 0.2)):
    """A rigid-body link with one box collider, marked guide like a real asset's proxy."""
    prim = UsdGeom.Xform.Define(stage, path).GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(prim)
    UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(1.0)
    if collider:
        cube = UsdGeom.Cube.Define(stage, path + "/Collisions/C001")
        cube.CreateSizeAttr(2.0)
        cube.AddScaleOp().Set(Gf.Vec3f(*size))
        cube.CreatePurposeAttr(UsdGeom.Tokens.guide)
        UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return prim


def _joint(stage, path, kind, parent, child, *, axis="X",
           local_rot=None, lower=None, upper=None, pos=(0.0, 0.0, 0.0)):
    prim = stage.DefinePrim(path, kind)
    if parent is not None:
        prim.CreateRelationship("physics:body0").SetTargets([parent.GetPath()])
    prim.CreateRelationship("physics:body1").SetTargets([child.GetPath()])
    prim.CreateAttribute("physics:axis", Sdf.ValueTypeNames.Token).Set(axis)
    prim.CreateAttribute("physics:localPos1", Sdf.ValueTypeNames.Point3f).Set(Gf.Vec3f(*pos))
    if local_rot is not None:
        prim.CreateAttribute("physics:localRot1", Sdf.ValueTypeNames.Quatf).Set(local_rot)
    if lower is not None:
        prim.CreateAttribute("physics:lowerLimit", Sdf.ValueTypeNames.Float).Set(lower)
    if upper is not None:
        prim.CreateAttribute("physics:upperLimit", Sdf.ValueTypeNames.Float).Set(upper)
    return prim


def _cabinet():
    """A body with a hinged door, the shape almost every asset in the corpus takes."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Xform.Define(stage, "/root")
    body = _link(stage, "/root/Body")
    door = _link(stage, "/root/Door", size=(0.02, 0.2, 0.2))
    _joint(stage, "/root/Body/FixedJoint", "PhysicsFixedJoint", None, body)
    _joint(stage, "/root/Door/hinge", "PhysicsRevoluteJoint", body, door,
           lower=-90.0, upper=0.0)
    return stage, stage.GetPrimAtPath("/root")


class TestTreeFromJoints:
    def test_parent_comes_from_body0_not_the_prim_hierarchy(self):
        """Links are flat siblings; only the joint says which contains which."""
        stage, root = _cabinet()

        roots = rbd.read_articulations(root)

        assert len(roots) == 1
        assert roots[0].name == "Body"
        assert [c.name for c in roots[0].children] == ["Door"]

    def test_an_asset_with_no_joints_is_not_an_articulation(self):
        """The signal to fall through to the plain rigid-body path."""
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/root")
        _link(stage, "/root/Body")

        assert rbd.read_articulations(stage.GetPrimAtPath("/root")) == []

    def test_every_disconnected_mechanism_is_returned(self):
        """28 of 243 assets in the corpus have more than one root, one has fourteen.
        Returning only the first silently drops most of such an asset."""
        stage, root = _cabinet()
        _link(stage, "/root/Carafe")  # a separate body, jointed to nothing

        roots = rbd.read_articulations(root)

        assert sorted(r.name for r in roots) == ["Body", "Carafe"]

    def test_an_annotation_scope_is_not_a_body(self):
        """These assets apply PhysicsRigidBodyAPI to `Sites` scopes holding manipulation
        markers. They hold no collider, compile as massless bodies, and take the whole
        model down with an error naming only the scope."""
        stage, root = _cabinet()
        _link(stage, "/root/Sites", collider=False)

        assert [r.name for r in rbd.read_articulations(root)] == ["Body"]

    def test_a_closed_loop_is_declined(self):
        """MuJoCo's tree cannot express one, so it must not be built."""
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/root")
        a = _link(stage, "/root/A")
        b = _link(stage, "/root/B")
        _joint(stage, "/root/B/j1", "PhysicsRevoluteJoint", a, b)
        _joint(stage, "/root/A/j2", "PhysicsRevoluteJoint", b, a)

        assert rbd.read_articulations(stage.GetPrimAtPath("/root")) == []

    def test_a_loop_closed_by_an_excluded_joint_is_the_open_chain(self):
        """UsdPhysics' excludeFromArticulation marks the joint that closes a loop (a four-bar's
        fourth bar): it stays out of the tree, which is then the chain it closes."""
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/root")
        a, b, c = (_link(stage, f"/root/{n}") for n in "ABC")
        _joint(stage, "/root/B/j1", "PhysicsRevoluteJoint", a, b)
        _joint(stage, "/root/C/j2", "PhysicsRevoluteJoint", b, c)
        closing = _joint(stage, "/root/A/j3", "PhysicsRevoluteJoint", c, a)
        UsdPhysics.Joint(closing).CreateExcludeFromArticulationAttr(True)

        roots = rbd.read_articulations(stage.GetPrimAtPath("/root"))

        assert [r.name for r in roots] == ["A"]
        middle = roots[0].children
        assert [m.name for m in middle] == ["B"] and [c.name for c in middle[0].children] == ["C"]


class TestJointFrames:
    def test_axis_is_rotated_into_the_child_frame(self):
        """physics:axis names an axis of the joint frame, and localRot1 expresses that
        frame in the child's space. Skipping the rotation hinges the door about the right
        point in the wrong plane -- which looks plausible and is not.

        The quaternion here is the one the corpus uses: it takes X to Z, which is what
        makes a microwave door swing about a vertical axis."""
        stage, root = _cabinet()
        hinge = stage.GetPrimAtPath("/root/Door/hinge")
        hinge.CreateAttribute("physics:localRot1", Sdf.ValueTypeNames.Quatf).Set(
            Gf.Quatf(0.7071068, Gf.Vec3f(0.0, -0.7071068, 0.0))
        )

        door = rbd.read_articulations(root)[0].children[0]

        assert door.joint.axis == pytest.approx([0.0, 0.0, 1.0], abs=1e-5)

    def test_axis_is_taken_as_is_when_no_rotation_is_authored(self):
        stage, root = _cabinet()

        door = rbd.read_articulations(root)[0].children[0]

        assert door.joint.axis == pytest.approx([1.0, 0.0, 0.0], abs=1e-6)

    def test_rotational_limits_are_converted_to_radians(self):
        """USD authors them in degrees."""
        stage, root = _cabinet()

        door = rbd.read_articulations(root)[0].children[0]

        assert door.joint.lower == pytest.approx(math.radians(-90.0))
        assert door.joint.upper == pytest.approx(0.0)

    def test_prismatic_limits_are_left_in_linear_units(self):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/root")
        body = _link(stage, "/root/Body")
        drawer = _link(stage, "/root/Drawer")
        _joint(stage, "/root/Body/FixedJoint", "PhysicsFixedJoint", None, body)
        _joint(stage, "/root/Drawer/slide", "PhysicsPrismaticJoint", body, drawer,
               lower=0.0, upper=0.35)

        slide = rbd.read_articulations(stage.GetPrimAtPath("/root"))[0].children[0]

        assert slide.joint.kind == "prismatic"
        assert slide.joint.upper == pytest.approx(0.35)

    def test_a_generic_joint_is_treated_as_fixed_rather_than_guessed_at(self):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/root")
        body = _link(stage, "/root/Body")
        part = _link(stage, "/root/Part")
        _joint(stage, "/root/Part/j", "PhysicsJoint", body, part)

        part_link = rbd.read_articulations(stage.GetPrimAtPath("/root"))[0].children[0]

        assert part_link.joint.kind == "fixed"


@needs_mujoco
class TestColliders:
    def test_size_comes_from_the_gprim_not_its_extent(self):
        """The corpus authors `extent` as the final size while also carrying a scale and
        leaving `size` unauthored, so it defaults to 2. A bound-based reader multiplies the
        two and gets a collider five times too small, with nothing reporting an error."""
        stage, root = _cabinet()
        cube = stage.GetPrimAtPath("/root/Body/Collisions/C001")
        # Author the misleading extent exactly as the real assets do.
        cube.GetAttribute("extent").Set([Gf.Vec3f(-0.2, -0.2, -0.2), Gf.Vec3f(0.2, 0.2, 0.2)])

        shape = rbd._collider_shape(cube, Gf.Matrix4d(1.0).SetScale(Gf.Vec3d(0.2, 0.2, 0.2)))

        assert shape is not None
        _type, size, _pos, _quat = shape
        assert size == pytest.approx([0.2, 0.2, 0.2], abs=1e-6)


@needs_mujoco
class TestModelBuild:
    def test_the_hinge_survives_into_the_compiled_model(self):
        stage, root = _cabinet()
        roots = rbd.read_articulations(root)

        model, _data, ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=False,
        )

        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Door_joint")
        assert jid >= 0
        assert model.jnt_type[jid] == mujoco.mjtJoint.mjJNT_HINGE
        assert len(ordered) == 2

    def test_limits_are_not_converted_twice(self):
        """MuJoCo's compiler defaults to reading angles as degrees. Left alone, a limit
        already converted to radians is converted again and a 90-degree door compiles as
        1.6 degrees: a hinge that is present, correctly placed, and barely moves."""
        stage, root = _cabinet()
        roots = rbd.read_articulations(root)

        model, _data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=False,
        )

        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Door_joint")
        assert math.degrees(model.jnt_range[jid][0]) == pytest.approx(-90.0, abs=0.5)

    def test_a_link_starting_inside_a_collider_still_collides_with_it(self):
        """Initial overlaps are excluded between the mechanism's own links only. The
        colliders sit in a body of their own, so excluding every overlapping pair dropped a
        link's contact with the table its pose had pushed it into, and it passed
        straight through."""
        stage, root = _cabinet()
        table = UsdGeom.Cube.Define(stage, "/table")
        table.CreateSizeAttr(0.1)
        roots = rbd.read_articulations(root)

        model, data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[table.GetPrim()], floor_drop=2.0, free_root=False,
        )

        links = {"Body", "Door"}
        touching = {
            model.body(model.geom_bodyid[data.contact[i].geom1]).name
            for i in range(data.ncon)
        } | {
            model.body(model.geom_bodyid[data.contact[i].geom2]).name
            for i in range(data.ncon)
        }
        assert touching & links and touching - links - {"world"}

    def test_a_fixed_joint_adds_no_degree_of_freedom(self):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.Xform.Define(stage, "/root")
        body = _link(stage, "/root/Body")
        welded = _link(stage, "/root/Welded")
        _joint(stage, "/root/Welded/j", "PhysicsFixedJoint", body, welded)

        roots = rbd.read_articulations(stage.GetPrimAtPath("/root"))
        model, _data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=False,
        )

        assert mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Welded_joint") < 0


def _motor_cabinet(targets, *, stiffness=0.5, damping=0.05, max_force=None):
    """The cabinet with its door hinge driven: `targets` is {frame: degrees}, or one value
    for an unanimated target."""
    stage, root = _cabinet()
    hinge = stage.GetPrimAtPath("/root/Door/hinge")
    drive = UsdPhysics.DriveAPI.Apply(hinge, "angular")
    drive.CreateStiffnessAttr(stiffness)
    drive.CreateDampingAttr(damping)
    if max_force is not None:
        drive.CreateMaxForceAttr(max_force)
    attr = drive.CreateTargetPositionAttr()
    if isinstance(targets, dict):
        for frame, value in targets.items():
            attr.Set(float(value), Usd.TimeCode(frame))
    else:
        attr.Set(float(targets))
    return stage, root


def _hinge_degrees(results, frame):
    """The door's angle about the hinge (X) relative to the body, at a baked frame."""
    rel = results["Door"][frame] * results["Body"][frame].GetInverse()
    quat = rel.ExtractRotationQuat()
    return math.degrees(2.0 * math.atan2(quat.GetImaginary()[0], quat.GetReal()))


def _run_sim(roots, **kwargs):
    gen = rbd._sim_articulated_gen_mj(
        roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0, substeps=2,
        start_frame=1, dt=1 / 24.0, up_idx=2, solver_iters=20, collision_prims=[],
        floor_drop=2.0, free_root=False, **kwargs,
    )
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value


@needs_mujoco
class TestMotors:
    def test_a_drive_with_a_target_is_a_position_actuator(self):
        """USD's angular drive is per degree, MuJoCo's per radian: a stiffness passed
        through unscaled is 57 times too soft, and the arm sags rather than tracking."""
        stage, root = _motor_cabinet(-30.0, stiffness=2.0, damping=0.1, max_force=5.0)
        roots = rbd.read_articulations(root)

        model, _data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=False,
        )

        aid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, "Door_joint_motor")
        assert aid >= 0
        assert model.actuator_gainprm[aid][0] == pytest.approx(2.0 * 180.0 / math.pi)
        assert model.actuator_biasprm[aid][2] == pytest.approx(-0.1 * 180.0 / math.pi)
        assert list(model.actuator_forcerange[aid]) == pytest.approx([-5.0, 5.0])
        # The drive's stiffness is the motor's, not also a spring back to the rest pose.
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Door_joint")
        assert model.jnt_stiffness[jid] == 0.0

    def test_limits_are_measured_from_the_pose_the_model_is_built_in(self):
        """The model's zero is the posed links, so a joint posed to -80 degrees of a
        -90..0 limit has -10 left one way and 80 the other. Unshifted, a pose that jumped
        to another solution was clamped somewhere else entirely, its hand 28 cm off."""
        stage, root = _motor_cabinet(-80.0)
        roots = rbd.read_articulations(root)

        model, _data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=False,
        )

        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Door_joint")
        assert [math.degrees(v) for v in model.jnt_range[jid]] == pytest.approx([-10.0, 80.0], abs=0.01)

    def test_a_drive_without_a_target_stays_passive(self):
        stage, root = _cabinet()
        drive = UsdPhysics.DriveAPI.Apply(stage.GetPrimAtPath("/root/Door/hinge"), "angular")
        drive.CreateStiffnessAttr(2.0)
        roots = rbd.read_articulations(root)

        model, _data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=False,
        )

        assert model.nu == 0
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Door_joint")
        assert model.jnt_stiffness[jid] == pytest.approx(2.0)

    def test_the_joint_follows_an_animated_target(self):
        """The point of a motor: keyframed drive targets (Pose Articulation, inverse kinematics)
        come out of the solve as the same motion, with physics."""
        stage, root = _motor_cabinet({1: 0.0, 6: -60.0})
        roots = rbd.read_articulations(root)

        results = _run_sim(roots, frame_count=24)

        assert _hinge_degrees(results, 3) < -5.0  # moving while the target moves
        assert _hinge_degrees(results, 23) == pytest.approx(-60.0, abs=2.0)

    def test_a_target_is_measured_from_its_default(self):
        """Pose Articulation authors its pose into the link transforms AND the default
        target. The model is built from those transforms, so taken as absolute the target
        would turn the joint by the pose a second time."""
        stage, root = _motor_cabinet(-30.0)  # the default: the pose already in the links
        roots = rbd.read_articulations(root)

        results = _run_sim(roots, frame_count=12)

        assert _hinge_degrees(results, 11) == pytest.approx(0.0, abs=1.0)

    def _free_joint(self, stage, root):
        roots = rbd.read_articulations(root)
        model, _data, _ordered = rbd.build_articulated_spec(
            roots, gravity=-9.81, friction=0.7, restitution=0.0, mass=1.0,
            substeps=4, dt=1 / 24.0, up_idx=2, solver_iters=20,
            collision_prims=[], floor_drop=2.0, free_root=True,
        )
        return mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "Body_free") >= 0

    def test_an_anchored_robot_stays_on_its_mount(self):
        """Anchored and motorised is an arm bolted down: freed, it topples as it reaches."""
        stage, root = _motor_cabinet(-30.0)
        assert not self._free_joint(stage, root)

    def test_an_anchored_mechanism_without_motors_still_drops(self):
        """An appliance's sim-ready anchor is not honoured: the solver is asked to drop it."""
        stage, root = _cabinet()
        assert self._free_joint(stage, root)

    def test_a_robot_with_motors_and_no_anchor_is_free(self):
        """Something that walks keeps its free base."""
        stage, root = _motor_cabinet(-30.0)
        stage.RemovePrim("/root/Body/FixedJoint")
        assert self._free_joint(stage, root)

    def test_the_solve_starts_at_the_first_frames_target(self):
        """Not at the rest pose: an arm animated from mid-reach would snap there on frame 1."""
        stage, root = _motor_cabinet({1: -45.0, 10: -45.0})
        roots = rbd.read_articulations(root)

        results = _run_sim(roots, frame_count=4)

        assert _hinge_degrees(results, 0) == pytest.approx(-45.0, abs=0.5)
        assert _hinge_degrees(results, 3) == pytest.approx(-45.0, abs=2.0)


@needs_mujoco
class TestWriteBack:
    def _sim(self, stage, roots, **kwargs):
        gen = rbd._sim_articulated_gen_mj(
            roots, frame_count=6, gravity=-9.81, friction=0.7, restitution=0.0,
            mass=1.0, substeps=2, start_frame=1, dt=1 / 24.0, up_idx=2,
            solver_iters=20, collision_prims=[], floor_drop=2.0, **kwargs,
        )
        try:
            while True:
                next(gen)
        except StopIteration as exc:
            return exc.value

    def test_each_link_gets_its_own_baked_transform(self):
        stage, root = _cabinet()
        roots = rbd.read_articulations(root)
        results = self._sim(stage, roots)

        rbd._write_articulated_results(stage, roots, stage.GetSessionLayer(), results, 1)

        for path in ("/root/Body", "/root/Door"):
            ops = UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetOrderedXformOps()
            assert ops and len(ops[0].GetTimeSamples()) == 6

    def test_settle_writes_a_default_and_no_samples(self):
        """A prim carrying PhysicsRigidBodyAPI *and* animated transforms is contradictory:
        a downstream simulator ends up fighting the keyframes."""
        stage, root = _cabinet()
        roots = rbd.read_articulations(root)
        results = self._sim(stage, roots)

        rbd._write_articulated_results(stage, roots, stage.GetSessionLayer(), results, 1,
                                       settle_only=True)

        op = UsdGeom.Xformable(stage.GetPrimAtPath("/root/Body")).GetOrderedXformOps()[0]
        assert op.GetTimeSamples() == []
        assert op.Get() is not None

    def test_settle_only_keeps_stepping_past_frame_count_until_at_rest(self):
        """frame_count=1 is nowhere near enough for a falling+swinging door to stop moving.
        Before this fix, settle_only just grabbed whichever frame frame_count happened to
        land on -- correct only when the caller's frame_count was generous enough, which is
        exactly why "Settle To Rest" looked flaky rather than reliably broken. The generator
        must now keep stepping past frame_count on its own until the mechanism is actually
        at rest (or hits its safety cap), appending that pose as one extra frame."""
        stage, root = _cabinet()
        roots = rbd.read_articulations(root)

        gen = rbd._sim_articulated_gen_mj(
            roots, frame_count=1, gravity=-9.81, friction=0.7, restitution=0.0,
            mass=1.0, substeps=2, start_frame=1, dt=1 / 24.0, up_idx=2,
            solver_iters=20, collision_prims=[], floor_drop=2.0, settle_only=True,
        )
        try:
            while True:
                next(gen)
        except StopIteration as exc:
            results = exc.value

        for mats in results.values():
            assert len(mats) == 2  # the one preview frame, plus the appended settle frame

    def test_sample_times_are_not_rescaled_by_a_layer_offset(self):
        """USD maps sample times through a layer offset built from the ratio between the
        target layer's timeCodesPerSecond and the stage's. Writing frames 1..6 into a
        24-TCPS layer over a 60-TCPS stage lands them at 2.5..15 -- right values, wrong
        timing, surfacing as an animation that plays at the wrong speed."""
        stage, root = _cabinet()
        stage.GetRootLayer().timeCodesPerSecond = 60.0
        roots = rbd.read_articulations(root)
        results = self._sim(stage, roots)

        rbd._write_articulated_results(stage, roots, stage.GetSessionLayer(), results, 1)

        op = UsdGeom.Xformable(stage.GetPrimAtPath("/root/Body")).GetOrderedXformOps()[0]
        assert op.GetTimeSamples() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
