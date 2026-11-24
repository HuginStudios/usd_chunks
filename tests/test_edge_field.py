"""Edge Field paints a mesh's sharp edges (outer) or corners (inner), spread by Width."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from pxr import Gf, Sdf, UsdGeom, Vt

from usd_chunks import Scene

_spec = importlib.util.spec_from_file_location(
    "edge_field_module", Path(__file__).resolve().parents[1] / "chunks" / "modelling" / "edge_field.py")
edge_field = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(edge_field)

# A step in X/Z, half a unit per segment: a floor at height 0 (x 0..2), a wall up to 2 at x = 2,
# and a top at height 2 (x 2..4). Where the floor meets the wall is an inner corner; where the
# wall meets the top is an outer edge.
_PROFILE = ([(x, 0.0) for x in (0.0, 0.5, 1.0, 1.5)] + [(2.0, z) for z in (0.0, 0.5, 1.0, 1.5)]
            + [(x, 2.0) for x in (2.0, 2.5, 3.0, 3.5, 4.0)])
_INNER, _OUTER = 4, 8  # the profile points on the corner and on the edge


def _step(stage, path="/step"):
    """The step as a mesh one unit deep in Y. Point i of the profile is points 2i (y 0) and
    2i + 1 (y 1); faces are wound so their normals face up and toward the floor."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr([Gf.Vec3f(x, y, z) for x, z in _PROFILE for y in (0.0, 1.0)])
    n = len(_PROFILE) - 1
    mesh.CreateFaceVertexCountsAttr([4] * n)
    mesh.CreateFaceVertexIndicesAttr([v for i in range(n) for v in (2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1)])
    return mesh


def _values(stage, path="/step", name="edges"):
    """The field at each profile point (the y = 0 row)."""
    return np.asarray(UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(path)).GetPrimvar(name).Get())[::2]


def _cube():
    pts = [(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return pts, [4] * 6, [v for f in faces for v in f]


def test_a_cube_has_twelve_outward_edges_at_ninety_degrees():
    a, b, angle, outward = edge_field.sharp_edges(*_cube())
    assert len(a) == 12
    assert angle == pytest.approx(np.full(12, 90.0))
    assert outward.all()
    _, _, _, inside_out = edge_field.sharp_edges(*_cube(), left_handed=True)
    assert not inside_out.any(), "left-handed winding turns the faces inside out"


@pytest.mark.parametrize(("find", "marked"), [
    ("Outer Edges", {_OUTER}), ("Inner Corners", {_INNER}), ("Both", {_INNER, _OUTER})])
def test_find_picks_outer_edges_inner_corners_or_both(find, marked):
    with Scene() as scene:
        stage = scene.get_stage()
        _step(stage)
        scene.create.edge_field(prim_path="/step", find=find, width=0.0)

        values = _values(stage)
        assert np.flatnonzero(values).tolist() == sorted(marked)
        assert values[sorted(marked)] == pytest.approx(1.0)


def test_width_fades_the_field_out_across_the_faces():
    with Scene() as scene:
        stage = scene.get_stage()
        _step(stage)
        scene.create.edge_field(prim_path="/step", width=1.0)

        values = _values(stage)
        assert values[_OUTER] == pytest.approx(1.0)
        assert values[_OUTER + 1] == pytest.approx(0.5, abs=1e-6), "half a unit along the top: smoothstep 0.5"
        assert values[_OUTER - 1] == pytest.approx(0.5, abs=1e-6), "and half a unit down the wall"
        assert values[_OUTER + 2] == pytest.approx(0.0), "a full Width away"
        assert values[:_OUTER - 1] == pytest.approx(0.0), "the inner corner isn't asked for"


def test_angle_and_softness_decide_how_sharp_an_edge_must_be():
    angle = np.array([0.0, 10.0, 30.0, 50.0, 90.0])
    out = np.ones(5, dtype=bool)
    hard = edge_field.edge_strength(angle, out, find="Outer Edges", min_angle=40.0, softness=0.0)
    assert hard.tolist() == [0.0, 0.0, 0.0, 1.0, 1.0]
    soft = edge_field.edge_strength(angle, out, find="Outer Edges", min_angle=40.0, softness=20.0)
    assert soft.tolist() == pytest.approx([0.0, 0.0, 0.5, 1.0, 1.0])
    with Scene() as scene:
        stage = scene.get_stage()
        _step(stage)
        scene.create.edge_field(prim_path="/step", angle=100.0, softness=0.0, width=0.0)

        assert not _values(stage).any(), "the step's 90 degree edge is below 100"


def test_a_mirroring_transform_keeps_outer_edges_outer():
    with Scene() as scene:
        stage = scene.get_stage()
        _step(stage)
        UsdGeom.Xformable(stage.GetPrimAtPath("/step")).AddScaleOp().Set(Gf.Vec3f(-1, 1, 1))
        scene.create.edge_field(prim_path="/step", width=0.0)

        assert np.flatnonzero(_values(stage)).tolist() == [_OUTER]


def test_multiply_with_show_field_and_the_readout():
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = _step(stage)
        UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("noise", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex).Set(
            Vt.FloatArray([0.5] * len(_PROFILE) * 2))
        field = scene.create.edge_field(prim_path="/step", base_primvar="noise", width=0.0, show_field=True)

        values = _values(stage)
        assert values[_OUTER] == pytest.approx(0.5)
        colours = np.asarray(UsdGeom.Gprim(stage.GetPrimAtPath("/step")).GetDisplayColorAttr().Get())
        assert len(colours) == len(_PROFILE) * 2
        assert field.get_param("readout").value.startswith("primvars:edges on 1 mesh · 1 edge")


def test_nothing_to_paint_reports_instead_of_raising():
    with Scene() as scene:
        field = scene.create.edge_field()
        assert field.get_param("readout").value == "set the meshes to paint"
        field.get_param("prim_path").value = "/nowhere"
        assert field.get_param("readout").value == "no meshes found to paint"


def test_faces_with_their_own_corners_still_share_edges():
    """Many meshes give each face its own points (hard edges, UV seams): welded, they meet."""
    with Scene() as scene:
        stage = scene.get_stage()
        mesh = UsdGeom.Mesh.Define(stage, "/step")
        pts, n = [], len(_PROFILE) - 1
        for i in range(n):
            (x0, z0), (x1, z1) = _PROFILE[i], _PROFILE[i + 1]
            pts += [Gf.Vec3f(x0, 0, z0), Gf.Vec3f(x1, 0, z1), Gf.Vec3f(x1, 1, z1), Gf.Vec3f(x0, 1, z0)]
        mesh.CreatePointsAttr(pts)
        mesh.CreateFaceVertexCountsAttr([4] * n)
        mesh.CreateFaceVertexIndicesAttr(list(range(4 * n)))
        field = scene.create.edge_field(prim_path="/step", width=0.0)

        values = np.asarray(UsdGeom.PrimvarsAPI(mesh).GetPrimvar("edges").Get())
        on_edge = [k for k, p in enumerate(pts) if (p[0], p[2]) == _PROFILE[_OUTER]]
        assert len(on_edge) == 4, "the edge's two corners, once in each face"
        assert np.flatnonzero(values).tolist() == on_edge
        assert "1 edge" in field.get_param("readout").value
