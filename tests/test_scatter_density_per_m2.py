"""Scatter Points takes a density: instances per square metre of the faces it may use.

The count was Max Count, a number for the whole surface. Asked for "2 rocks per m² where the
ground is under 20°", an agent wrote a chunk of its own to sum the flat faces' area and work
the count out, and so does anyone who resizes the terrain.
"""
from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene


def _scatter(scene, ground, **params):
    leaf = scene.create.prim(ground, primpath_location="/World/leaf", prim_type="Cube", name="leaf")
    rocks = scene.create.scatter_points(leaf, inst_path="/World/rocks", auto_spacing=False, spacing=0.0,
                                       **{"prototypes.0.proto_path": "/World/leaf"}, **params)
    scene.set(rocks, surface_prim="/World/ground")
    scene.set_output(rocks)
    return rocks


def _ground(scene, size=10.0, meters_per_unit=1.0):
    """A size x size ground in a stage of those units (a new stage's own are centimetres)."""
    settings = scene.create.stage_settings(meters_per_unit=meters_per_unit)
    return scene.create.ground(settings, prim_path="/World/ground", width1=size, height2=size)


def _count(scene) -> int:
    return len(UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/World/rocks")).GetPositionsAttr().Get() or [])


def test_the_count_follows_the_area() -> None:
    with Scene() as scene:
        rocks = _scatter(scene, _ground(scene), density=2.0, max_count=7)
        assert _count(scene) == 200, "100 m² at 2 per m², Max Count ignored"
        scene.set(rocks, density=0.0)
        assert _count(scene) == 7, "density off: Max Count again"


def test_density_is_per_square_metre_in_any_units() -> None:
    with Scene() as scene:
        _scatter(scene, _ground(scene, size=1000.0, meters_per_unit=0.01), density=2.0)
        assert _count(scene) == 200, "1000 cm squared is 100 m²"


@pytest.mark.parametrize("tilt, expected", [(10.0, 200), (35.0, 0)])
def test_faces_steeper_than_max_slope_add_no_area(tilt, expected) -> None:
    with Scene() as scene:
        tilted = scene.create.transform(_ground(scene), primpath_location="/World/ground", rx=tilt)
        _scatter(scene, tilted, density=2.0, max_slope=20.0)
        assert _count(scene) == expected
