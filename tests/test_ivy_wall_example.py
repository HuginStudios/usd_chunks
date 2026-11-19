"""The vine-to-mesh workflow, end to end: examples/scripts/ivy_wall.py, built and checked.

Paint -> Vine -> Curves to Mesh -> bark material, with leaves instanced on the Vine's leaf
points, is how a vine gets into a renderer that draws no curves (Aurora). Each link has its
own tests; this checks that they still meet: the stems arrive as a textured, shaded mesh on
the painted face, the curves are out of the way, and the leaves follow the vine, still and
animated.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from pxr import Usd, UsdGeom, UsdShade

_SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "scripts" / "ivy_wall.py"
_spec = importlib.util.spec_from_file_location("ivy_wall_example", _SCRIPT)
ivy_wall = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ivy_wall)


@pytest.fixture(scope="module")
def still(tmp_path_factory):
    folder = tmp_path_factory.mktemp("ivy")
    _network, result = ivy_wall.build(str(folder / "net.usda"), str(folder / "ivy.usda"))
    return Usd.Stage.Open(result)


@pytest.fixture(scope="module")
def animated(tmp_path_factory):
    folder = tmp_path_factory.mktemp("ivy_anim")
    _network, result = ivy_wall.build(str(folder / "net.usda"), str(folder / "ivy.usda"), animate=True)
    return Usd.Stage.Open(result)


def _bound(stage, path):
    return UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()[0].GetPath()


def test_the_stems_are_a_shaded_textured_mesh(still) -> None:
    stems = UsdGeom.Mesh(still.GetPrimAtPath("/World/ivy/Stems"))
    points = np.asarray(stems.GetPointsAttr().Get())

    assert len(points) > 1000
    assert UsdGeom.PrimvarsAPI(stems).GetPrimvar("st").HasValue()
    assert UsdGeom.PrimvarsAPI(stems).GetPrimvar("growth").HasValue()
    assert _bound(still, "/World/ivy/Stems") == "/World/looks/bark"


def test_the_curves_are_hidden_once_swept(still) -> None:
    curves = still.GetPrimAtPath("/World/ivy/Curves")
    assert UsdGeom.Imageable(curves).ComputeVisibility() == UsdGeom.Tokens.invisible


def test_the_ivy_grows_on_the_painted_front_of_the_wall(still) -> None:
    """The brush painted the face at z = +0.2. Most of the vine is there, not on the buried
    underside or the back."""
    curves = np.asarray(UsdGeom.BasisCurves(still.GetPrimAtPath("/World/ivy/Curves")).GetPointsAttr().Get())
    assert np.mean(curves[:, 2] > 0.19) > 0.75
    assert curves[:, 1].min() > -0.05


def test_the_leaves_are_instanced_and_shaded(still) -> None:
    foliage = UsdGeom.PointInstancer(still.GetPrimAtPath("/World/ivy/Foliage"))
    prototype = foliage.GetPrototypesRel().GetTargets()[0]

    assert len(foliage.GetPositionsAttr().Get()) > 100
    assert _bound(still, prototype) == "/World/looks/leaf"


def test_the_scene_has_light_and_a_camera(still) -> None:
    assert still.GetPrimAtPath("/World/lights/sun").IsValid()
    assert still.GetPrimAtPath("/World/lights/sky").IsValid()
    assert UsdGeom.Camera(still.GetPrimAtPath("/World/cam"))


def test_animated_the_mesh_and_the_leaves_grow_in_together(animated) -> None:
    stems = UsdGeom.Mesh(animated.GetPrimAtPath("/World/ivy/Stems")).GetPointsAttr()
    foliage = UsdGeom.PointInstancer(animated.GetPrimAtPath("/World/ivy/Foliage"))
    leaves = len(foliage.GetPositionsAttr().Get())
    shown = [leaves - len(foliage.GetInvisibleIdsAttr().Get(f) or []) for f in (1, 24, 48)]

    assert len(stems.GetTimeSamples()) == 48
    assert [len(stems.Get(f)) > 0 for f in (1, 24, 48)] == [False, True, True]
    assert len(stems.Get(24)) < len(stems.Get(48))
    assert shown[0] == 0 and 0 < shown[1] < shown[2] == leaves
