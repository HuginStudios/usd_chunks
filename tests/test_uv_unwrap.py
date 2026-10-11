"""UV Unwrap: UVs a baker can use.

Every corner gets a coordinate inside the 0-1 square, charts don't overlap -- not within a
mesh, and not across meshes sharing one atlas -- and a polygon mesh keeps its materials and
faceVarying data through the triangulation xatlas needs.
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk_registry, cook_cache
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init

uv = importlib.import_module("chunks.modelling.uv_unwrap")

pytestmark = pytest.mark.skipif(not uv._HAS_XATLAS, reason="needs xatlas")


def _sphere(rings=16, segments=24, centre=(0.0, 0.0, 0.0)):
    """A closed lat/long sphere as (points, counts, indices): quads, triangle fans at the poles."""
    lat = np.linspace(0.0, np.pi, rings + 1)[1:-1]
    lon = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    ring_points = np.stack([np.sin(la) * np.cos(lo), np.sin(la) * np.sin(lo), np.cos(la)], axis=-1).reshape(-1, 3)
    points = np.vstack([[0, 0, 1], ring_points, [0, 0, -1]]) + np.asarray(centre)
    south = len(points) - 1

    def ring(r, s):
        return 1 + r * segments + (s % segments)

    counts, indices = [], []
    for s in range(segments):
        counts.append(3)
        indices += [0, ring(0, s), ring(0, s + 1)]
    for r in range(rings - 2):
        for s in range(segments):
            counts.append(4)
            indices += [ring(r, s), ring(r + 1, s), ring(r + 1, s + 1), ring(r, s + 1)]
    for s in range(segments):
        counts.append(3)
        indices += [ring(rings - 2, s), south, ring(rings - 2, s + 1)]
    return points, counts, indices


def _define(stage, path, points, counts, indices):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points, dtype=np.float32)))
    mesh.GetFaceVertexCountsAttr().Set(counts)
    mesh.GetFaceVertexIndicesAttr().Set(indices)
    return mesh


def _triangulated(points, counts, indices):
    tris, _corners, _faces = uv.fan_triangulate(counts, indices)
    return np.asarray(points, dtype=np.float64), tris


def _coverage(uv_triangles, size=256) -> np.ndarray:
    """How many triangles cover each texel centre of a size x size grid."""
    grid = np.zeros((size, size), dtype=np.int32)
    centres = (np.arange(size) + 0.5) / size
    for a, b, c in uv_triangles:
        lo = np.floor(np.minimum(np.minimum(a, b), c) * size).astype(int).clip(0, size - 1)
        hi = np.ceil(np.maximum(np.maximum(a, b), c) * size).astype(int).clip(0, size)
        xs, ys = np.meshgrid(centres[lo[0] : hi[0]], centres[lo[1] : hi[1]], indexing="ij")
        p = np.stack([xs, ys], axis=-1)
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12:
            continue
        w0 = ((b[1] - c[1]) * (p[..., 0] - c[0]) + (c[0] - b[0]) * (p[..., 1] - c[1])) / d
        w1 = ((c[1] - a[1]) * (p[..., 0] - c[0]) + (a[0] - c[0]) * (p[..., 1] - c[1])) / d
        inside = (w0 > 1e-6) & (w1 > 1e-6) & (1.0 - w0 - w1 > 1e-6)
        grid[lo[0] : hi[0], lo[1] : hi[1]] += inside
    return grid


class _DummyScene(SceneSender):
    def __init__(self, stage=None):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def wired(tmp_path, monkeypatch):
    # A private cook cache: the user's would answer a cook from an earlier run, and these
    # tests are about what the chunk itself does.
    monkeypatch.setenv(cook_cache.ENV_DIR, str(tmp_path / "cook_cache"))
    stage = Usd.Stage.CreateInMemory()
    scene = _DummyScene(stage)
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    def make():
        module = reg.get_chunk_module("uv_unwrap")
        chunks = module.create() if module.loaded else module.load()
        for c in chunks:
            scene.add_chunk(c)
        return chunks[0]

    return stage, make


# ---------------------------------------------------------------------------
# the unwrap itself
# ---------------------------------------------------------------------------


def test_a_closed_mesh_unwraps_into_the_unit_square_without_overlap() -> None:
    points, tris = _triangulated(*_sphere())
    (st,) = uv.unwrap([(points, tris)], resolution=512, padding=2)

    assert st.shape == (len(tris), 3, 2)
    assert st.min() >= 0.0 and st.max() <= 1.0
    assert _coverage(st).max() <= 1


def test_meshes_sharing_an_atlas_do_not_overlap_each_other() -> None:
    a = _triangulated(*_sphere())
    b = _triangulated(*_sphere(centre=(3.0, 0.0, 0.0)))
    st_a, st_b = uv.unwrap([a, b], resolution=512, padding=2)
    assert (_coverage(st_a) + _coverage(st_b)).max() <= 1


# ---------------------------------------------------------------------------
# the chunk
# ---------------------------------------------------------------------------


def test_a_triangle_mesh_gets_face_varying_st_and_keeps_its_topology(wired) -> None:
    stage, make = wired
    points, tris = _triangulated(*_sphere())
    _define(stage, "/Mesh", points, [3] * len(tris), tris.reshape(-1).tolist())

    node = make()
    node.get_param("prim_path").value = "/Mesh"

    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/Mesh"))
    assert list(mesh.GetFaceVertexIndicesAttr().Get()) == tris.reshape(-1).tolist()
    st = UsdGeom.PrimvarsAPI(mesh).GetPrimvar("st")
    assert st.GetInterpolation() == UsdGeom.Tokens.faceVarying
    assert len(st.Get()) == 3 * len(tris)
    assert not st.IsIndexed()


def test_a_polygon_mesh_is_triangulated_with_its_subsets_and_primvars_carried(wired) -> None:
    stage, make = wired
    points, counts, indices = _sphere()
    mesh = _define(stage, "/Mesh", points, counts, indices)
    # Faces tagged by hemisphere, as a subset and as a uniform primvar; and a faceVarying
    # value equal to the corner's point index, so where it lands can be checked exactly.
    face_centres = np.array(
        [points[indices[o : o + n]].mean(axis=0) for o, n in zip(np.cumsum([0] + counts[:-1]), counts)]
    )
    upper = np.flatnonzero(face_centres[:, 2] > 0.0)
    UsdGeom.Subset.CreateGeomSubset(mesh, "upper", UsdGeom.Tokens.face, Vt.IntArray(upper.tolist()))
    api = UsdGeom.PrimvarsAPI(mesh)
    api.CreatePrimvar("hemi", Sdf.ValueTypeNames.IntArray, UsdGeom.Tokens.uniform).Set(
        Vt.IntArray((face_centres[:, 2] > 0.0).astype(int).tolist())
    )
    api.CreatePrimvar("corner_point", Sdf.ValueTypeNames.IntArray, UsdGeom.Tokens.faceVarying).Set(
        Vt.IntArray(list(indices))
    )

    node = make()
    node.get_param("prim_path").value = "/Mesh"

    tri_indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get())
    assert set(mesh.GetFaceVertexCountsAttr().Get()) == {3}
    n_tris = len(tri_indices) // 3
    centres = np.asarray(points)[tri_indices.reshape(-1, 3)].mean(axis=1)

    subset = UsdGeom.Subset(stage.GetPrimAtPath("/Mesh/upper"))
    members = np.zeros(n_tris, dtype=bool)
    members[np.asarray(subset.GetIndicesAttr().Get())] = True
    assert np.array_equal(members, centres[:, 2] > 0.0)
    assert np.array_equal(np.asarray(api.GetPrimvar("hemi").Get()), (centres[:, 2] > 0.0).astype(int))
    assert np.array_equal(np.asarray(api.GetPrimvar("corner_point").Get()), tri_indices)
    assert len(api.GetPrimvar("st").Get()) == len(tri_indices)


def test_recooking_unwraps_the_input_not_its_own_output(wired) -> None:
    stage, make = wired
    points, counts, indices = _sphere()
    _define(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("prim_path").value = "/Mesh"
    first = np.asarray(UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Mesh")).GetPrimvar("st").Get())
    node.get_param("padding").value = 4
    node.get_param("padding").value = 2
    again = np.asarray(UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Mesh")).GetPrimvar("st").Get())
    assert np.allclose(first, again)


def test_the_uv_set_name_is_honoured(wired) -> None:
    stage, make = wired
    points, counts, indices = _sphere()
    _define(stage, "/Mesh", points, counts, indices)

    node = make()
    node.get_param("uv_name").value = "bake"
    node.get_param("prim_path").value = "/Mesh"
    api = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/Mesh"))
    assert api.GetPrimvar("bake") and not api.GetPrimvar("st")
