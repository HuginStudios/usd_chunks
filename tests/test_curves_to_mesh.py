"""The sweep: does the tube sit on the curve, and are its UVs usable.

The motivating measurement, for anyone wondering why this chunk exists at all: rendering the
same BasisCurves with and without a `primvars:st`, through the three delegates in the 26.05
bundle, Storm's two images differ and V-Ray's and Cycles' are pixel-identical. Curves cannot
be textured there. A Mesh with st can, V-Ray included.
"""

from __future__ import annotations

import importlib
import math

import numpy as np
import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")
ctm = importlib.import_module("scripts.modelling.curves_to_mesh")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _line(n=8, length=7.0):
    """A straight run along +X, which makes every expectation checkable by hand."""
    return np.stack([
        np.linspace(0.0, length, n),
        np.zeros(n),
        np.zeros(n),
    ], axis=1)


def _arc(n=24, radius=5.0):
    """A quarter turn in XY — a curve with real curvature for the frame to cope with."""
    t = np.linspace(0.0, math.pi / 2.0, n)
    return np.stack([np.cos(t) * radius, np.sin(t) * radius, np.zeros(n)], axis=1)


def _sweep_one(points, radius=0.5, sides=6, uv_mode=ctm._UV_PER_STRAND, tiling=1.0,
               kind=UsdGeom.Tokens.linear):
    # build_mesh takes radii; _read_strands is what halves authored widths into them.
    radii = np.full(len(points), radius)
    return ctm.build_mesh([(points, radii)], kind, UsdGeom.Tokens.bspline,
                          sides, 4, uv_mode, tiling, 1.0)


def _curves_prim(stage, path="/World/Curves", points=None, widths=None,
                 kind=UsdGeom.Tokens.linear):
    points = _line() if points is None else points
    curves = UsdGeom.BasisCurves.Define(stage, path)
    curves.CreateTypeAttr().Set(kind)
    if kind == UsdGeom.Tokens.cubic:
        curves.CreateBasisAttr().Set(UsdGeom.Tokens.bspline)
    curves.CreateWrapAttr().Set(UsdGeom.Tokens.nonperiodic)
    curves.CreateCurveVertexCountsAttr().Set([len(points)])
    curves.CreatePointsAttr().Set([Gf.Vec3f(*p) for p in points])
    curves.CreateWidthsAttr().Set(widths or [1.0] * len(points))
    curves.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
    return curves


# ---------------------------------------------------------------------------
# the tube is where the curve is
# ---------------------------------------------------------------------------

def test_a_swept_tube_surrounds_the_line_it_came_from() -> None:
    built = _sweep_one(_line(), radius=0.5)
    points = built[0]

    assert points is not None and len(points)
    # Every vertex is exactly one radius from the axis it was swept along.
    distance = np.linalg.norm(points[:, 1:], axis=1)
    assert distance == pytest.approx(0.5, abs=1e-6)
    # And it spans the line's own length, no further.
    assert points[:, 0].min() == pytest.approx(0.0, abs=1e-6)
    assert points[:, 0].max() == pytest.approx(7.0, abs=1e-6)


def test_a_cubic_curve_is_evaluated_not_swept_along_its_control_points() -> None:
    """A B-spline does not pass through its CVs. Sweeping the hull directly puts the mesh
    beside the curve the renderer draws -- worst at the ends, which is where Vine pins its
    roots. The give-away is the span: the evaluated centreline of an unpinned spline is
    strictly shorter than its control polygon."""
    points = _line(n=8, length=7.0)
    built = _sweep_one(points, radius=0.1, kind=UsdGeom.Tokens.cubic)

    span = built[0][:, 0].max() - built[0][:, 0].min()
    assert span < 7.0 - 0.5, "the hull was swept verbatim; the basis was never evaluated"
    assert span > 3.0, "evaluation collapsed the curve"


def test_a_pinned_cubic_curve_reaches_its_ends() -> None:
    """Vine triples its end control points so the spline reaches where the vine grew. The
    sweep has to reproduce that, or every vine lifts off the wall at the root."""
    line = _line(n=6, length=5.0)
    pinned = np.concatenate([line[:1], line[:1], line, line[-1:], line[-1:]])
    built = _sweep_one(pinned, radius=0.1, kind=UsdGeom.Tokens.cubic)

    assert built[0][:, 0].min() == pytest.approx(0.0, abs=1e-3)
    assert built[0][:, 0].max() == pytest.approx(5.0, abs=1e-3)


# ---------------------------------------------------------------------------
# UVs
# ---------------------------------------------------------------------------

def test_u_runs_once_around_the_tube_without_a_reversed_seam() -> None:
    """The seam ring is duplicated so u can run a clean 0..1. Sharing one vertex there would
    make it hold both ends at once and the texture would run backwards down that column."""
    sides = 8
    built = _sweep_one(_line(), sides=sides)
    st = built[2]

    ring = st[:sides + 1]
    assert ring[:, 0] == pytest.approx(np.linspace(0.0, 1.0, sides + 1))


def test_v_runs_nose_to_tail_over_each_strand() -> None:
    built = _sweep_one(_line(length=7.0), uv_mode=ctm._UV_PER_STRAND)
    v = built[2][:, 1]

    assert v.min() == pytest.approx(0.0)
    assert v.max() == pytest.approx(1.0)


def test_by_length_keeps_texel_density_across_strands_of_different_length() -> None:
    """Per Strand gives every strand the whole texture, so a long one is stretched. By Length
    measures in scene units instead, which is the point of having the choice."""
    short = _sweep_one(_line(length=2.0), uv_mode=ctm._UV_BY_LENGTH)[2][:, 1].max()
    long = _sweep_one(_line(length=8.0), uv_mode=ctm._UV_BY_LENGTH)[2][:, 1].max()

    assert short == pytest.approx(2.0, abs=1e-5)
    assert long == pytest.approx(8.0, abs=1e-5)
    assert long / short == pytest.approx(4.0, rel=1e-4)


def test_tiling_multiplies_the_repeats() -> None:
    once = _sweep_one(_line(), uv_mode=ctm._UV_PER_STRAND, tiling=1.0)[2][:, 1].max()
    thrice = _sweep_one(_line(), uv_mode=ctm._UV_PER_STRAND, tiling=3.0)[2][:, 1].max()

    assert thrice / once == pytest.approx(3.0)


def test_there_is_one_uv_per_point() -> None:
    """st is authored at vertex interpolation, so a mismatch is a malformed mesh rather than
    a wrong-looking one."""
    built = _sweep_one(_arc(), sides=5)
    points, _normals, st, _counts, _indices = built

    assert len(st) == len(points)


# ---------------------------------------------------------------------------
# the frame
# ---------------------------------------------------------------------------

def test_the_cross_section_does_not_spin_as_the_curve_turns() -> None:
    """The naive frame -- rebuilt per sample from a fixed up-vector -- twists around a bend,
    and a texture shows that up immediately as a barber pole. The rotation-minimising frame
    turns only as much as the curve forces it to."""
    points = _arc(n=40)
    tangents, normals, _ = ctm._frames(points)

    # Consecutive normals stay as close together as the tangents do: no extra roll.
    normal_turn = np.degrees(np.arccos(np.clip(
        np.einsum("ij,ij->i", normals[:-1], normals[1:]), -1.0, 1.0)))
    tangent_turn = np.degrees(np.arccos(np.clip(
        np.einsum("ij,ij->i", tangents[:-1], tangents[1:]), -1.0, 1.0)))

    assert normal_turn.max() <= tangent_turn.max() + 1e-6


def test_the_frame_stays_orthonormal_all_the_way_along() -> None:
    points = _arc(n=30)
    tangents, normals, binormals = ctm._frames(points)

    assert np.abs(np.einsum("ij,ij->i", tangents, normals)).max() < 1e-6
    assert np.abs(np.linalg.norm(normals, axis=1) - 1.0).max() < 1e-6
    assert np.abs(np.linalg.norm(binormals, axis=1) - 1.0).max() < 1e-6


def test_a_repeated_point_does_not_break_the_frame() -> None:
    """Vine pins its ends by tripling them, so duplicated samples are the normal case here,
    not an edge one -- and a zero-length segment has no direction to build a frame from."""
    line = _line(n=5)
    doubled = np.concatenate([line[:1], line[:1], line, line[-1:], line[-1:]])

    built = _sweep_one(doubled, radius=0.3)

    assert built is not None
    assert np.isfinite(built[0]).all(), "the frame produced NaNs on a zero-length segment"


# ---------------------------------------------------------------------------
# topology
# ---------------------------------------------------------------------------

def test_every_face_is_a_quad_and_indexes_a_real_point() -> None:
    built = _sweep_one(_arc(n=12), sides=7)
    points, _n, _st, counts, indices = built

    assert set(counts.tolist()) == {4}
    assert len(indices) == len(counts) * 4
    assert indices.min() >= 0
    assert indices.max() < len(points)


def test_two_strands_come_back_as_one_mesh_with_separate_tubes() -> None:
    """Indices of the second strand have to be offset past the first, or its faces reach back
    into the wrong tube and the mesh folds in on itself."""
    a = _line(n=6)
    b = _line(n=6) + np.array([0.0, 10.0, 0.0])
    radii = np.full(6, 0.5)
    built = ctm.build_mesh([(a, radii), (b, radii)], UsdGeom.Tokens.linear,
                           UsdGeom.Tokens.bspline, 6, 4, ctm._UV_PER_STRAND, 1.0, 1.0)

    points, _n, _st, _counts, indices = built
    assert indices.max() < len(points)
    # The two tubes stay a strand apart rather than being welded together.
    assert points[:, 1].max() - points[:, 1].min() > 9.0


def test_width_scale_multiplies_the_radius() -> None:
    radii = np.full(8, 1.0)
    thin = ctm.build_mesh([(_line(), radii)], UsdGeom.Tokens.linear, UsdGeom.Tokens.bspline,
                          6, 4, ctm._UV_PER_STRAND, 1.0, 1.0)[0]
    thick = ctm.build_mesh([(_line(), radii)], UsdGeom.Tokens.linear, UsdGeom.Tokens.bspline,
                           6, 4, ctm._UV_PER_STRAND, 1.0, 3.0)[0]

    assert np.linalg.norm(thick[:, 1:], axis=1).max() == pytest.approx(
        np.linalg.norm(thin[:, 1:], axis=1).max() * 3.0)


# ---------------------------------------------------------------------------
# reading the curves off a stage
# ---------------------------------------------------------------------------

def test_widths_are_read_whatever_interpolation_they_were_authored_at() -> None:
    stage = Usd.Stage.CreateInMemory()
    curves = _curves_prim(stage)
    curves.GetWidthsAttr().Set([2.0])
    curves.SetWidthsInterpolation(UsdGeom.Tokens.constant)

    strands, _kind, _basis = ctm._read_strands(curves.GetPrim())

    assert len(strands) == 1
    assert strands[0][1] == pytest.approx(1.0)  # radius, so half the width


def test_a_curve_under_a_transform_is_swept_where_it_appears() -> None:
    """The Mesh is authored without a transform of its own, so the sweep has to bake the
    curve's. A tube that ignored it would land at the origin while the curve is elsewhere."""
    stage = Usd.Stage.CreateInMemory()
    group = UsdGeom.Xform.Define(stage, "/World")
    group.AddTranslateOp().Set(Gf.Vec3d(100.0, 0.0, 0.0))
    curves = _curves_prim(stage, "/World/Curves")

    strands, _kind, _basis = ctm._read_strands(curves.GetPrim())

    assert strands[0][0][:, 0].min() == pytest.approx(100.0)


def test_a_scaled_group_scales_the_tube_too() -> None:
    stage = Usd.Stage.CreateInMemory()
    group = UsdGeom.Xform.Define(stage, "/World")
    group.AddScaleOp().Set(Gf.Vec3f(2.0, 2.0, 2.0))
    curves = _curves_prim(stage, "/World/Curves")

    strands, _kind, _basis = ctm._read_strands(curves.GetPrim())

    # width 1.0 -> radius 0.5, doubled by the group.
    assert strands[0][1] == pytest.approx(1.0)


def test_every_curves_prim_under_the_pick_is_found() -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    _curves_prim(stage, "/World/A")
    _curves_prim(stage, "/World/B")

    found = ctm._curve_prims(stage.GetPrimAtPath("/World"), recurse=True)
    assert len(found) == 2
    assert ctm._curve_prims(stage.GetPrimAtPath("/World"), recurse=False) == []


# ---------------------------------------------------------------------------
# what is deliberately NOT authored
# ---------------------------------------------------------------------------

def test_the_mesh_carries_no_normals_because_v_ray_drops_uvs_when_it_does() -> None:
    """Not an oversight to tidy up later, so it is pinned here.

    hdVRay stops reading a mesh's primvars when `normals` is authored alongside
    subdivisionScheme=none: the texture silently vanishes and the surface renders its
    fallback colour -- the exact symptom this chunk exists to cure. Measured by rendering
    the same swept vine five ways and diffing against an untextured render: st at vertex,
    varying and faceVarying interpolation all fail while normals are present, and both
    dropping the normals and letting the mesh subdivide fix it.

    So the choice is smooth shading or textures, and textures are the point. `sides` is what
    governs how round the tube looks instead.
    """
    stage = Usd.Stage.CreateInMemory()
    built = _sweep_one(_arc(n=10), sides=8)
    ctm._author(stage, "/World/Mesh", built, None)
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Mesh"))

    # HasAuthoredValue, not HasAttribute: `normals` is declared by the Mesh schema, so it
    # answers True whether or not anything ever wrote to it.
    assert not mesh.GetNormalsAttr().HasAuthoredValue(), (
        "authoring normals here is what stops V-Ray texturing the tube"
    )
    assert mesh.GetSubdivisionSchemeAttr().Get() == UsdGeom.Tokens.none


def test_the_authored_mesh_has_st_at_vertex_interpolation() -> None:
    stage = Usd.Stage.CreateInMemory()
    built = _sweep_one(_arc(n=10), sides=8)
    ctm._author(stage, "/World/Mesh", built, None)
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Mesh"))

    st = UsdGeom.PrimvarsAPI(mesh).GetPrimvar("st")
    assert st and st.HasValue()
    assert st.GetInterpolation() == UsdGeom.Tokens.vertex
    assert len(st.Get()) == len(mesh.GetPointsAttr().Get())


def test_the_curves_colour_carries_over_so_the_tube_is_not_suddenly_grey() -> None:
    stage = Usd.Stage.CreateInMemory()
    source = _curves_prim(stage, "/World/Src")
    source.CreateDisplayColorAttr().Set([Gf.Vec3f(0.18, 0.42, 0.15)])
    built = _sweep_one(_line(), sides=6)

    ctm._author(stage, "/World/Mesh", built, source.GetPrim())

    colour = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Mesh")).GetDisplayColorAttr().Get()
    assert colour and tuple(colour[0]) == pytest.approx((0.18, 0.42, 0.15))
