"""A collision mesh asking for convexDecomposition is simulated as several convex pieces.

One convex hull of a concave part fills its hollows: a curved part became a solid
wedge, and a rod held in the hollow was squeezed against invisible faces -- the part
stopped a centimetre short of the rod in the render. The RBD Solver now splits such a mesh
into compact groups of faces and hulls each, so the hollow stays open.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from pxr import Gf, Usd, UsdGeom, UsdPhysics

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chunks" / "dynamics"))
import rbd_solver as rbd  # noqa: E402


def _box_faces(centre, half, offset):
    """A box's 8 corners and 6 quads, its indices offset by `offset`."""
    cx, cy, cz = centre
    pts = [(cx + sx * half, cy + sy * half, cz + sz * half) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return pts, [[i + offset for i in q] for q in quads]


def _two_prongs(approximation):
    """One mesh of two boxes a gap apart -- a gripper's two fingers, the hollow between them at
    the origin."""
    stage = Usd.Stage.CreateInMemory()
    mesh = UsdGeom.Mesh.Define(stage, "/prongs")
    left, lq = _box_faces((-1.0, 0.0, 0.0), 0.4, 0)
    right, rq = _box_faces((1.0, 0.0, 0.0), 0.4, 8)
    quads = lq + rq
    mesh.CreatePointsAttr([Gf.Vec3f(*p) for p in left + right])
    mesh.CreateFaceVertexCountsAttr([4] * len(quads))
    mesh.CreateFaceVertexIndicesAttr([i for q in quads for i in q])
    UsdPhysics.CollisionAPI.Apply(mesh.GetPrim())
    UsdPhysics.MeshCollisionAPI.Apply(mesh.GetPrim()).CreateApproximationAttr(approximation)
    return stage, mesh.GetPrim()  # the stage too: a prim outlives nothing without it


def _contains(verts, point):
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    return bool(np.all(point >= lo) and np.all(point <= hi))


@pytest.mark.skipif(not rbd._HAS_MUJOCO, reason="needs mujoco")
def test_a_decomposed_collider_leaves_the_hollow_open():
    _stage, prim = _two_prongs("convexDecomposition")
    kind, pieces, _pos, _quat = rbd._collider_shape(prim, Gf.Matrix4d(1.0))

    assert kind == "meshes" and len(pieces) >= 2
    gap = np.zeros(3)
    assert not any(_contains(p, gap) for p in pieces), "a piece spans the hollow between the prongs"


@pytest.mark.skipif(not rbd._HAS_MUJOCO, reason="needs mujoco")
def test_a_convex_hull_collider_is_still_one_hull():
    """The hull as before, gap and all: only convexDecomposition splits."""
    _stage, prim = _two_prongs("convexHull")
    kind, verts, _pos, _quat = rbd._collider_shape(prim, Gf.Matrix4d(1.0))

    assert kind == "mesh"
    assert _contains(verts, np.zeros(3))


def test_the_split_is_the_same_every_time():
    _stage, prim = _two_prongs("convexDecomposition")
    mesh = UsdGeom.Mesh(prim)
    first = rbd._convex_pieces(mesh, np.eye(4))
    second = rbd._convex_pieces(mesh, np.eye(4))
    assert len(first) == len(second)
    assert all(np.array_equal(a, b) for a, b in zip(first, second))


def test_a_mesh_is_split_once_and_split_again_when_it_changes(monkeypatch):
    """The split is most of a solve's cost on a robot's link meshes, and a bake used to redo it every time:
    it is kept by the mesh's content, placed by each call's own matrix, and redone for an edited mesh."""
    _stage, prim = _two_prongs("convexDecomposition")
    mesh = UsdGeom.Mesh(prim)
    calls = []
    real = rbd._group_pieces
    monkeypatch.setattr(rbd, "_group_pieces", lambda *a: calls.append(1) or real(*a))
    rbd._PIECES_CACHE.clear()

    first = rbd._convex_pieces(mesh, np.eye(4))
    moved = np.eye(4)
    moved[3, :3] = (5.0, 0.0, 0.0)
    shifted = rbd._convex_pieces(mesh, moved)
    assert len(calls) == 1                                        # the second call reused the split
    assert all(np.allclose(b, a + [5.0, 0.0, 0.0]) for a, b in zip(first, shifted))

    points = list(mesh.GetPointsAttr().Get())
    points[0] = Gf.Vec3f(-1.6, -0.4, -0.4)                        # an edited mesh: split again
    mesh.GetPointsAttr().Set(points)
    rbd._convex_pieces(mesh, np.eye(4))
    assert len(calls) == 2
