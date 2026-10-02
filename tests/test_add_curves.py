"""Add Hair: each curve grows from the face it was dealt to.

`build_hair` shares the curves out between faces and roots each one on its face. It used to
find a face's vertices by counting them up as the loop went, and skipped the count for a face
that was dealt no curves -- so from the first such face on, every curve was rooted on the
wrong face's vertices. Both tests below build a mesh whose faces are far apart, so a root on
the wrong face is unmistakable.
"""

from __future__ import annotations

import importlib

import numpy as np
from pxr import Gf, Usd, UsdGeom

add_curves = importlib.import_module("chunks.modelling.add_curves")


def _two_quads(stage):
    """Two unit quads in the XZ plane, the second five units along +X from the first."""
    mesh = UsdGeom.Mesh.Define(stage, "/Skin")
    mesh.GetPointsAttr().Set([
        Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(1, 0, 1), Gf.Vec3f(0, 0, 1),
        Gf.Vec3f(5, 0, 0), Gf.Vec3f(6, 0, 0), Gf.Vec3f(6, 0, 1), Gf.Vec3f(5, 0, 1),
    ])
    mesh.GetFaceVertexCountsAttr().Set([4, 4])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3, 4, 5, 6, 7])
    return mesh


def _roots(hair, seg):
    return np.asarray(hair.GetPointsAttr().Get()).reshape(-1, seg, 3)[:, 0]


def test_a_face_dealt_no_curves_does_not_shift_the_next_faces_vertices() -> None:
    stage = Usd.Stage.CreateInMemory()
    mesh = _two_quads(stage)
    # One curve over two faces: the first face rounds 0.5 down to none, and the curve
    # carried over lands on the second.
    hair = add_curves.build_hair(stage, mesh, count=1, seg=2)
    roots = _roots(hair, 2)
    assert len(roots) == 1
    assert 5.0 <= roots[0][0] <= 6.0, f"rooted at {roots[0]}, not on the second quad"


def test_a_subset_of_faces_reads_its_own_vertices() -> None:
    stage = Usd.Stage.CreateInMemory()
    mesh = _two_quads(stage)
    hair = add_curves.build_hair(stage, mesh, count=3, seg=2, faces=[1])
    roots = _roots(hair, 2)
    assert len(roots) == 3
    assert np.all((roots[:, 0] >= 5.0) & (roots[:, 0] <= 6.0)), f"roots {roots[:, 0]}"
