"""Around Field: the ground around an object's base, marked by how near it is, for a scatter.

* 1 at the object, 0 past Reach, and nothing under a closed object;
* on a picked Ground the patch follows its heights, without one it is flat at the lowest point;
* the patch is hidden, and a Scatter Points on it lands around the object.
"""
from __future__ import annotations

import numpy as np
import pytest
from pxr import UsdGeom

from usd_chunks import Scene


def _patch(stage, path="/around"):
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath(path))
    points = np.asarray(mesh.GetPointsAttr().Get())
    values = np.asarray(UsdGeom.PrimvarsAPI(mesh).GetPrimvar("around").Get())
    return mesh, points, values


def _outside(points, half=1.0):
    """How far each point is outside a box of that half size, across the ground (X, Z)."""
    return np.maximum(np.abs(points[:, 0]), np.abs(points[:, 2])) - half


def test_the_field_is_one_at_the_object_and_none_past_reach_or_under_it():
    with Scene() as scene:
        stage = scene.get_stage()
        crate = scene.create.cube(prim_path="/crate", width=2.0, height=2.0, depth=2.0)
        node = scene.create.around_field(crate, prim_path="/crate", primpath_location="/around", reach=1.0,
                                         breakup=0.0, softness=0.5)

        mesh, points, values = _patch(stage)
        out = _outside(points)
        assert (values[(out > 0.05) & (out < 0.45)] > 0.95).all(), "next to the walls"
        assert (values[out > 1.05] == 0).all(), "past Reach"
        assert (values[out < -0.2] == 0).all(), "nothing under a closed box"
        assert np.allclose(points[:, 1], points[0, 1]) and abs(points[0, 1]) < 0.01, "flat at the base"
        assert UsdGeom.Imageable(mesh).GetVisibilityAttr().Get() == UsdGeom.Tokens.invisible
        assert node.get_param("base_height").value == pytest.approx(0.0)


def test_on_a_ground_the_patch_follows_its_heights():
    with Scene() as scene:
        stage = scene.get_stage()
        ground = UsdGeom.Mesh.Define(stage, "/ground")
        ground.CreatePointsAttr([(-6, 0, -6), (6, 1.2, -6), (6, 1.2, 6), (-6, 0, 6)])  # rises 0.1 per unit in X
        ground.CreateFaceVertexCountsAttr([4])
        ground.CreateFaceVertexIndicesAttr([0, 3, 2, 1])
        crate = scene.create.cube(prim_path="/crate", width=2.0, height=2.0, depth=2.0)
        scene.create.around_field(crate, prim_path="/crate", ground="/ground", primpath_location="/around",
                                  reach=1.0)

        _, points, _ = _patch(stage)
        expected = 0.6 + 0.1 * points[:, 0]
        assert np.allclose(points[:, 1], expected, atol=0.01)


def test_a_scatter_on_the_patch_lands_around_the_object():
    with Scene() as scene:
        stage = scene.get_stage()
        crate = scene.create.cube(prim_path="/crate", width=2.0, height=2.0, depth=2.0)
        around = scene.create.around_field(crate, prim_path="/crate", primpath_location="/around", reach=0.8,
                                           breakup=0.0)
        UsdGeom.Sphere.Define(stage, "/stone").GetRadiusAttr().Set(0.03)
        scene.create.scatter_points(around, surface_prim="/around", density_primvar="around", inst_path="/stones",
                                    prototypes=[{"proto_path": "/stone"}], auto_spacing=False, spacing=0.1,
                                    max_count=2000)

        positions = np.asarray(UsdGeom.PointInstancer(stage.GetPrimAtPath("/stones")).GetPositionsAttr().Get())
        assert len(positions) > 50
        out = _outside(positions)
        # Scatter reads the field at the nearest patch point, so a stone can sit up to a cell
        # (a tenth of Reach) inside the edge the field draws.
        assert (out > -0.09).all() and (out < 0.85).all(), "around the crate, within Reach, not under it"
