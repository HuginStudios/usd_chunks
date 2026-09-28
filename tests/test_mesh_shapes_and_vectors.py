"""Mesh's sphere, torus and cone; every Mesh being polygons rather than a subdivision
cage; an integer Vector param that holds its edits; and frame specs that arrive as
numbers.
"""
from __future__ import annotations

import numpy as np
import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene, chunk_utils


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


# --------------------------------------------------------------------------------------
# Mesh


def _points(node, path):
    return np.array(UsdGeom.Mesh(node.get_stage().GetPrimAtPath(path)).GetPointsAttr().Get())


def test_mesh_builds_a_sphere(scene):
    node = scene.create.mesh(prim_path="/World/ball", mesh_type="sphere", sphere_radius=2.0)
    assert np.linalg.norm(_points(node, "/World/ball"), axis=1) == pytest.approx(2.0)


def test_mesh_builds_a_torus(scene):
    node = scene.create.mesh(prim_path="/World/ring", mesh_type="torus", torus_radius=3.0, torus_tube_radius=1.0)
    points = _points(node, "/World/ring")
    from_axis = np.hypot(points[:, 0], points[:, 2])
    assert from_axis.min() == pytest.approx(2.0) and from_axis.max() == pytest.approx(4.0)
    assert np.abs(points[:, 1]).max() == pytest.approx(1.0)


def test_mesh_builds_a_cone_standing_on_its_base(scene):
    node = scene.create.mesh(prim_path="/World/cone", mesh_type="cone", cone_radius=1.5, cone_height=3.0)
    points = _points(node, "/World/cone")
    assert points[:, 1].min() == pytest.approx(0.0) and points[:, 1].max() == pytest.approx(3.0)
    assert np.hypot(points[:, 0], points[:, 2]).max() == pytest.approx(1.5)


@pytest.mark.parametrize("mesh_type", ["cube", "sphere", "torus", "cone", "tube", "grid"])
def test_every_mesh_is_polygons_not_a_subdivision_cage(scene, mesh_type):
    # catmullClark, USD's default, is drawn subdivided by V-Ray: a cube became a blob.
    node = scene.create.mesh(prim_path=f"/World/{mesh_type}", mesh_type=mesh_type)
    mesh = UsdGeom.Mesh(node.get_stage().GetPrimAtPath(f"/World/{mesh_type}"))
    assert mesh.GetSubdivisionSchemeAttr().Get() == UsdGeom.Tokens.none


# --------------------------------------------------------------------------------------
# An integer Vector param holds its edits


def test_integer_vector_param_can_be_changed(scene):
    settings = scene.create.render_settings()
    resolution = settings.get_param("resolution")
    stage = settings.get_stage()

    resolution.value = (1242, 375)
    attr = stage.GetAttributeAtPath(resolution._param_attr_path)
    assert attr.Get() == Gf.Vec2i(1242, 375)

    resolution.value = (640, 480)  # the second edit is the one that used to be lost
    assert attr.Get() == Gf.Vec2i(640, 480)


# --------------------------------------------------------------------------------------
# Frame specs


@pytest.mark.parametrize("spec, frames", [
    ("", []),
    ("1:4", [1.0, 2.0, 3.0, 4.0]),
    ("1, 5,9", [1.0, 5.0, 9.0]),
    ("0:20x5", [0.0, 5.0, 10.0, 15.0, 20.0]),
    # Wired to a Wedge's Index, Frames receives the number itself -- 0 included.
    (0, [0.0]),
    (7, [7.0]),
])
def test_parse_frames(spec, frames):
    assert chunk_utils.parse_frames(spec) == frames


@pytest.mark.parametrize("spec", ["5:1", "1:10x0"])
def test_parse_frames_rejects_nonsense(spec):
    with pytest.raises(ValueError):
        chunk_utils.parse_frames(spec)
