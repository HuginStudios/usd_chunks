"""An instancer put under a moved prim keeps its instances on the surface they were scattered on.

Scatter Points wrote world-space positions even when Instancer Path sat under a moved,
turned or scaled prim, which applied that prim's transform a second time: flowers scattered
on an urn placed at x = -10 landed at x = -20.
"""
from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import Scene


def _world_instances(stage, path):
    """Each instance's world matrix."""
    instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath(path))
    xforms = instancer.ComputeInstanceTransformsAtTime(Usd.TimeCode.Default(), Usd.TimeCode.Default())
    parent = UsdGeom.XformCache().GetLocalToWorldTransform(instancer.GetPrim())
    return [Gf.Matrix4d(m) * parent for m in xforms]


def _world_points(stage, path):
    return [m.ExtractTranslation() for m in _world_instances(stage, path)]


@pytest.mark.parametrize("transform", [
    {"tx": -10.0, "ty": 1.2},
    {"tx": 3.0, "ry": 90.0, "sx": 1.5, "sy": 1.5, "sz": 1.5},
])
def test_instances_sit_on_the_surface_under_a_moved_parent(transform) -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        last = scene.create.prim(primpath_location="/World/urn", prim_type="Xform")
        last = scene.create.transform(last, primpath_location="/World/urn", **transform)
        last = scene.create.sphere(last, prim_path="/World/urn/mound", radius1=1.0, dimx2=24, dimy2=16)
        last = scene.create.usd_sphere(last, prim_path="/World/bloom", sphere_radius=0.02)
        last = scene.create.scatter_points(
            last, surface_prim="/World/urn/mound", inst_path="/World/urn/flowers",
            prototypes=[{"proto_path": "/World/bloom", "proto_weight": 100}],
            auto_spacing=False, spacing=0.0, max_count=50, seed=1)
        scene.set_output(last)
        centre = UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath("/World/urn/mound")).Transform(
            Gf.Vec3d(0, 0, 0))
        radius = transform.get("sx", 1.0)
        instances = _world_instances(stage, "/World/urn/flowers")
        assert len(instances) == 50
        for matrix in instances:
            point = matrix.ExtractTranslation()
            assert (point - centre).GetLength() == pytest.approx(radius, abs=0.08 * radius)
            # Aligned to the surface: each instance's up points out from the ball's centre.
            up = matrix.TransformDir(Gf.Vec3d(0, 1, 0)).GetNormalized()
            assert up * (point - centre).GetNormalized() > 0.9
            # And the parent's scale is not applied on top of the scatter's own.
            assert matrix.TransformDir(Gf.Vec3d(0, 1, 0)).GetLength() == pytest.approx(1.0, rel=0.02)


def test_an_instancer_at_the_root_is_unchanged() -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        last = scene.create.sphere(prim_path="/World/ball", radius1=2.0, dimx2=24, dimy2=16)
        last = scene.create.transform(last, primpath_location="/World/ball", tx=5.0)
        last = scene.create.usd_sphere(last, prim_path="/World/bloom", sphere_radius=0.02)
        last = scene.create.scatter_points(
            last, surface_prim="/World/ball", inst_path="/flowers",
            prototypes=[{"proto_path": "/World/bloom", "proto_weight": 100}],
            auto_spacing=False, spacing=0.0, max_count=20, seed=2)
        scene.set_output(last)
        for point in _world_points(stage, "/flowers"):
            assert (point - Gf.Vec3d(5, 0, 0)).GetLength() == pytest.approx(2.0, abs=0.15)
