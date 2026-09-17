"""Pose Articulation authors a SHARE of each posed link's transform, not the absolute
placement -- composing with whatever the exec graph places upstream (URDF Import's rest
pose, say) and downstream (an RBD Solver bake) of it, via chunk_utils.xform_op_name +
chunk_gizmo.xform_op_order, the same convention every other transform-authoring chunk in
this codebase already uses (see test_rbd_bake_composes.py for the sibling case).

Reported live: with an RBD Solver wired downstream and already having baked once, posing a
joint again corrupted the whole arm's placement. pose_articulation used to author the bare
"xformOp:transform" and hard-replace the WHOLE xformOpOrder with just that op -- so its own
read of "the current rest pose" (GetLocalTransformation()) silently included whatever RBD's
own (untracked, still-composing) bake had already contributed, baking it a second time into
pose's own "absolute" answer, while RBD's stale op stayed in its own unchanged, stronger
layer and got applied ON TOP a second time. Confirmed reproducible by switching a view away
and back (forces a full recook that happens to settle it) or by changing the pose again
(does not).
"""
from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

from usd_chunks import chunk_gizmo, chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.chunk_registry import ChunkLayerManager
from usd_chunks import chunk_stage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "rigs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "dynamics"))
pose_mod = importlib.import_module("pose_articulation")
rbd_mod = importlib.import_module("rbd_solver")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _link(stage, path):
    prim = UsdGeom.Xform.Define(stage, path).GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(prim)
    cube = UsdGeom.Cube.Define(stage, path + "/Collisions/C001")
    cube.CreatePurposeAttr(UsdGeom.Tokens.guide)
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return prim


def _joint(stage, path, parent, child, *, axis="Y", pos=(0.0, 0.0, 0.0)):
    prim = stage.DefinePrim(path, "PhysicsRevoluteJoint")
    prim.CreateRelationship("physics:body0").SetTargets([parent.GetPath()])
    prim.CreateRelationship("physics:body1").SetTargets([child.GetPath()])
    prim.CreateAttribute("physics:axis", Sdf.ValueTypeNames.Token).Set(axis)
    prim.CreateAttribute("physics:localPos1", Sdf.ValueTypeNames.Point3f).Set(Gf.Vec3f(*pos))
    prim.CreateAttribute("physics:lowerLimit", Sdf.ValueTypeNames.Float).Set(-90.0)
    prim.CreateAttribute("physics:upperLimit", Sdf.ValueTypeNames.Float).Set(90.0)
    return prim


def _build_arm(stage):
    """Two links flat under a common root -- exactly how urdf_import authors a mechanism
    (siblings, not nested), with the rest pose living in a plain translate op nobody's
    chunk owns, untracked, same as urdf_import.py's own translate/orient. `prim_path` for
    both Pose Articulation and RBD Solver is the containing root, not either link -- that's
    what read_articulations walks to find the flat tree."""
    UsdGeom.Xform.Define(stage, "/root")
    base = _link(stage, "/root/base")
    upper = _link(stage, "/root/upper")
    UsdGeom.Xformable(upper).AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 0.15))
    _joint(stage, "/root/upper/shoulder", base, upper, axis="Y")
    return base, upper


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        _build_arm(stage)

    sender = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(sender, "pytest")
    ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)
    return stage, sender


def _mk(sender, module, name, module_name):
    node = module.main()
    node._config = ChunkConfig(module=module_name)
    node._module = module
    node.name = name
    sender.add_chunk(node)
    return node


def _world(stage, path):
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    return cache.GetLocalToWorldTransform(stage.GetPrimAtPath(path))


class TestPosingAlone:
    def test_rotating_about_its_own_origin_leaves_translation_unchanged(self, scene):
        """The joint's pivot is the child link's own origin here, so a pure rotation must
        not move it -- only reorient it."""
        stage, sender = scene
        pose = _mk(sender, pose_mod, "Pose", "pose_articulation")
        pose.get_param("prim_path").value = "/root"
        pose.execute()

        pose.get_param("upper").value = 90.0
        pose.execute()

        world = _world(stage, "/root/upper")
        assert world.ExtractTranslation() == pytest.approx(Gf.Vec3d(0, 0, 0.15), abs=1e-6)

    def test_reposing_repeatedly_does_not_accumulate(self, scene):
        """execute() clears its own layer first specifically so a pose is absolute, not
        cumulative -- posing to 90 twice must land at 90, not 180."""
        stage, sender = scene
        pose = _mk(sender, pose_mod, "Pose", "pose_articulation")
        pose.get_param("prim_path").value = "/root"
        pose.execute()

        pose.get_param("upper").value = 90.0
        pose.execute()
        first = _world(stage, "/root/upper").ExtractRotation().GetAngle()

        pose.execute()
        second = _world(stage, "/root/upper").ExtractRotation().GetAngle()

        assert second == pytest.approx(first, abs=1e-6)

    def test_the_rest_pose_untracked_ops_survive(self, scene):
        """The link's own translate op (nobody's tracked op, same as urdf_import's) must
        still be in the composed order after posing -- pose owns a share, not the whole
        answer."""
        stage, sender = scene
        pose = _mk(sender, pose_mod, "Pose", "pose_articulation")
        pose.get_param("prim_path").value = "/root"
        pose.execute()
        pose.get_param("upper").value = 45.0
        pose.execute()

        order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/root/upper"))
                     .GetXformOpOrderAttr().Get())
        assert "xformOp:translate" in order
        assert chunk_utils.xform_op_name(pose) in order


class TestPosingAlongsideADownstreamWriter:
    """The regression: a chunk wired downstream of Pose Articulation (an RBD Solver bake,
    the reported case) that also owns an op on the same prim."""

    def _wire_solver_with_a_stale_bake(self, stage, sender, pose, share):
        """Author what an RBD Solver bake writes, without running MuJoCo -- matching
        test_rbd_bake_composes.py's own approach: what's under test is composition, not
        physics."""
        solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
        solver.get_param("prim_path").value = "/root"
        solver.add_dependency(pose)

        upper = stage.GetPrimAtPath("/root/upper")
        my_op = chunk_utils.xform_op_name(solver)
        order = chunk_gizmo.xform_op_order(solver, upper, my_op)
        UsdGeom.Xformable(upper).GetXformOpOrderAttr().Set(order)
        attr = upper.CreateAttribute(my_op, Sdf.ValueTypeNames.Matrix4d, custom=False)
        attr.Set(share)
        return solver

    def test_posing_again_after_a_downstream_bake_stays_finite(self, scene):
        """The reported symptom: the whole arm collapsing. At minimum, the result must not
        explode into an unbounded or non-finite transform."""
        stage, sender = scene
        pose = _mk(sender, pose_mod, "Pose", "pose_articulation")
        pose.get_param("prim_path").value = "/root"
        pose.execute()
        pose.get_param("upper").value = 45.0
        pose.execute()

        share = Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0, 0, -1.0))
        self._wire_solver_with_a_stale_bake(stage, sender, pose, share)

        # The reported trigger: pose again, with the solver's stale share still authored.
        pose.get_param("upper").value = 10.0
        pose.execute()

        t = _world(stage, "/root/upper").ExtractTranslation()
        assert all(math.isfinite(v) for v in t)
        assert Gf.Vec3d(*t).GetLength() < 10.0, (
            "translation exploded far beyond anything a -1m share plus a 0.15m rest pose "
            "could produce -- the old bug folded the downstream share into pose's own "
            "answer and then applied it again on top"
        )

    def test_posing_again_composes_with_the_downstream_share_exactly_once(self, scene):
        """The precise claim: re-posing with a downstream share present must equal that
        share applied ONCE on top of what posing alone (no downstream chunk at all) would
        produce for the same joint value -- not zero times (share lost) and not twice
        (the reported bug)."""
        stage, sender = scene
        pose = _mk(sender, pose_mod, "Pose", "pose_articulation")
        pose.get_param("prim_path").value = "/root"
        pose.execute()
        pose.get_param("upper").value = 45.0
        pose.execute()

        share = Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0, 0, -1.0))
        self._wire_solver_with_a_stale_bake(stage, sender, pose, share)

        pose.get_param("upper").value = 10.0
        pose.execute()
        with_solver = _world(stage, "/root/upper")

        # Oracle: the same joint value, posed with no downstream chunk in the picture at all.
        stage2 = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage2, UsdGeom.Tokens.z)
        scene_layer = Sdf.Layer.CreateAnonymous("scene2")
        stage2.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
        with Usd.EditContext(stage2, scene_layer):
            _build_arm(stage2)
        sender2 = _Scene(stage2)
        chunk_registry.ChunkRegistry().initialize(sender2, "pytest2")
        ChunkLayerManager().initialize(stage2)
        chunk_stage.ChunkStageSync().initialize(stage2, sender2)
        pose2 = _mk(sender2, pose_mod, "Pose", "pose_articulation")
        pose2.get_param("prim_path").value = "/root"
        pose2.execute()
        pose2.get_param("upper").value = 10.0
        pose2.execute()
        pose_only = _world(stage2, "/root/upper")

        expected = share * pose_only
        for row_a, row_b in zip(with_solver, expected):
            for a, b in zip(row_a, row_b):
                assert a == pytest.approx(b, abs=1e-6)
