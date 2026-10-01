"""Grid and Ground lie flat on the stage's ground plane, centered, facing up.

They used to be built in X-Y with a corner at the origin: on a Y-up stage, Ground was a
20x20 wall rather than something to stand things on.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import Scene


def _extent_and_normal(stage, path):
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath(path))
    pts = [Gf.Vec3d(p) for p in mesh.GetPointsAttr().Get()]
    a, b, c = (pts[i] for i in mesh.GetFaceVertexIndicesAttr().Get()[:3])
    lo = tuple(min(p[k] for p in pts) for k in range(3))
    hi = tuple(max(p[k] for p in pts) for k in range(3))
    return lo, hi, Gf.Cross(b - a, c - a).GetNormalized()


@pytest.mark.parametrize("up, flat_axis", [("Y", 1), ("Z", 2)])
def test_ground_is_a_centered_floor(up, flat_axis) -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up)
    with Scene(stage) as scene:
        scene.create.ground(prim_path="/set/ground")

        lo, hi, normal = _extent_and_normal(stage, "/set/ground")

    assert lo[flat_axis] == hi[flat_axis] == 0.0, "flat on the ground plane"
    for k in {0, 1, 2} - {flat_axis}:
        assert (lo[k], hi[k]) == (-10.0, 10.0), "20 wide, centered on the origin"
    assert normal[flat_axis] == pytest.approx(1.0), "facing up"
