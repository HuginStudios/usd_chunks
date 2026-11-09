"""Transform's Aim: turn a prim's -Z axis, the way lights and cameras face, toward a prim or a point.

Agents pointing a light at a table, or a sun down at 45 degrees, worked the angles out by
hand. Checked from the composed result, so it holds however the rotation is spelled. The
rotation lands in the Rotate params, so the sliders and a saved file read what it does.
"""
from __future__ import annotations

import math

import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene


@pytest.fixture
def scene():
    with Scene(host_name="pytest") as scene:
        yield scene


def _world(stage, path):
    return UsdGeom.Xformable(stage.GetPrimAtPath(path)).ComputeLocalToWorldTransform(0)


def _miss(stage, path, point):
    m = _world(stage, path)
    eye = m.Transform(Gf.Vec3d(0, 0, 0))
    forward = m.TransformDir(Gf.Vec3d(0, 0, -1)).GetNormalized()
    return math.degrees(math.acos(min(1.0, Gf.Dot(forward, (Gf.Vec3d(*point) - eye).GetNormalized()))))


def _roll(stage, path):
    side = _world(stage, path).TransformDir(Gf.Vec3d(1, 0, 0)).GetNormalized()
    return math.degrees(math.asin(max(-1.0, min(1.0, side[1]))))


def test_a_light_aimed_at_a_point(scene):
    key = scene.create.rect_light(primpath_location="/World/key")
    aim = scene.create.transform(key, primpath_location="/World/key", tx=3.0, ty=4.0, tz=3.0,
                                 aim_at_point=True, aim_point=(0.0, 0.75, 0.0))
    scene.set_output(aim)
    stage = scene.get_stage()
    assert _miss(stage, "/World/key", (0, 0.75, 0)) < 1e-3
    assert abs(_roll(stage, "/World/key")) < 1e-3
    assert _world(stage, "/World/key").ExtractTranslation() == pytest.approx(Gf.Vec3d(3, 4, 3), abs=1e-9)
    assert aim.get_param("rz").value == pytest.approx(0.0, abs=1e-6)


def test_a_light_aimed_at_a_prim_follows_it(scene):
    table = scene.create.cube(prim_path="/World/table", width=1.5, height=0.75, depth=0.8)
    key = scene.create.rect_light(table, primpath_location="/World/key")
    aim = scene.create.transform(key, primpath_location="/World/key", tx=3.0, ty=4.0, tz=3.0,
                                 aim_at="/World/table")
    scene.set_output(aim)
    stage = scene.get_stage()
    centre = UsdGeom.BBoxCache(0, ["default"]).ComputeWorldBound(stage.GetPrimAtPath("/World/table")).ComputeCentroid()
    assert _miss(stage, "/World/key", centre) < 1e-3
    scene.set(aim, tx=-2.0, tz=5.0)
    assert _miss(stage, "/World/key", centre) < 1e-3


def test_the_point_wins_over_the_prim(scene):
    table = scene.create.cube(prim_path="/World/table")
    key = scene.create.rect_light(table, primpath_location="/World/key")
    aim = scene.create.transform(key, primpath_location="/World/key", tx=3.0, ty=4.0, tz=3.0,
                                 aim_at="/World/table", aim_at_point=True, aim_point=(10.0, 0.0, 0.0))
    scene.set_output(aim)
    assert _miss(scene.get_stage(), "/World/key", (10, 0, 0)) < 1e-3


def test_aimed_in_world_space_under_a_moved_parent_and_an_op_above(scene):
    rig = scene.create.prim(primpath_location="/World/rig", prim_type="Xform")
    moved = scene.create.transform(rig, primpath_location="/World/rig", tx=10.0, ry=90.0, sx=2.0, sy=2.0, sz=2.0)
    key = scene.create.rect_light(moved, primpath_location="/World/rig/key")
    tilt = scene.create.transform(key, primpath_location="/World/rig/key", rz=30.0)  # an op above the aim
    aim = scene.create.transform(tilt, primpath_location="/World/rig/key", tx=1.0, ty=2.0, tz=0.5,
                                 aim_at_point=True, aim_point=(0.0, 0.0, 0.0))
    scene.set_output(aim)
    assert _miss(scene.get_stage(), "/World/rig/key", (0, 0, 0)) < 1e-3


def test_off_by_default_and_unchecking_keeps_the_last_facing(scene):
    key = scene.create.rect_light(primpath_location="/World/key")
    aim = scene.create.transform(key, primpath_location="/World/key", tx=3.0, ty=4.0, tz=3.0, ry=12.0)
    scene.set_output(aim)
    assert aim.get_param("ry").value == pytest.approx(12.0)  # no aim: Rotate is left alone
    scene.set(aim, aim_at_point=True, aim_point=(0.0, 0.0, 0.0))
    facing = [aim.get_param(n).value for n in ("rx", "ry", "rz")]
    scene.set(aim, aim_at_point=False)
    assert [aim.get_param(n).value for n in ("rx", "ry", "rz")] == pytest.approx(facing)
    assert _miss(scene.get_stage(), "/World/key", (0, 0, 0)) < 1e-3


def test_a_selection_fills_prim_path_but_never_the_aim(scene, monkeypatch):
    """In usdview a new chunk's prim params fill from the selection. Aim At filled too, so a
    Transform made with the box selected turned toward the box; an agent had to clear it."""
    box = scene.create.cube(prim_path="/World/box")
    selected = scene.get_stage().GetPrimAtPath("/World/box")
    monkeypatch.setattr(type(scene), "get_selected_prims", lambda self: [selected])
    move = scene.create.transform(box, tx=2.0)
    assert move.get_param("primpath_location").value == "/World/box"  # still follows the selection
    assert move.get_param("aim_at").value == ""
    assert [move.get_param(n).value for n in ("rx", "ry", "rz")] == [0.0, 0.0, 0.0]
