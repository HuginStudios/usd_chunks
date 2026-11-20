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


def test_a_changed_band_repaints_at_once():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        node = scene.create.height_field(prim_path="/ground", min_height=0.0, max_height=2.0, softness=0.0)
        assert _values(stage).tolist() == [1.0, 1.0, 0.0, 0.0, 0.0]

        node.get_param("max_height").value = 6.0

        assert _values(stage).tolist() == [1.0, 1.0, 1.0, 1.0, 0.0]


def test_a_height_past_the_slider_range_is_kept():
    """A centimetre scene's heights run into thousands, past the sliders' 500."""
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = _slope(stage)
        UsdGeom.Xformable(mesh).AddScaleOp().Set(Gf.Vec3f(1000.0))
        node = scene.create.height_field(prim_path="/ground", min_height=3000.0, max_height=5000.0, softness=0.0)

        assert node.get_param("min_height").value == pytest.approx(3000.0)
        assert _values(stage).tolist() == [0.0, 0.0, 1.0, 0.0, 0.0], "heights 0 to 8000: only 4000 is in"


def test_min_above_max_is_read_the_other_way_round():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        scene.create.height_field(prim_path="/ground", min_height=4.0, max_height=2.0, softness=0.0)

        assert _values(stage).tolist() == [0.0, 1.0, 1.0, 0.0, 0.0]


def test_a_face_varying_primvar_is_not_multiplied_in(caplog):
    """One value per face corner is not one per point: taking the first ones would be wrong."""
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = _slope(stage)
        UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("noise", Sdf.ValueTypeNames.FloatArray,
                                                 UsdGeom.Tokens.faceVarying).Set([0.0] * 16)
        scene.create.height_field(prim_path="/ground", base_primvar="noise", min_height=0.0, max_height=4.0,
                                  softness=0.0)

        assert _values(stage).tolist() == [1.0, 1.0, 1.0, 0.0, 0.0]
        assert "not multiplied in" in caplog.text


def test_a_constant_primvar_scales_every_point():
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = _slope(stage)
        UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("noise", Sdf.ValueTypeNames.Float,
                                                 UsdGeom.Tokens.constant).Set(0.25)
        scene.create.height_field(prim_path="/ground", base_primvar="noise", min_height=0.0, max_height=100.0)

        assert _values(stage).tolist() == [0.25] * 5


def test_a_face_with_no_corners_is_skipped():
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = _slope(stage)
        mesh.GetFaceVertexCountsAttr().Set([4, 0, 4, 4, 4])
        scene.create.height_field(prim_path="/ground", min_height=2.0, max_height=6.0, subset="band")

        assert list(UsdGeom.Subset(stage.GetPrimAtPath("/ground/band")).GetIndicesAttr().Get()) == [2, 3]


def test_show_field_tints_the_band_and_unticking_puts_the_look_back():
    with Scene() as scene:
        stage = scene.get_stage()
        _slope(stage)
        node = scene.create.height_field(prim_path="/ground", min_height=0.0, max_height=0.0, softness=0.0,
                                         show_field=True)
        colours = UsdGeom.Mesh(stage.GetPrimAtPath("/ground")).GetDisplayColorAttr().Get()
        assert tuple(colours[0]) == pytest.approx((0.15, 0.55, 1.0)), "inside the band: the field colour"
        assert tuple(colours[-1]) == pytest.approx((0.5, 0.5, 0.5)), "outside: the mesh's own grey"

        node.get_param("show_field").value = False

        assert not UsdGeom.Mesh(stage.GetPrimAtPath("/ground")).GetDisplayColorAttr().HasAuthoredValue()


def test_a_saved_network_paints_the_same_when_reopened(tmp_path):
    path = tmp_path / "net.usda"
    with Scene.new(path=str(path)) as scene:
        last = scene.create.mesh(prim_path="/ground", mesh_type="grid", dimx1=4, dimy1=4)
        last = scene.create.transform(last, primpath_location="/ground", rx=30.0)
        scene.create.height_field(last, name="Band", prim_path="/ground", min_height=0.0, max_height=0.2,
                                  subset="low")
        before = list(UsdGeom.PrimvarsAPI(scene.get_stage().GetPrimAtPath("/ground")).GetPrimvar("height_band").Get())
        scene.save()

    with Scene.open(str(path)) as scene:
        stage = scene.get_stage()
        after = list(UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/ground")).GetPrimvar("height_band").Get())
        assert after == pytest.approx(before) and 0 < sum(after) < len(after)
        assert len(UsdGeom.Subset(stage.GetPrimAtPath("/ground/low")).GetIndicesAttr().Get())
