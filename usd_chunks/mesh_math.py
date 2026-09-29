"""Mesh maths shared by the chunks that read polygon meshes as triangles.

Needs numpy, which the framework itself does not: only the chunks that declare numpy
import this module, so a missing numpy is not fatal to the rest of the package.
"""
from __future__ import annotations

import numpy as np
from pxr import UsdGeom


def world_points(prim, points):
    """A mesh's points in world space, as an (N, 3) float64 array."""
    matrix = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(prim), dtype=np.float64)
    # Row-vector convention, which is what Gf.Matrix4d.Transform uses: p * M, with the
    # translation in the last row.
    return np.asarray(points, dtype=np.float64) @ matrix[:3, :3] + matrix[3, :3]


def fan_triangulate(counts, indices):
    """Fan-triangulate a polygon mesh: `(triangles, corners, face_of_triangle)`.

    `corners` is the same shape as `triangles` but indexes the flat face-corner array
    (i.e. positions within `faceVertexIndices`) instead of the point array -- that is what
    lets a faceVarying primvar be read per triangle corner afterwards.

    Faces with fewer than three vertices contribute nothing but still consume their slots
    in `indices`, so offsets are computed over every face, not just the usable ones.
    """
    counts = np.asarray(counts, dtype=np.int64)
    indices = np.asarray(indices, dtype=np.int64)
    if counts.size == 0 or indices.size == 0:
        empty_i = np.zeros((0, 3), dtype=np.int64)
        return empty_i, empty_i, np.zeros(0, dtype=np.int64)

    offsets = np.concatenate(([0], np.cumsum(counts)[:-1]))
    per_face = np.maximum(counts - 2, 0)
    total = int(per_face.sum())
    if total == 0:
        empty_i = np.zeros((0, 3), dtype=np.int64)
        return empty_i, empty_i, np.zeros(0, dtype=np.int64)

    face_of_tri = np.repeat(np.arange(counts.size, dtype=np.int64), per_face)
    # Position of each triangle within its own face's fan, without a Python loop.
    run_start = np.repeat(np.cumsum(per_face) - per_face, per_face)
    step = np.arange(total, dtype=np.int64) - run_start

    base = offsets[face_of_tri]
    corners = np.stack([base, base + step + 1, base + step + 2], axis=1)
    return indices[corners], corners, face_of_tri
