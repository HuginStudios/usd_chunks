"""Poly Reduce: the parts a reducer is actually judged on.

Getting a triangle count down is the easy half. What separates a usable reducer from a
destructive one is what survives the collapse — the silhouette, the UV layout, the
material assignment — and whether running it twice does the same thing as running it once.
Those are what these pin.
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Gf, Sdf, Tf, Usd, UsdGeom, Vt

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins.chunk_usd import initialize as _chunk_usd_init

pr = importlib.import_module("scripts.modelling.poly_reduce")

needs_simplify = pytest.mark.skipif(
    not pr._HAS_SIMPLIFY, reason="needs fast-simplification"
)


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


def _grid(n, height=lambda x, y: 0.0):
    """An n x n quad grid over [-1, 1]^2, displaced by `height`."""
    xs = np.linspace(-1.0, 1.0, n)
    x, y = np.meshgrid(xs, xs, indexing="ij")
    z = height(x, y)
    points = np.stack([x, y, np.broadcast_to(z, x.shape)], axis=-1).reshape(-1, 3)

    i = np.arange(n - 1)[:, None]
    j = np.arange(n - 1)[None, :]
    a, b, c, d = (i * n + j), ((i + 1) * n + j), ((i + 1) * n + j + 1), (i * n + j + 1)
    a, b, c, d = (np.broadcast_to(k, (n - 1, n - 1)).ravel() for k in (a, b, c, d))
    counts = np.full(len(a), 4, dtype=np.int64)
    indices = np.stack([a, b, c, d], axis=1).ravel()
    return points, counts, indices


def _sphere(rings=40, segments=40, radius=1.0):
    """A lat/long sphere as (points, counts, indices), triangles at the poles."""
    lat = np.linspace(0.0, np.pi, rings)
    lon = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    points = np.stack(
        [radius * np.sin(la) * np.cos(lo), radius * np.sin(la) * np.sin(lo), radius * np.cos(la)],
        axis=-1,
    ).reshape(-1, 3)

    counts, indices = [], []
    for r in range(rings - 1):
        for s in range(segments):
            s2 = (s + 1) % segments
            counts.append(4)
            indices += [r * segments + s, (r + 1) * segments + s, (r + 1) * segments + s2, r * segments + s2]
    return points, np.asarray(counts, dtype=np.int64), np.asarray(indices, dtype=np.int64)


def _define_mesh(stage, path, points, counts, indices):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points, dtype=np.float32)))
    mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.ascontiguousarray(counts, dtype=np.int32)))
    mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(np.ascontiguousarray(indices, dtype=np.int32)))
    return mesh


class _DummyScene(SceneSender):
    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def wired():
    """A stage with the chunk machinery live, plus a factory for Poly Reduce chunks."""
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def make():
        module = reg.get_chunk_module("poly_reduce")
        chunks = module.create() if module.loaded else module.load()
        for c in chunks:
            scene.add_chunk(c)
        return chunks[0]

    return stage, make


def _triangle_count(stage, path="/Mesh"):
    counts = UsdGeom.Mesh(stage.GetPrimAtPath(path)).GetFaceVertexCountsAttr().Get()
    return len(counts) if counts else 0


# ---------------------------------------------------------------------------
# triangulation
# ---------------------------------------------------------------------------


def test_a_quad_fans_into_two_triangles_and_remembers_its_corners() -> None:
    """The corner array is what lets a faceVarying primvar be read per triangle corner
    afterwards; it must index the flat faceVertexIndices array, not the point array."""
    triangles, corners, face_of_tri = pr.fan_triangulate([4], [7, 8, 9, 10])

    assert triangles.tolist() == [[7, 8, 9], [7, 9, 10]]
    assert corners.tolist() == [[0, 1, 2], [0, 2, 3]]
    assert face_of_tri.tolist() == [0, 0]


def test_mixed_face_sizes_keep_their_own_corner_offsets() -> None:
    """A real asset is quads with the odd triangle and n-gon in it. Getting the offset of
    the second face wrong silently reads the first face's UVs onto it."""
    triangles, corners, face_of_tri = pr.fan_triangulate([3, 5], [0, 1, 2, 3, 4, 5, 6, 7])

    assert triangles.tolist() == [[0, 1, 2], [3, 4, 5], [3, 5, 6], [3, 6, 7]]
    assert corners.tolist() == [[0, 1, 2], [3, 4, 5], [3, 5, 6], [3, 6, 7]]
    assert face_of_tri.tolist() == [0, 1, 1, 1]


def test_a_degenerate_face_contributes_nothing_but_still_consumes_its_indices() -> None:
    """Offsets are computed over every face. Skipping the bad one shifts every face after
    it onto the wrong indices."""
    triangles, corners, _ = pr.fan_triangulate([2, 3], [0, 1, 4, 5, 6])

    assert triangles.tolist() == [[4, 5, 6]]
    assert corners.tolist() == [[2, 3, 4]]


# ---------------------------------------------------------------------------
# the reduction itself
# ---------------------------------------------------------------------------


@needs_simplify
def test_reduction_hits_the_asked_for_triangle_count() -> None:
    points, counts, indices = _grid(60, lambda x, y: 0.3 * np.sin(3 * x) * np.cos(3 * y))
    triangles, corners, _ = pr.fan_triangulate(counts, indices)

    reduced = pr.reduce_triangles(points, triangles, corners, target_count=len(triangles) // 10)

    assert len(reduced.triangles) == pytest.approx(len(triangles) // 10, rel=0.05)


@needs_simplify
def test_a_sphere_reduced_by_90_percent_is_still_a_sphere() -> None:
    """The whole point of quadric error metrics over, say, vertex clustering: the surface
    is allowed to lose most of its triangles and almost none of its shape."""
    points, counts, indices = _sphere(48, 48)
    triangles, corners, _ = pr.fan_triangulate(counts, indices)

    reduced = pr.reduce_triangles(points, triangles, corners, target_count=len(triangles) // 10)

    radii = np.linalg.norm(reduced.points, axis=1)
    assert np.abs(radii - 1.0).max() < 0.02  # every vertex within 2% of the real surface


@needs_simplify
def test_planar_only_flattens_a_flat_sheet_and_leaves_a_curved_one_alone() -> None:
    """Lossless mode is the "clean up over-tessellation" tool, not a reducer: it must take
    a flat grid to almost nothing and a curved one to nothing at all."""
    flat_points, counts, indices = _grid(30)
    triangles, corners, _ = pr.fan_triangulate(counts, indices)
    curved_points, _, _ = _grid(30, lambda x, y: 0.4 * np.sin(3 * x) * np.cos(3 * y))

    flat = pr.reduce_triangles(flat_points, triangles, corners, planar_only=True)
    curved = pr.reduce_triangles(curved_points, triangles, corners, planar_only=True)

    assert len(flat.triangles) < 10
    assert curved is None or len(curved.triangles) > 0.98 * len(triangles)


@needs_simplify
def test_locking_the_boundary_keeps_the_outline_where_it_was() -> None:
    """An open sheet — a leaf card, a piece of cloth — reduced without this can lose its
    silhouette from the edges inward, which reads as the asset shrinking."""
    points, counts, indices = _grid(40, lambda x, y: 0.2 * np.sin(4 * x))
    triangles, corners, _ = pr.fan_triangulate(counts, indices)

    locked = pr.reduce_triangles(
        points, triangles, corners, target_count=len(triangles) // 20, lock_boundary=True
    )

    on_edge = np.isclose(np.abs(locked.points[:, :2]).max(axis=1), 1.0)
    assert on_edge.sum() == 4 * (40 - 1)  # every original border vertex still there


@needs_simplify
def test_a_weld_key_stops_collapses_from_crossing_it() -> None:
    """The single mechanism behind both UV-seam and material preservation: corners on
    opposite sides of a key boundary become separate vertices, so no edge — and therefore
    no collapse — ever spans them."""
    points, counts, indices = _grid(30)
    triangles, corners, _ = pr.fan_triangulate(counts, indices)
    # Split the sheet down the middle: every corner is labelled by which half its own face
    # sits in, exactly as a GeomSubset or a UV seam would label it.
    face_x = points[indices].reshape(len(counts), 4, 3)[:, :, 0].mean(axis=1)
    codes = np.repeat((face_x >= 0.0).astype(np.int64), counts)

    reduced = pr.reduce_triangles(
        points, triangles, corners, codes, target_count=len(triangles) // 10
    )

    side_of_vertex = codes[reduced.source_corner]
    per_triangle = side_of_vertex[reduced.triangles]
    assert (per_triangle.min(axis=1) == per_triangle.max(axis=1)).all()


# ---------------------------------------------------------------------------
# the chunk, on a stage
# ---------------------------------------------------------------------------


@needs_simplify
def test_reducing_a_mesh_rewrites_it_in_place_as_triangles(wired) -> None:
    stage, make = wired
    points, counts, indices = _grid(40, lambda x, y: 0.3 * np.sin(3 * x))
    _define_mesh(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 10.0
    node.get_param("prim_path").value = "/Mesh"

    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh"))
    assert set(mesh.GetFaceVertexCountsAttr().Get()) == {3}
    assert len(mesh.GetFaceVertexCountsAttr().Get()) == pytest.approx(len(counts) * 2 * 0.1, rel=0.1)
    assert node.get_param("out_triangles").value == len(mesh.GetFaceVertexCountsAttr().Get())


@needs_simplify
def test_cooking_twice_reduces_the_same_input_twice_not_the_previous_result(wired) -> None:
    """The bug this chunk is one edit away from at all times: it authors points and
    faceVertexIndices overrides on the prim it reads, so reading the composed stage would
    make every cook reduce the last cook's output. Two cooks with the same settings must
    land on the same triangle count, not a quarter of it."""
    stage, make = wired
    points, counts, indices = _grid(40, lambda x, y: 0.3 * np.sin(3 * x))
    _define_mesh(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 50.0
    node.get_param("prim_path").value = "/Mesh"
    first = _triangle_count(stage)

    node.get_param("keep_percent").value = 50.000001  # a cook that changes nothing else
    assert _triangle_count(stage) == first

    node.get_param("keep_percent").value = 25.0
    node.get_param("keep_percent").value = 50.0
    assert _triangle_count(stage) == first


@needs_simplify
def test_disabling_the_chunk_gives_the_full_resolution_mesh_back(wired) -> None:
    """The reduction lives in the chunk's own layer and nowhere else — that is what makes
    it a non-destructive modelling op rather than an export step."""
    stage, make = wired
    points, counts, indices = _grid(30)
    _define_mesh(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 10.0
    node.get_param("prim_path").value = "/Mesh"
    assert _triangle_count(stage) < len(counts) * 2

    chunk_registry.ChunkLayerManager().clear_layer(node.get_edit_context().identifier)
    assert _triangle_count(stage) == len(counts)


@needs_simplify
def test_uvs_survive_the_reduction_with_their_seam_intact(wired) -> None:
    """A faceVarying `st` comes back as a vertex primvar of the right length, and both
    sides of a hard seam are still represented — a reducer that welded across the seam
    would leave only one of them, and the texture would smear across the join."""
    stage, make = wired
    points, counts, indices = _grid(30)
    mesh = _define_mesh(stage, "/Mesh", points, counts, indices)

    # u is 0..0.4 on the left half of the sheet and 0.6..1.0 on the right: a deliberate,
    # empty band in the middle that no honest remap can produce a value inside.
    corner_x = points[indices, 0]
    u = np.where(corner_x < 0.0, 0.2 + corner_x * 0.2, 0.8 + corner_x * 0.2)
    st = np.stack([u, points[indices, 1]], axis=1).astype(np.float32)
    primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
    )
    primvar.Set(Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(st)))

    node = make()
    node.get_param("keep_percent").value = 15.0
    node.get_param("prim_path").value = "/Mesh"

    out = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Mesh")).GetPrimvar("st")
    values = np.asarray(out.Get())
    assert out.GetInterpolation() == UsdGeom.Tokens.vertex
    assert len(values) == len(UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh")).GetPointsAttr().Get())
    assert ((values[:, 0] > 0.45) & (values[:, 0] < 0.55)).sum() == 0  # nothing in the gap
    assert (values[:, 0] < 0.45).any() and (values[:, 0] > 0.55).any()  # both sides kept


@needs_simplify
def test_geom_subsets_still_partition_the_reduced_faces(wired) -> None:
    """Materials are assigned per face. Reduce a two-material mesh without rewriting the
    subsets and every face index points at a different triangle than it used to — the
    materials end up scattered across the asset."""
    stage, make = wired
    points, counts, indices = _grid(30)
    mesh = _define_mesh(stage, "/Mesh", points, counts, indices)

    face_x = points[indices].reshape(len(counts), 4, 3)[:, :, 0].mean(axis=1)
    left = np.flatnonzero(face_x < 0.0).astype(np.int32)
    right = np.flatnonzero(face_x >= 0.0).astype(np.int32)
    for name, faces in (("left", left), ("right", right)):
        subset = UsdGeom.Subset.CreateGeomSubset(
            mesh, name, UsdGeom.Tokens.face, Vt.IntArray.FromNumpy(faces), "materialBind"
        )
        assert subset

    node = make()
    node.get_param("keep_percent").value = 20.0
    node.get_param("prim_path").value = "/Mesh"

    total = _triangle_count(stage)
    out_left = np.asarray(UsdGeom.Subset(stage.GetPrimAtPath("/Mesh/left")).GetIndicesAttr().Get())
    out_right = np.asarray(UsdGeom.Subset(stage.GetPrimAtPath("/Mesh/right")).GetIndicesAttr().Get())

    assert len(out_left) and len(out_right)
    assert len(out_left) + len(out_right) == total  # covers everything
    assert not set(out_left.tolist()) & set(out_right.tolist())  # exactly once each
    assert max(out_left.max(), out_right.max()) < total


@needs_simplify
def test_what_cannot_survive_the_new_topology_is_blocked_rather_than_left_stale(wired) -> None:
    """A uniform primvar is one value per face and authored normals describe the old
    surface. Leaving either behind gives an array whose length no longer matches the
    mesh — which renderers handle by ignoring the mesh, or by crashing."""
    stage, make = wired
    points, counts, indices = _grid(30)
    mesh = _define_mesh(stage, "/Mesh", points, counts, indices)
    api = UsdGeom.PrimvarsAPI(mesh)
    api.CreatePrimvar("faceId", Sdf.ValueTypeNames.IntArray, UsdGeom.Tokens.uniform).Set(
        Vt.IntArray.FromNumpy(np.arange(len(counts), dtype=np.int32))
    )
    mesh.GetNormalsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * len(points)))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)

    node = make()
    node.get_param("keep_percent").value = 20.0
    node.get_param("prim_path").value = "/Mesh"

    out = UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh"))
    assert out.GetNormalsAttr().Get() is None
    assert UsdGeom.PrimvarsAPI(out).GetPrimvar("faceId").Get() is None


@needs_simplify
def test_vertex_colours_are_carried_onto_the_reduced_vertices(wired) -> None:
    """Vertex primvars are the ones that *can* survive, and the same mapping carries skin
    weights. Every value must be one that really existed on the source mesh."""
    stage, make = wired
    points, counts, indices = _grid(30)
    mesh = _define_mesh(stage, "/Mesh", points, counts, indices)
    colours = np.zeros((len(points), 3), dtype=np.float32)
    colours[:, 0] = (points[:, 0] > 0).astype(np.float32)
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex
    ).Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(colours)))

    node = make()
    node.get_param("keep_percent").value = 20.0
    node.get_param("prim_path").value = "/Mesh"

    out = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Mesh")).GetPrimvar("displayColor")
    values = np.asarray(out.Get())
    assert out.GetInterpolation() == UsdGeom.Tokens.vertex
    assert len(values) == len(UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh")).GetPointsAttr().Get())
    assert set(np.unique(values[:, 0]).tolist()) <= {0.0, 1.0}  # never averaged into a blend


@needs_simplify
def test_a_hierarchy_reduces_every_mesh_under_the_selected_prim(wired) -> None:
    """What an artist picks is the asset, not each of the twelve meshes inside it."""
    stage, make = wired
    UsdGeom.Xform.Define(stage, "/Asset")
    points, counts, indices = _grid(30)
    _define_mesh(stage, "/Asset/Body", points, counts, indices)
    _define_mesh(stage, "/Asset/Lid", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 10.0
    node.get_param("prim_path").value = "/Asset"

    assert _triangle_count(stage, "/Asset/Body") < len(counts)
    assert _triangle_count(stage, "/Asset/Lid") < len(counts)

    node.get_param("recurse").value = False
    assert _triangle_count(stage, "/Asset/Body") == len(counts)  # back to the source mesh


@needs_simplify
def test_target_triangle_count_mode_ignores_the_input_size(wired) -> None:
    """A collision proxy or an engine budget is an absolute number; two assets of very
    different densities must both land on it."""
    stage, make = wired
    dense_points, dense_counts, dense_indices = _grid(60)
    sparse_points, sparse_counts, sparse_indices = _grid(30)
    _define_mesh(stage, "/Dense", dense_points, dense_counts, dense_indices)
    _define_mesh(stage, "/Sparse", sparse_points, sparse_counts, sparse_indices)

    node = make()
    node.get_param("reduce_mode").value = pr.MODE_COUNT
    node.get_param("target_tris").value = 200
    node.get_param("prim_path").value = "/Dense,/Sparse"

    assert _triangle_count(stage, "/Dense") == pytest.approx(200, abs=8)
    assert _triangle_count(stage, "/Sparse") == pytest.approx(200, abs=8)


def test_the_mode_menu_shows_only_the_control_that_mode_reads(wired) -> None:
    """Three modes sharing one panel; a Keep % slider that does nothing in Triangle Count
    mode is a bug report waiting to happen."""
    from usd_chunks import chunk

    _, make = wired
    node = make()
    hidden = chunk.ChunkFlags.State.HIDDEN

    assert not node.get_param("keep_percent").has_state(hidden)
    assert node.get_param("target_tris").has_state(hidden)

    node.get_param("reduce_mode").value = pr.MODE_COUNT
    assert node.get_param("keep_percent").has_state(hidden)
    assert not node.get_param("target_tris").has_state(hidden)

    node.get_param("reduce_mode").value = pr.MODE_PLANAR
    assert node.get_param("keep_percent").has_state(hidden)
    assert node.get_param("target_tris").has_state(hidden)


# ---------------------------------------------------------------------------
# the weld key's fast path
# ---------------------------------------------------------------------------


def test_labelling_components_separately_partitions_rows_the_same_way() -> None:
    """The whole reason the weld key is built per component rather than per row: one-column
    uniques are ordinary sorts, row-wise ones are lexsorts, and on a million-triangle mesh
    that is the difference between six seconds and twenty-five. It is only a legitimate
    shortcut if it draws exactly the same boundaries."""
    rng = np.random.default_rng(7)
    rows = rng.integers(0, 4, (500, 3)).astype(np.float32)

    combined = pr.combine_codes(*pr.component_codes(rows))
    row_wise = np.unique(rows, axis=0, return_inverse=True)[1].reshape(-1)

    # Same partition, whatever the labels themselves happen to be.
    assert (np.unique(combined, return_inverse=True)[1] == row_wise).all()


def test_a_key_too_wide_to_pack_falls_back_instead_of_wrapping_around() -> None:
    """Packing labels into one int64 is what makes the weld fast. Silently overflowing it
    would merge unrelated corners — vertices welded across a seam they should never cross."""
    wide = np.array([0, 1 << 40, 2 << 40], dtype=np.int64)

    assert pr._pack_key([wide, wide, wide]) is None
    assert len(np.unique(pr.combine_codes(wide, wide, wide))) == 3


# ---------------------------------------------------------------------------
# the collapse mapping
# ---------------------------------------------------------------------------


@needs_simplify
def test_the_derived_mapping_agrees_with_the_decimation_it_describes() -> None:
    """Every carried primvar is indexed through this mapping, so it has to name the same
    reduced vertex the decimator actually produced. It is derived from the collapse pairs
    rather than obtained from the library, because the library's own replay helper deletes
    isolated points one at a time in a Python loop and goes quadratic (see
    `collapse_mapping`) — this pins that the cheap derivation is also the correct one."""
    points, counts, indices = _sphere(30, 30)
    triangles, corners, _ = pr.fan_triangulate(counts, indices)

    reduced = pr.reduce_triangles(points, triangles, corners, target_count=len(triangles) // 4)

    # Each reduced vertex must sit near the original vertex whose primvars it inherits:
    # a mapping off by even one index would pair them with an unrelated part of the mesh.
    moved = np.linalg.norm(reduced.points - points[reduced.source_point], axis=1)
    assert moved.max() < 0.2  # unit sphere
    assert len(np.unique(reduced.source_point)) == len(reduced.points)


@needs_simplify
def test_a_mesh_of_thousands_of_disconnected_cards_reduces_promptly() -> None:
    """The shape that broke it: a fir tree is a quarter of a million twig cards, and the
    library's replay helper took 698 seconds on it where the decimation itself took 0.4.
    Nothing about this mesh is unusual except how many separate islands it has."""
    import time

    n_cards = 4000
    origins = np.arange(n_cards, dtype=np.float64)[:, None] * [1.0, 0.0, 0.0]
    quad = np.array([[0, 0, 0], [0.8, 0, 0], [0.8, 0.8, 0.1], [0, 0.8, 0]], dtype=np.float64)
    points = (origins[:, None, :] + quad[None, :, :]).reshape(-1, 3)
    counts = np.full(n_cards, 4, dtype=np.int64)
    indices = np.arange(n_cards * 4, dtype=np.int64)
    triangles, corners, _ = pr.fan_triangulate(counts, indices)

    started = time.perf_counter()
    reduced = pr.reduce_triangles(points, triangles, corners, target_count=len(triangles) // 4)
    elapsed = time.perf_counter() - started

    assert reduced is not None and reduced.source_point is not None
    assert elapsed < 10.0  # loose: the point is seconds-not-minutes, not a timing target


# ---------------------------------------------------------------------------
# not re-doing work
# ---------------------------------------------------------------------------


def _live_module():
    """The module copy the registry actually loaded and calls `execute` on.

    Importing `scripts.modelling.poly_reduce` gives a *second*, independent copy under a
    different `sys.modules` key — with its own `_LAST_COOK` — so a test about cook state
    has to reach the registry's one.
    """
    return chunk_registry.ChunkRegistry().get_chunk_module("poly_reduce").module


@needs_simplify
def test_an_unchanged_recook_does_not_redo_the_reduction(wired) -> None:
    """The graph re-runs a chunk's execute far more often than its inputs change — a view
    switch, a new node, a fresh wire all trigger a view-driven recook of the whole upstream
    tree. Live-reported as usdview freezing on unrelated edits, because a 4M-triangle
    reduction is 40s and was being redone every time. Proven here by tampering with the
    authored result: an unchanged recook must leave it exactly as it was."""
    stage, make = wired
    points, counts, indices = _grid(30)
    _define_mesh(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 20.0
    node.get_param("prim_path").value = "/Mesh"
    reduced_count = _triangle_count(stage)
    assert reduced_count < len(counts)

    with Usd.EditContext(stage, node.get_edit_context()):
        UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh")).GetFaceVertexCountsAttr().Set(Vt.IntArray([3]))

    _live_module().execute(node)

    assert _triangle_count(stage) == 1  # the tamper survived: nothing was recomputed


@needs_simplify
def test_a_changed_setting_still_recooks(wired) -> None:
    """The other half of the same guard — skipping work must never outlive the reason for
    skipping it."""
    stage, make = wired
    points, counts, indices = _grid(30)
    _define_mesh(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 50.0
    node.get_param("prim_path").value = "/Mesh"
    half = _triangle_count(stage)

    node.get_param("keep_percent").value = 10.0

    assert _triangle_count(stage) < half


@needs_simplify
def test_an_upstream_edit_to_the_same_mesh_still_recooks(wired) -> None:
    """The case that makes a cheap fingerprint of point *counts* alone wrong: an upstream
    chunk moving the geometry leaves every array the same length."""
    stage, make = wired
    points, counts, indices = _grid(30)
    mesh = _define_mesh(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("keep_percent").value = 30.0
    node.get_param("prim_path").value = "/Mesh"
    before = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh")).GetPointsAttr().Get())

    moved = points + np.array([0.0, 0.0, 5.0])
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(moved, dtype=np.float32)))
    _live_module().execute(node)

    after = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh")).GetPointsAttr().Get())
    assert after[:, 2].mean() == pytest.approx(before[:, 2].mean() + 5.0, abs=1e-3)


def test_the_readout_stays_short_enough_for_the_node_header() -> None:
    """Live-reported: "4,176,819 → 1,044,205 triangles (25.0%)" is simply cut off in the
    node's fixed-width header, so the number you most want is the one you cannot see."""
    assert pr._compact(4_176_819) == "4.18M"
    assert pr._compact(1_044_205) == "1.04M"
    assert pr._compact(90_989) == "91k"
    assert pr._compact(940) == "940"


# ---------------------------------------------------------------------------
# subsets and topology have to change together
# ---------------------------------------------------------------------------


def _subsets_out_of_range(stage, path="/Mesh"):
    """Any face subset on `path` currently indexing past the end of the face list."""
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath(path))
    counts = mesh.GetFaceVertexCountsAttr().Get()
    n_faces = len(counts) if counts else 0
    return [
        subset.GetPrim().GetName()
        for subset in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(mesh))
        if (indices := subset.GetIndicesAttr().Get()) and n_faces and max(indices) >= n_faces
    ]


def _grid_with_subsets(stage, path="/Mesh", n=30):
    points, counts, indices = _grid(n)
    mesh = _define_mesh(stage, path, points, counts, indices)
    face_x = points[indices].reshape(len(counts), 4, 3)[:, :, 0].mean(axis=1)
    for name, selected in (("left", face_x < 0.0), ("right", face_x >= 0.0)):
        UsdGeom.Subset.CreateGeomSubset(
            mesh,
            name,
            UsdGeom.Tokens.face,
            Vt.IntArray.FromNumpy(np.flatnonzero(selected).astype(np.int32)),
            "materialBind",
        )
    return mesh, counts


@needs_simplify
def test_a_cook_never_publishes_subsets_that_outrun_the_topology(wired) -> None:
    """Live-reported as a wall of Hydra warnings on every recook:

        Geom subset index 2107245 is larger than number of faces (1044205), removing.

    Every `Set()` publishes its own change notice, so writing the new topology first and
    the rewritten subset indices last left a window where the two disagreed — and a subset
    indexing past the face list is not stale, it is invalid, so Hydra discards it. The
    final state was always right, which is why it only ever showed up as noise. Watching
    every intermediate state is the only way to catch it."""
    stage, make = wired
    _grid_with_subsets(stage)

    seen = []

    def watch(notice, sender):
        seen.extend(_subsets_out_of_range(stage))

    key = Tf.Notice.Register(Usd.Notice.ObjectsChanged, watch, stage)
    try:
        node = make()
        node.get_param("keep_percent").value = 20.0
        node.get_param("prim_path").value = "/Mesh"
        assert _triangle_count(stage) < 100_000  # it really did reduce
        assert seen == []

        node.get_param("keep_percent").value = 40.0
        assert seen == []
    finally:
        del key

    assert _subsets_out_of_range(stage) == []


@needs_simplify
def test_a_mesh_whose_subsets_cannot_be_rewritten_is_left_alone(monkeypatch) -> None:
    """The permanent version of the same fault. If the collapse history ever fails to
    resolve, the subsets cannot be remapped — and authoring the reduced topology anyway
    would leave them out of range for good. Not reducing is the lesser failure: visibly
    un-reduced beats quietly missing its materials."""
    stage = Usd.Stage.CreateInMemory()
    _grid_with_subsets(stage)
    monkeypatch.setattr(pr, "collapse_mapping", lambda *a, **k: None)

    settings = pr._Settings(
        mode=pr.MODE_PERCENT,
        keep_percent=20.0,
        target_tris=5000,
        aggressiveness=7.0,
        lock_boundary=False,
        keep_uvs=True,
        recurse=True,
    )
    plan = pr._plan_mesh(stage.GetPrimAtPath("/Mesh"), settings)

    assert plan is None


def test_an_unchanged_array_is_recognised_without_rereading_it() -> None:
    """The skip-a-recook check is almost entirely the cost of checksumming the geometry,
    and almost every time it confirms nothing moved. VtArray is copy-on-write, so an
    unchanged attribute hands back the same buffer — a matching address is proof enough,
    and holding the array we signed is what keeps that sound (nothing else can be allocated
    at an address we still own)."""
    stage = Usd.Stage.CreateInMemory()
    points, counts, indices = _grid(20)
    mesh = _define_mesh(stage, "/Mesh", points, counts, indices)
    attr = mesh.GetPointsAttr()

    first: dict = {}
    signature = pr._checksum(attr.Get(), {}, first)
    second: dict = {}
    assert pr._checksum(attr.Get(), first, second) == signature
    assert len(second) == 1  # carried over rather than recomputed

    moved = points + np.array([0.0, 0.0, 3.0])
    attr.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(moved, dtype=np.float32)))
    third: dict = {}

    # Same length, same dtype, different contents — the signature has to move with them.
    assert pr._checksum(attr.Get(), first, third) != signature
