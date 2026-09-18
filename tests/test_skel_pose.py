"""Skel Pose holds named joints over an animation, as one sparse array edit.

A three-joint skeleton (hips -> arm -> hand) with a SkelAnimation that spins every joint
over frames 0..4 stands in for a walking character. Naming `arm` must loft one rotate
handle, write `rotations = edit [write q to [1]]` -- element 1, nothing else -- and leave
the hips and hand animating underneath. The handle's offset must sit on the joint, in the
SkelRoot's space, since joints are not prims and cannot anchor a gizmo themselves.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdSkel, Vt

from usd_chunks import chunk as _chunk
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import export_chunks, get_chunk_prim_path
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

JOINTS = ["hips", "hips/arm", "hips/arm/hand"]


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _make_scene(stage: Usd.Stage) -> SceneSender:
    class _SyncScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _SyncScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return scene


def _build_character(stage: Usd.Stage, skel_rotate_x: float = 0.0) -> None:
    """A SkelRoot at /Char, its skeleton's rest pose a straight arm out along +X.

    `skel_rotate_x` puts a transform on the Skeleton prim itself, so skeleton space and
    SkelRoot space stop coinciding.
    """
    root = UsdSkel.Root.Define(stage, "/Char")
    UsdGeom.Xformable(root).AddTranslateOp().Set((100.0, 0.0, 0.0))
    skel = UsdSkel.Skeleton.Define(stage, "/Char/Skel")
    if skel_rotate_x:
        UsdGeom.Xformable(skel).AddRotateXOp().Set(skel_rotate_x)
    skel.GetJointsAttr().Set(JOINTS)
    rest = Vt.Matrix4dArray([
        Gf.Matrix4d(1.0),
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(10, 0, 0)),
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(10, 0, 0)),
    ])
    skel.GetRestTransformsAttr().Set(rest)
    skel.GetBindTransformsAttr().Set(Vt.Matrix4dArray([
        Gf.Matrix4d(1.0),
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(10, 0, 0)),
        Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(20, 0, 0)),
    ]))
    anim = UsdSkel.Animation.Define(stage, "/Char/Anim")
    anim.GetJointsAttr().Set(JOINTS)
    anim.GetTranslationsAttr().Set(Vt.Vec3fArray([(0, 0, 0), (10, 0, 0), (10, 0, 0)]))
    anim.GetScalesAttr().Set(Vt.Vec3hArray([(1, 1, 1)] * 3))
    for f in range(5):
        spin = Gf.Rotation(Gf.Vec3d(0, 0, 1), 10.0 * f).GetQuat()
        q = Gf.Quatf(spin.GetReal(), Gf.Vec3f(spin.GetImaginary()))
        anim.GetRotationsAttr().Set(Vt.QuatfArray([q, q, q]), f)
    binding = UsdSkel.BindingAPI.Apply(root.GetPrim())
    binding.CreateSkeletonRel().SetTargets([skel.GetPath()])
    binding.CreateAnimationSourceRel().SetTargets([anim.GetPath()])


def _make_chunk(scene):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("rigs.skel_pose")
    chunks = mod.create() if mod.loaded else mod.load()
    node = chunks[0]
    scene.add_chunk(node)
    return node, mod.module


def _vectors(node):
    return sorted(p.name for p in node.get_my_params() if isinstance(p, _chunk.Vector))


@pytest.fixture
def posed():
    stage = Usd.Stage.CreateInMemory()
    _build_character(stage)
    scene = _make_scene(stage)
    node, module = _make_chunk(scene)
    node.get_param("primpath_location").value = "/Char"
    node.get_param("joints").value = "arm"
    return stage, node, module


class TestLofting:
    def test_one_handle_per_matched_joint(self, posed):
        _, node, _ = posed
        assert _vectors(node) == ["arm"]
        node.get_param("joints").value = "h*"
        assert _vectors(node) == ["hand", "hips"], "leaf patterns match leaves, not paths under hips"
        node.get_param("joints").value = "hips/*"
        assert _vectors(node) == ["arm", "hand"], "a pattern with a slash matches the path, any depth"

    def test_narrowing_removes_the_handle_and_its_gizmo(self, posed):
        stage, node, _ = posed
        node.get_param("joints").value = "arm, hand"
        prim = stage.GetPrimAtPath(get_chunk_prim_path(node))
        assert prim.GetAttribute("ui:gizmo:hand:type").Get() == "rotate"
        node.get_param("joints").value = "arm"
        assert _vectors(node) == ["arm"]
        assert not prim.GetAttribute("ui:gizmo:hand:type")
        assert prim.GetAttribute("ui:gizmo:arm:type").Get() == "rotate"

    def test_gizmo_sits_on_the_joint_in_the_roots_space(self, posed):
        stage, node, module = posed
        # The arm joint rests 10 along X in skeleton space, and the skeleton sits at the
        # SkelRoot's origin -- so the handle offset is (10, 0, 0), not the root's world 110.
        assert module.gizmo_offset(node, "arm") == pytest.approx((10.0, 0.0, 0.0))
        prim = stage.GetPrimAtPath(get_chunk_prim_path(node))
        assert list(prim.GetRelationship("ui:gizmo:arm:anchor").GetTargets()) == [Sdf.Path("/Char")]


class TestPose:
    def test_writes_one_sparse_edit_and_leaves_the_rest_animating(self, posed):
        stage, node, _ = posed
        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        before = rotations.Get(2)

        node.get_param("arm").value = (0.0, 0.0, 90.0)
        node.execute()

        text = node.get_edit_context().ExportToString()
        assert "rotations = edit [write" in text and "to [1]]" in text

        after = rotations.Get(2)
        assert after[0] == before[0] and after[2] == before[2]
        assert after[1] != before[1]
        assert rotations.Get(3)[0] != rotations.Get(2)[0], "hips still spin underneath"
        assert rotations.Get(3)[1] == rotations.Get(2)[1], "the arm is held"

    def test_zero_rotation_writes_nothing(self, posed):
        _, node, _ = posed
        node.execute()
        assert "rotations" not in node.get_edit_context().ExportToString()

    def test_pose_is_absolute_not_cumulative(self, posed):
        stage, node, _ = posed
        rotations = stage.GetAttributeAtPath("/Char/Anim.rotations")
        node.get_param("arm").value = (0.0, 0.0, 90.0)
        node.execute()
        once = rotations.Get(2)[1]
        node.execute()
        assert rotations.Get(2)[1] == once

    def test_rotation_is_in_the_characters_space_about_the_joint(self, posed):
        stage, node, _ = posed
        node.get_param("arm").value = (0.0, 0.0, 90.0)
        node.execute()
        # The rest arm points along +X from the arm joint; turned 90 about Z it points +Y.
        # Read the skinning result the way a mesh would: the joint's skel-space transform.
        cache = UsdSkel.Cache()
        root = UsdSkel.Root(stage.GetPrimAtPath("/Char"))
        cache.Populate(root, Usd.PrimDefaultPredicate)
        query = cache.GetSkelQuery(UsdSkel.Skeleton(stage.GetPrimAtPath("/Char/Skel")))
        xforms = query.ComputeJointSkelTransforms(Usd.TimeCode(0))
        hand = xforms[2].ExtractTranslation()
        assert hand == pytest.approx((10.0, 10.0, 0.0), abs=1e-4)


    def test_rotation_is_in_the_roots_space_even_when_the_skeleton_is_not(self):
        # The skeleton is turned 90 about X under the root, so its Z is the root's -Y. A
        # 90 turn about the *root's* Z must still swing the hand from root +X to root +Y;
        # applying the delta in skeleton space would swing it out of the XY plane.
        stage = Usd.Stage.CreateInMemory()
        _build_character(stage, skel_rotate_x=90.0)
        scene = _make_scene(stage)
        node, module = _make_chunk(scene)
        node.get_param("primpath_location").value = "/Char"
        node.get_param("joints").value = "arm"
        node.get_param("arm").value = (0.0, 0.0, 90.0)
        node.execute()
        cache = UsdSkel.Cache()
        root = UsdSkel.Root(stage.GetPrimAtPath("/Char"))
        cache.Populate(root, Usd.PrimDefaultPredicate)
        query = cache.GetSkelQuery(UsdSkel.Skeleton(stage.GetPrimAtPath("/Char/Skel")))
        skel_to_root = UsdGeom.Xformable(stage.GetPrimAtPath("/Char/Skel")).GetLocalTransformation()
        hand = skel_to_root.Transform(query.ComputeJointSkelTransforms(Usd.TimeCode(0))[2].ExtractTranslation())
        assert hand == pytest.approx((10.0, 10.0, 0.0), abs=1e-4)
        assert module.gizmo_offset(node, "arm") == pytest.approx((10.0, 0.0, 0.0), abs=1e-6)

    def test_the_rig_is_read_once_per_target(self, posed):
        _, node, module = posed
        first = module._rig(node)
        assert module._rig(node) is first
        node.get_param("primpath_location").value = "/Char/Skel"
        assert module._rig(node) is not first, "re-targeting drops the cache"
        assert _vectors(node) == ["arm"], "and anything under the root finds the same rig"


class TestReload:
    def test_pose_survives_a_save_and_reload(self, posed, tmp_path):
        stage, node, _ = posed
        node.get_param("arm").value = (0.0, 0.0, 90.0)
        node.execute()
        held = stage.GetAttributeAtPath("/Char/Anim.rotations").Get(2)[1]

        path = str(tmp_path / "pose.usda")
        export_chunks(node.get_scene(), path)
        # export_chunks saves the chunks, not the scene: the character comes back the way
        # it would in production, as a layer the reopened scene sublayers.
        character = str(tmp_path / "char.usda")
        stage.GetRootLayer().Export(character)
        stage2 = Usd.Stage.Open(path)
        stage2.GetRootLayer().subLayerPaths.append(character)
        scene2 = _make_scene(stage2)
        reloaded = [c for c in scene2.get_chunks()
                    if c.get_config() and c.get_config().module == "skel_pose"][0]
        assert _vectors(reloaded) == ["arm"]
        assert tuple(reloaded.get_param("arm").value) == pytest.approx((0.0, 0.0, 90.0))
        assert stage2.GetAttributeAtPath("/Char/Anim.rotations").Get(2)[1] == held
