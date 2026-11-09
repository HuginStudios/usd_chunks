"""Shape makes USD's own shape prims -- a Cube prim, not a polygon mesh shaped like one.

Mesh builds polygons, so a scene that asks for a Cube in the USD sense (typeName Cube) had
no chunk. In an agent benchmark, three agents out of three wrote one of their own.
"""
from __future__ import annotations

import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene


def _range(stage, path):
    r = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_]).ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()
    return r.GetMin(), r.GetMax()


def test_a_usd_cube_is_a_cube_prim_of_its_size() -> None:
    with Scene() as scene:
        scene.set_output(scene.create.usd_cube(prim_path="/World/box", cube_size=1.0))
        stage = scene.get_stage()
        prim = stage.GetPrimAtPath("/World/box")
        assert prim.GetTypeName() == "Cube"
        assert UsdGeom.Cube(prim).GetSizeAttr().Get() == 1.0
        assert _range(stage, "/World/box") == (Gf.Vec3d(-0.5, -0.5, -0.5), Gf.Vec3d(0.5, 0.5, 0.5))


@pytest.mark.parametrize("entry, type_name", [("usd_sphere", "Sphere"), ("usd_cylinder", "Cylinder"),
                                              ("usd_cone", "Cone"), ("usd_capsule", "Capsule"),
                                              ("usd_plane", "Plane")])
def test_every_entry_makes_its_shape(entry, type_name) -> None:
    with Scene() as scene:
        scene.set_output(getattr(scene.create, entry)(prim_path="/World/s"))
        assert scene.get_stage().GetPrimAtPath("/World/s").GetTypeName() == type_name


def test_a_cylinder_runs_along_its_axis() -> None:
    with Scene() as scene:
        scene.set_output(scene.create.usd_cylinder(prim_path="/World/post", round_radius=0.1, round_height=3.0))
        lo, hi = _range(scene.get_stage(), "/World/post")
        assert Gf.IsClose(hi - lo, Gf.Vec3d(0.2, 3.0, 0.2), 1e-6), "not 3 m tall along Y"


def test_it_reopens_the_same(tmp_path) -> None:
    path = str(tmp_path / "net.usda")
    with Scene() as scene:
        scene.set_output(scene.create.usd_cube(prim_path="/World/box", cube_size=2.5))
        scene.save(path)
    with Scene.open(path) as scene:
        assert UsdGeom.Cube(scene.get_stage().GetPrimAtPath("/World/box")).GetSizeAttr().Get() == 2.5


def test_its_entries_do_not_shadow_meshs() -> None:
    """Mesh's Cube is still scene.create.cube, a polygon mesh; Shape's is usd_cube."""
    with Scene() as scene:
        mesh_box = scene.create.cube(prim_path="/World/mesh_box")
        scene.set_output(scene.create.usd_cube(mesh_box, prim_path="/World/usd_box"))
        stage = scene.get_stage()
        assert stage.GetPrimAtPath("/World/mesh_box").GetTypeName() == "Mesh"
        assert stage.GetPrimAtPath("/World/usd_box").GetTypeName() == "Cube"
