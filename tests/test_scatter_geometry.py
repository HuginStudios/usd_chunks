"""The triangle soup: same answers as the per-triangle version, at a workable speed.

`_TriangleSoup` replaced a list of Gf.Vec3f tuples with four numpy arrays, which is a change
of representation under everything Scatter Points does with geometry. These pin the parts of
that representation the samplers depend on and the old loops made obvious by construction --
fan order, world space, degenerate faces, the empty case -- so a later rewrite of the array
maths cannot quietly change what gets scattered.
"""

from __future__ import annotations

import importlib
import random

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

scatter = importlib.import_module("chunks.modelling.scatter_points")


def _mesh(stage, path, points, counts, indices):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set([Gf.Vec3f(*p) for p in points])
    mesh.GetFaceVertexCountsAttr().Set(counts)
    mesh.GetFaceVertexIndicesAttr().Set(indices)
    return mesh


def _quad_soup(translate=None):
    """A 2x2 quad, as a soup.

    The stage is kept alive until the soup is built and not a moment less: a `Usd.Prim` holds
    only a weak pointer, so `_stage().GetPrimAtPath(...)` as one expression hands
    `_collect_triangles` a prim whose stage has already gone, and it quite correctly finds
    nothing on it.
    """
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    mesh = _mesh(
        stage,
        "/World/Quad",
        [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)],
        [4],
        [0, 1, 2, 3],
    )
    if translate:
        UsdGeom.Xformable(mesh).AddTranslateOp().Set(Gf.Vec3d(*translate))
    return scatter._collect_triangles(stage.GetPrimAtPath("/World/Quad"))


# ---------------------------------------------------------------------------
# what comes out of a mesh
# ---------------------------------------------------------------------------

def test_a_quad_fans_into_two_triangles_in_the_old_order() -> None:
    """(v0, v1, v2) then (v0, v2, v3) -- the fan the per-face loop walked. Written as index
    arithmetic now, which is the one place a rewrite could silently reorder it."""
    soup = _quad_soup()

    assert len(soup) == 2
    first, second = soup[0], soup[1]
    assert [tuple(v) for v in first[:3]] == [(0, 0, 0), (2, 0, 0), (2, 2, 0)]
    assert [tuple(v) for v in second[:3]] == [(0, 0, 0), (2, 2, 0), (0, 2, 0)]


def test_triangles_come_back_in_world_space() -> None:
    """The samplers, the density mask and the brush all work in world space; a soup in local
    space would put every instance in the wrong place on any transformed asset."""
    soup = _quad_soup(translate=(10, 0, 0))

    low, high = soup.bounds()
    assert low == Gf.Vec3f(10, 0, 0)
    assert high == Gf.Vec3f(12, 2, 0)


def test_a_hierarchy_contributes_every_mesh_under_it() -> None:
    """What a user picks as "the surface" is an Xform far more often than a Mesh."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World/Table")
    _mesh(stage, "/World/Table/Top", [(0, 0, 0), (1, 0, 0), (1, 1, 0)], [3], [0, 1, 2])
    _mesh(stage, "/World/Table/Leg", [(0, 0, 5), (1, 0, 5), (1, 1, 5)], [3], [0, 1, 2])

    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Table"))

    assert len(soup) == 2
    assert soup.bounds()[1][2] == 5


def test_normals_point_out_of_the_face_and_are_unit_length() -> None:
    soup = _quad_soup()

    for _v0, _v1, _v2, n in soup:
        assert Gf.GetLength(n) == pytest.approx(1.0)
        assert n == Gf.Vec3f(0, 0, 1)


def test_a_degenerate_face_keeps_its_place_and_is_filtered_by_slope() -> None:
    """Dropping them would renumber every triangle after them. The slope filter rejects
    anything with no direction, which is where they were always going to be caught."""
    stage = Usd.Stage.CreateInMemory()
    _mesh(
        stage,
        "/World/Mesh",
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0)],
        [3, 3],
        [0, 1, 2, 0, 1, 3],  # first face is a straight line
    )

    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Mesh"))

    assert len(soup) == 2
    kept = soup.slope_mask(Gf.Vec3f(0, 0, 1), 90.0)
    assert list(kept) == [False, True]


def test_a_mesh_with_no_faces_falls_back_to_the_bounding_box_lid() -> None:
    """Two triangles across the top of the bounds, so a scatter onto something unmeshed still
    has somewhere to put things."""
    stage = Usd.Stage.CreateInMemory()
    sphere = UsdGeom.Sphere.Define(stage, "/World/Ball")
    sphere.GetRadiusAttr().Set(3.0)

    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Ball"))

    assert len(soup) == 2
    low, high = soup.bounds()
    assert low[2] == high[2] == pytest.approx(3.0), "the lid, not the whole box"


def test_nothing_at_all_is_an_empty_soup_rather_than_an_error() -> None:
    empty = scatter._collect_triangles(None)
    assert len(empty) == 0
    assert not empty
    assert empty.bounds() is None
    assert scatter._scatter_on_triangles(empty, 10, random.Random(0)) == ([], [])


# ---------------------------------------------------------------------------
# what the soup answers
# ---------------------------------------------------------------------------

def test_areas_match_the_triangles_they_describe() -> None:
    """The sampler draws against these, so an area that is wrong is a distribution that is
    wrong -- and silently, since the result still looks like a scatter."""
    soup = _quad_soup()

    assert list(soup.areas()) == pytest.approx([2.0, 2.0])


def test_the_slope_filter_agrees_with_the_one_triangle_rule() -> None:
    """`slope_mask` is `_slope_ok` vectorised. Two implementations of one rule only stay
    honest while something checks they say the same thing."""
    stage = Usd.Stage.CreateInMemory()
    # A ramp, a wall and a floor: 45 degrees, 90 and 0.
    _mesh(
        stage,
        "/World/Mesh",
        [(0, 0, 0), (1, 0, 0), (1, 1, 1), (2, 0, 0), (2, 0, 1), (3, 1, 0), (4, 0, 0), (5, 0, 0), (5, 1, 0)],
        [3, 3, 3],
        [0, 1, 2, 3, 4, 5, 6, 7, 8],
    )
    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Mesh"))
    up = Gf.Vec3f(0, 0, 1)

    for limit in (0.0, 30.0, 45.0, 90.0, 180.0):
        vector = list(soup.slope_mask(up, limit))
        scalar = [scatter._slope_ok(tri[3], up, limit) for tri in soup]
        assert vector == scalar, f"the two disagree at {limit} degrees"


def test_select_keeps_the_triangles_the_mask_picked() -> None:
    soup = _quad_soup()

    kept = soup.select(np.asarray([False, True]))

    assert len(kept) == 1
    assert [tuple(v) for v in kept[0][:3]] == [(0, 0, 0), (2, 2, 0), (0, 2, 0)]


# ---------------------------------------------------------------------------
# the bucket grid both ray users share
# ---------------------------------------------------------------------------

def test_a_triangle_spanning_several_cells_is_in_all_of_them() -> None:
    """The case the expansion exists for. A triangle covering a quarter of the surface has to
    answer a ray cast anywhere under it, not only where its first corner fell."""
    stage = Usd.Stage.CreateInMemory()
    _mesh(
        stage,
        "/World/Mesh",
        [(0, 0, 0), (100, 0, 0), (100, 100, 0), (0, 100, 0)],
        [4],
        [0, 1, 2, 3],
    )
    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Mesh"))
    buckets = scatter._TriangleBuckets(soup, (0, 1), cells=8)

    # Both halves of the quad reach into the middle; every corner reaches at least one.
    assert len(buckets.candidates(50, 50)) == 2
    for corner in ((1, 1), (99, 1), (99, 99), (1, 99)):
        assert len(buckets.candidates(*corner)) >= 1


def test_a_query_outside_the_bounds_clamps_to_the_edge() -> None:
    """Candidates are not guaranteed to land inside the grid they were built from, and an
    unclamped key would index a cell that cannot exist."""
    soup = _quad_soup()
    buckets = scatter._TriangleBuckets(soup, (0, 1), cells=4)

    assert buckets.key(-1000, -1000) == (0, 0)
    assert buckets.key(1000, 1000) == (3, 3)


def test_the_stack_ray_lands_on_the_surface_it_was_cast_at() -> None:
    """Stack asks this thousands of times; a wrong hit is an instance floating or buried."""
    stage = Usd.Stage.CreateInMemory()
    _mesh(
        stage,
        "/World/Floor",
        [(0, 0, 3), (10, 0, 3), (10, 10, 3), (0, 10, 3)],
        [4],
        [0, 1, 2, 3],
    )
    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Floor"))
    grid = scatter._SurfaceRayGrid(soup, up_idx=2)

    point, normal = grid.hit(5.0, 5.0, Gf.Vec3f(0, 0, 1))

    assert point[2] == pytest.approx(3.0)
    assert normal == Gf.Vec3f(0, 0, 1)


def test_the_ray_stops_at_the_first_surface_from_above() -> None:
    """A bowl's inner floor, not its underside."""
    stage = Usd.Stage.CreateInMemory()
    _mesh(
        stage,
        "/World/Stack",
        [
            (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
            (0, 0, 5), (10, 0, 5), (10, 10, 5), (0, 10, 5),
        ],
        [4, 4],
        [0, 1, 2, 3, 4, 5, 6, 7],
    )
    soup = scatter._collect_triangles(stage.GetPrimAtPath("/World/Stack"))
    grid = scatter._SurfaceRayGrid(soup, up_idx=2)

    point, _normal = grid.hit(5.0, 5.0, Gf.Vec3f(0, 0, 1))

    assert point[2] == pytest.approx(5.0)


def test_a_ray_over_nothing_reports_nothing() -> None:
    soup = _quad_soup()
    grid = scatter._SurfaceRayGrid(soup, up_idx=2)

    assert grid.hit(500.0, 500.0, Gf.Vec3f(0, 0, 1)) is None


# ---------------------------------------------------------------------------
# the density lookup
# ---------------------------------------------------------------------------

def test_a_lookup_cell_holds_about_the_same_few_vertices_at_any_density() -> None:
    """The invariant the whole grid rests on.

    A query searches the 27 cells around it and nothing further, so the cell size has to be
    measured against the *vertices* -- if it is pinned to the bounding box instead, the same
    grid means "a handful of neighbours" on one mesh and "most of the surface" or "nothing at
    all" on another. A fixed 48 divisions was wrong at both ends.
    """
    for count in (16, 400, 10_000, 340_000):
        # Vertices of a surface fall in roughly divisions**2 cells.
        per_cell = count / (scatter._lookup_divisions(count) ** 2)
        assert 2 <= per_cell <= 32, f"{per_cell:.1f} vertices per cell at {count} vertices"

    assert scatter._lookup_divisions(50_000_000) == scatter._LOOKUP_MAX_DIVISIONS
    assert scatter._lookup_divisions(0) >= 1, "an empty field must not divide by zero"


def test_a_fully_painted_coarse_mesh_scatters_over_all_of_it() -> None:
    """Painted everywhere has to mean the same thing as unpainted.

    It did not. With the grid pinned to the bounding box, a floor of a few big quads had cells
    far smaller than the gap between its vertices, so every candidate in the middle of a quad
    landed in an empty neighbourhood and read as unpainted -- and the sampler's rejection loop
    hid it, retrying until it found somewhere near a corner. A fully painted 20-unit quad put
    139 of 200 instances within 1.1 units of a corner, in four clumps.
    """
    stage = Usd.Stage.CreateInMemory()
    mesh = _mesh(
        stage,
        "/World/Floor",
        [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0)],
        [4],
        [0, 1, 2, 3],
    )
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        scatter.DENSITY_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    ).Set(Vt.FloatArray([1.0] * 4))
    prim = stage.GetPrimAtPath("/World/Floor")
    soup = scatter._collect_triangles(prim)

    painted, _ = scatter._scatter_on_triangles(
        soup, 200, random.Random(1), 0,
        mask=scatter._density_mask(prim, scatter.DENSITY_PRIMVAR),
    )
    unpainted, _ = scatter._scatter_on_triangles(soup, 200, random.Random(1), 0)

    assert len(painted) == 200, "weight 1.0 everywhere must not reject anything"
    # Compared against the unpainted scatter rather than an absolute figure: the claim is that
    # the mask changed nothing, and the unpainted run is what "nothing" looks like.
    centre = Gf.Vec3f(10, 10, 0)
    reach = max((p - centre).GetLength() for p in painted)
    assert reach > 0.8 * max((p - centre).GetLength() for p in unpainted)


def _painted_row(weights_at):
    """Vertices half a unit apart along X over a ten-unit span, with the weights given.

    Spaced rather than scattered on purpose: the grid answers with the nearest vertex in the
    27 cells around a query, so how far it reaches is a property of vertex *density*, not of
    the query. A hand-made lookup with three vertices ten units apart would be answering a
    question no real mesh asks.
    """
    xs = np.arange(0.0, 10.5, 0.5)
    points = np.stack([xs, np.zeros_like(xs), np.zeros_like(xs)], axis=1)
    weights = np.asarray([weights_at(float(x)) for x in xs])
    return scatter._NearestWeight(points, weights)


def test_a_weight_lookup_finds_the_nearest_painted_vertex() -> None:
    lookup = _painted_row(lambda x: 1.0 if x < 1.0 else 0.25)

    assert lookup.sample(Gf.Vec3f(0.1, 0.0, 0.0)) == pytest.approx(1.0)
    assert lookup.sample(Gf.Vec3f(5.1, 0.0, 0.0)) == pytest.approx(0.25)


def test_a_weight_outside_the_painted_region_reads_as_unpainted() -> None:
    lookup = _painted_row(lambda _x: 1.0)

    assert lookup.sample(Gf.Vec3f(1000.0, 1000.0, 1000.0)) == 0.0


def test_weights_are_clamped_to_a_probability() -> None:
    """The sampler compares them against a random draw, so anything outside 0..1 is a mask
    that quietly stops masking."""
    lookup = _painted_row(lambda x: 5.0 if x < 1.0 else -3.0)

    assert lookup.sample(Gf.Vec3f(0.0, 0.0, 0.0)) == 1.0
    assert lookup.sample(Gf.Vec3f(9.9, 0.0, 0.0)) == 0.0
