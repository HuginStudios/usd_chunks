"""URDF Import finds a relative Mesh Root from the network's folder, as it does the .urdf.

Mesh Root was joined to the working directory, so a network whose meshes sat beside it
imported a robot with no geometry when run from anywhere else (found by the robot
benchmark, where the .urdf is in pkg/urdf/ and its package:// meshes in pkg/meshes/).
"""
from __future__ import annotations

import struct

from pxr import Usd, UsdGeom

from usd_chunks import Scene

URDF = """<robot name="r">
  <link name="base">
    <visual><geometry><mesh filename="package://pkg/meshes/tri.stl"/></geometry></visual>
  </link>
</robot>
"""


def _stl(path) -> None:
    path.parent.mkdir(parents=True)
    with open(path, "wb") as f:
        f.write(b"binary".ljust(80, b" ") + struct.pack("<I", 1))
        f.write(struct.pack("<12f", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0) + b"\0\0")


def test_a_relative_mesh_root_is_found_from_the_network_folder(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "proj" / "inputs" / "pkg"
    _stl(pkg / "meshes" / "tri.stl")
    (pkg / "urdf").mkdir()
    (pkg / "urdf" / "r.urdf").write_text(URDF, encoding="utf-8")
    (tmp_path / "proj" / "net").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with Scene.new(path=str(tmp_path / "proj" / "net" / "robot.usda")) as scene:
        robot = scene.create.urdf_import(
            primpath_location="/World/robot", file_path="../inputs/pkg/urdf/r.urdf",
            mesh_root="../inputs/pkg",
        )
        scene.set_output(robot)
        meshes = [p for p in Usd.PrimRange(scene.get_stage().GetPrimAtPath("/World/robot"))
                  if p.IsA(UsdGeom.Mesh)]
        assert meshes, "the STL was not found: Mesh Root was read from the working directory"
        assert len(UsdGeom.Mesh(meshes[0]).GetPointsAttr().Get()) == 3
