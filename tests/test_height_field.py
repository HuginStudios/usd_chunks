"""Height Field paints a height band on meshes, and can put the band's faces in a subset."""
from __future__ import annotations

import numpy as np
import pytest
from pxr import Gf, Sdf, UsdGeom

from usd_chunks import Scene


def _slope(stage, path="/ground", up="Z"):
    """A strip of four quads climbing from height 0 to 8 along X: vertex heights 0, 2, 4, 6, 8."""
    UsdGeom.SetStageUpAxis(stage, up)
    mesh = UsdGeom.Mesh.Define(stage, path)
    pts = []
    for i in range(5):
        for side in (0.0, 1.0):
            height = 2.0 * i
            pts.append(Gf.Vec3f(i, side, height) if up == "Z" else Gf.Vec3f(i, height, side))
    mesh.CreatePointsAttr(pts)
    mesh.CreateFaceVertexCountsAttr([4] * 4)
    mesh.CreateFaceVertexIndicesAttr([v for i in range(4) for v in (2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1)])
    return mesh


def _values(stage, path="/ground", name="height_band"):
    return np.asarray(UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(path)).GetPrimvar(name).Get())[::2]


def test_inside_the_band_is_one_and_outside_is_zero():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        scene.create.height_field(prim_path="/ground", min_height=2.0, max_height=4.0, softness=0.0)

        assert _values(stage).tolist() == [0.0, 1.0, 1.0, 0.0, 0.0]


def test_softness_fades_out_beyond_the_band():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        scene.create.height_field(prim_path="/ground", min_height=4.0, max_height=4.0, softness=4.0)

        values = _values(stage)
        assert values[2] == pytest.approx(1.0)
        assert values[1] == pytest.approx(0.5) and values[3] == pytest.approx(0.5), "halfway out, smoothstep 0.5"
        assert values[0] == pytest.approx(0.0) and values[4] == pytest.approx(0.0)


def test_height_follows_the_stage_up_axis_and_the_transform():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage, up="Y")
        UsdGeom.Xformable(stage.GetPrimAtPath("/ground")).AddTranslateOp().Set(Gf.Vec3d(0, 2, 0))
        scene.create.height_field(prim_path="/ground", min_height=6.0, max_height=100.0, softness=0.0)

        assert _values(stage).tolist() == [0.0, 0.0, 1.0, 1.0, 1.0], "raised 2 up Y: heights 2 to 10"


def test_multiply_with_combines_another_field():
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = _slope(stage)
        UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("noise", Sdf.ValueTypeNames.FloatArray,
                                                 UsdGeom.Tokens.vertex).Set([0.5] * 10)
        scene.create.height_field(prim_path="/ground", base_primvar="noise", min_height=0.0, max_height=4.0,
                                  softness=0.0)

        assert _values(stage).tolist() == [0.5, 0.5, 0.5, 0.0, 0.0]


def test_face_subset_holds_the_faces_inside_the_band():
    """Face centres are at heights 1, 3, 5 and 7: a 2 to 6 band holds the middle two."""
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        node = scene.create.height_field(prim_path="/ground", min_height=2.0, max_height=6.0, subset="sand")

        subset = UsdGeom.Subset(stage.GetPrimAtPath("/ground/sand"))
        assert list(subset.GetIndicesAttr().Get()) == [1, 2]
        assert subset.GetElementTypeAttr().Get() == UsdGeom.Tokens.face
        assert subset.GetFamilyNameAttr().Get() == "materialBind"
        assert node.get_edit_context().GetPrimAtPath("/ground/sand"), "in the chunk's own layer"
        assert "2 faces in sand" in node.get_param("readout").value


def test_no_subset_unless_named():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        scene.create.height_field(prim_path="/ground")

        assert not stage.GetPrimAtPath("/ground").GetChildren()
