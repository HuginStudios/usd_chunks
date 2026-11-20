"""Keep Away From keeps a scatter off the ground under a mesh, not only off its points.

A house's points are its corners, so a scatter kept away from it still filled its floor.
A fence seen from above covers no ground, so the yard it rings stays open.
"""
from __future__ import annotations

import numpy as np
import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene

HALF = 2.0  # the house is 4 x 4, centred on the origin


def _world(up, a, b, h):
    """A point from ground coordinates (a, b) and height *h*, for either up axis."""
    return Gf.Vec3f(a, h, b) if up == "Y" else Gf.Vec3f(a, b, h)


def _ground(stage, up):
    mesh = UsdGeom.Mesh.Define(stage, "/World/ground")
    mesh.CreatePointsAttr([_world(up, a, b, 0.0) for a, b in ((-10, -10), (10, -10), (10, 10), (-10, 10))])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3] if up == "Z" else [3, 2, 1, 0])


def _house(stage, up):
    """A closed box: points only at its eight corners."""
    mesh = UsdGeom.Mesh.Define(stage, "/World/house")
    mesh.CreatePointsAttr([_world(up, a, b, h) for h in (0.0, 3.0)
                           for a, b in ((-HALF, -HALF), (HALF, -HALF), (HALF, HALF), (-HALF, HALF))])
    mesh.CreateFaceVertexCountsAttr([4] * 6)
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4,
                                      1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7])


def _fence(stage, up, half=4.0):
    """Four upright panels round the origin, open to the sky."""
    mesh = UsdGeom.Mesh.Define(stage, "/World/fence")
    corners = ((-half, -half), (half, -half), (half, half), (-half, half))
    mesh.CreatePointsAttr([_world(up, a, b, h) for h in (0.0, 1.0) for a, b in corners])
    mesh.CreateFaceVertexCountsAttr([4] * 4)
    mesh.CreateFaceVertexIndicesAttr([0, 1, 5, 4, 1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7])


def _scatter(up, build, avoid, distance=0.0):
    """Ground positions (a, b) of a scatter kept away from *avoid*."""
    with Scene.new() as scene:
        stage = scene.get_stage()
        UsdGeom.SetStageUpAxis(stage, up)
        _ground(stage, up)
        build(stage, up)
        UsdGeom.Sphere.Define(stage, "/World/rock").GetRadiusAttr().Set(0.05)
        scatter = scene.create.scatter_points(
            surface_prim="/World/ground", inst_path="/World/rocks", max_count=600,
            auto_spacing=False, spacing=0.25, avoid_prims=avoid, avoid_distance=distance,
            **{"prototypes.0.proto_path": "/World/rock"})
        scene.cook(scatter)
        pts = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/rocks")).GetPositionsAttr().Get())
    assert len(pts) > 100, "the scatter placed too little to test"
    return pts[:, [0, 2]] if up == "Y" else pts[:, [0, 1]]


@pytest.mark.parametrize("up", ["Z", "Y"])
def test_nothing_lands_inside_a_house(up):
    ab = _scatter(up, _house, "/World/house")
    inside = (np.abs(ab[:, 0]) < HALF) & (np.abs(ab[:, 1]) < HALF)
    assert not inside.any(), f"{inside.sum()} instances inside the house"


def test_keep_away_distance_counts_from_the_house_walls():
    ab = _scatter("Z", _house, "/World/house", distance=1.0)
    off = np.maximum(np.abs(ab) - HALF, 0.0)
    assert np.hypot(off[:, 0], off[:, 1]).min() >= 1.0


def test_a_fenced_yard_stays_open():
    ab = _scatter("Z", _fence, "/World/fence")
    in_yard = (np.abs(ab[:, 0]) < 3.5) & (np.abs(ab[:, 1]) < 3.5)
    assert in_yard.sum() > 50
