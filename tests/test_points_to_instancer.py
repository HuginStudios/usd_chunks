"""Points to Instancer copies an animated Points prim onto a PointInstancer of a mesh."""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks.chunk_script import Scene


def _points_stage() -> Usd.Stage:
    """Three frames of points: two at frame 1, three after, each fading by id."""
    stage = Usd.Stage.CreateInMemory()
    pts = UsdGeom.Points.Define(stage, "/fx/dust")
    opacity = UsdGeom.PrimvarsAPI(pts).CreatePrimvar(
        "displayOpacity", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
    colour = UsdGeom.PrimvarsAPI(pts).CreatePrimvar(
        "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex)
    for frame, n in ((1, 2), (2, 3), (3, 3)):
        pts.GetPointsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(i, frame, 0) for i in range(n)]), frame)
        pts.GetWidthsAttr().Set(Vt.FloatArray([0.2] * n), frame)
        pts.GetIdsAttr().Set(Vt.Int64Array(list(range(n))), frame)
        opacity.Set(Vt.FloatArray([0.5, 0.25, 0.125][:n]), frame)
        colour.Set(Vt.Vec3fArray([Gf.Vec3f(0.5, 0.4, 0.3)] * n), frame)
    return stage


@pytest.fixture
def scene():
    s = Scene(_points_stage(), "pytest")
    yield s
    s.close()


def test_every_frame_becomes_instances_of_a_mesh_sphere(scene):
    scene.add("points_to_instancer", prim_path="/fx/dust", primpath_location="/fx/dust_instanced")

    stage = scene.get_stage()
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/fx/dust_instanced"))
    assert inst
    assert inst.GetPositionsAttr().GetTimeSamples() == [1.0, 2.0, 3.0]
    assert list(inst.GetPositionsAttr().Get(2)) == [Gf.Vec3f(i, 2, 0) for i in range(3)]
    assert list(inst.GetIdsAttr().Get(3)) == [0, 1, 2]
    assert len(inst.GetProtoIndicesAttr().Get(1)) == 2
    # Aurora skips implicit shapes, so the prototype is a mesh, with a material bound.
    proto = stage.GetPrimAtPath(inst.GetPrototypesRel().GetTargets()[0])
    assert proto.IsA(UsdGeom.Mesh)
    assert UsdShade.MaterialBindingAPI(proto).ComputeBoundMaterial()[0]
    # The per-point primvars are carried over for the renderers that read them.
    assert len(UsdGeom.PrimvarsAPI(inst).GetPrimvar("displayOpacity").Get(2)) == 3


def test_instances_shrink_with_opacity_unless_switched_off(scene):
    node = scene.add("points_to_instancer", prim_path="/fx/dust", primpath_location="/fx/i")
    inst = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/fx/i"))
    # Width 0.2 times opacity over the brightest opacity (0.5).
    assert [round(s[0], 4) for s in inst.GetScalesAttr().Get(2)] == [0.2, 0.1, 0.05]

    node.get_param("shrink_with_opacity").value = False
    inst = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/fx/i"))
    assert [round(s[0], 4) for s in inst.GetScalesAttr().Get(2)] == [0.2, 0.2, 0.2]


def test_every_nth_keeps_the_same_points_by_id(scene):
    scene.add("points_to_instancer", prim_path="/fx/dust", primpath_location="/fx/i", every_nth=2)

    inst = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/fx/i"))
    assert list(inst.GetIdsAttr().Get(1)) == [0]
    assert list(inst.GetIdsAttr().Get(3)) == [0, 2]


def test_a_prototype_prim_replaces_the_sphere(scene):
    stage = scene.get_stage()
    UsdGeom.Cube.Define(stage, "/library/card")
    scene.add("points_to_instancer", prim_path="/fx/dust", primpath_location="/fx/i", proto_prim="/library/card")

    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/fx/i"))
    proto = stage.GetPrimAtPath(inst.GetPrototypesRel().GetTargets()[0])
    assert proto.IsA(UsdGeom.Cube)


def test_no_points_picked_writes_nothing(scene):
    node = scene.add("points_to_instancer", primpath_location="/fx/i")

    assert not scene.get_stage().GetPrimAtPath("/fx/i")
    assert node.get_param("readout").value == "pick a Points prim"
