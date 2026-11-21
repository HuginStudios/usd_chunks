"""Facing Field: where meshes face up, sideways or down, or are under cover.

* Tops, Walls and Undersides mark a box's top, sides and bottom;
* Sheltered marks the ground under a roof, strongest just below it, and not the open ground.
"""
from __future__ import annotations

import numpy as np
from pxr import UsdGeom

from usd_chunks import Scene


def _values(stage, path, name):
    return np.asarray(UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(path)).GetPrimvar(name).Get())


def _points(stage, path):
    return np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath(path)).GetPointsAttr().Get())


def test_tops_walls_and_undersides_of_a_box():
    with Scene() as scene:
        stage = scene.get_stage()
        box = scene.create.cube(prim_path="/box", width=1.0, height=1.0, depth=1.0, dimx=5, dimy=5, dimz=5)
        last = box
        for find in ("Tops", "Walls", "Undersides"):
            last = scene.create.facing_field(last, prim_path="/box", find=find, primvar=find.lower(), angle=10.0,
                                             softness=0.0)
        pts = _points(stage, "/box")
        top, bottom = pts[:, 1] > 0.99, pts[:, 1] < 0.01  # the cube stands on y = 0
        side = ~top & ~bottom

        tops = _values(stage, "/box", "tops")
        assert tops[top & (np.abs(pts[:, 0]) < 0.4) & (np.abs(pts[:, 2]) < 0.4)].min() == 1.0
        assert tops[side].max() == 0.0 and tops[bottom].max() == 0.0
        walls = _values(stage, "/box", "walls")
        assert walls[side].min() == 1.0 and walls[top & (np.abs(pts[:, 0]) < 0.4)].max() == 0.0
        unders = _values(stage, "/box", "undersides")
        assert unders[bottom & (np.abs(pts[:, 0]) < 0.4) & (np.abs(pts[:, 2]) < 0.4)].min() == 1.0


def test_sheltered_marks_the_ground_under_a_roof():
    with Scene() as scene:
        stage = scene.get_stage()
        n = 41
        xs = np.linspace(-4.0, 4.0, n)
        gx, gz = np.meshgrid(xs, xs, indexing="ij")
        ground = UsdGeom.Mesh.Define(stage, "/ground")
        ground.CreatePointsAttr([(x, 0.0, z) for x, z in zip(gx.ravel(), gz.ravel())])
        ground.CreateFaceVertexCountsAttr([4] * (n - 1) ** 2)
        ground.CreateFaceVertexIndicesAttr([v for i in range(n - 1) for j in range(n - 1)
                                            for v in (i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j)])
        roof = UsdGeom.Mesh.Define(stage, "/roof")
        roof.CreatePointsAttr([(-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)])
        roof.CreateFaceVertexCountsAttr([4])
        roof.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
        scene.create.facing_field(prim_path="/ground", find="Sheltered", cover="/roof", reach=2.0)

        values = _values(stage, "/ground", "facing")
        pts = _points(stage, "/ground")
        under = (np.abs(pts[:, 0]) < 0.9) & (np.abs(pts[:, 2]) < 0.9)
        open_ground = (np.abs(pts[:, 0]) > 1.2) | (np.abs(pts[:, 2]) > 1.2)
        assert values[under].min() > 0.4, "under the roof"
        assert values[open_ground].max() == 0.0, "nothing over it"
