"""Frustum Prune: what the camera cannot see is pruned, padding and Keep hold the rest back.

* a prim wholly outside the view is deactivated and flagged; one inside stays;
* Padding keeps what is just outside the frame, behind the camera too;
* Keep, lights and cameras are never pruned;
* a group across the edge loses only its outside children;
* a PointInstancer's outside instances go into inactiveIds, and a prototype none left on uses
  is pruned;
* a shifted window (a Blender shift) moves the view with it.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Usd, UsdGeom, UsdLux

from usd_chunks import Scene


def _cube(stage, path, at, size=1.0):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(size)
    cube.CreateExtentAttr([(-size / 2,) * 3, (size / 2,) * 3])
    UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(*at))
    return cube.GetPrim()


def _stage(*cubes, offset=0.0):
    """A 36 x 24 mm, 35 mm camera at the origin looking down -z, far clip 100, and cubes."""
    stage = Usd.Stage.CreateInMemory()
    cam = UsdGeom.Camera.Define(stage, "/cam")
    cam.CreateFocalLengthAttr(35.0)
    cam.CreateHorizontalApertureAttr(36.0)
    cam.CreateVerticalApertureAttr(24.0)
    cam.CreateVerticalApertureOffsetAttr(offset)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, 100.0))
    for path, at in cubes:
        _cube(stage, path, at)
    return stage


def _prune(stage, **params):
    """Cook a Frustum Prune on *stage*; the readout, what is active, and the instancers'
    masks -- read while the scene is open, as its layer leaves with it."""
    with Scene(stage) as scene:
        chunk = scene.create.frustum_prune(camera="/cam", **params)
        s = scene.get_stage()
        everything = list(s.Traverse(Usd.PrimAllPrimsPredicate))
        active = {str(p.GetPath()): p.IsActive() for p in everything}
        # A flat key with a colon in it, as Prune Prim writes it: GetCustomDataByKey would
        # read the colon as a path into nested dicts.
        pruned = {str(p.GetPath()) for p in everything if p.GetCustomData().get("usd_chunks:pruned")}
        masks = {str(p.GetPath()): list(UsdGeom.PointInstancer(p).ComputeMaskAtTime(Usd.TimeCode.Default()) or [])
                 for p in everything if p.IsA(UsdGeom.PointInstancer)}
        return chunk.get_param("readout").value, active, pruned, masks


def test_outside_goes_inside_stays():
    stage = _stage(("/World/front", (0, 0, -10)), ("/World/behind", (0, 0, 30)),
                   ("/World/aside", (60, 0, -10)), ("/World/beyond_far", (0, 0, -150)))
    readout, active, pruned, _ = _prune(stage, padding=0.0)
    assert active["/World/front"]
    gone = {"/World/behind", "/World/aside", "/World/beyond_far"}
    assert not any(active[p] for p in gone)
    assert pruned == gone
    assert readout.startswith("3 prims pruned")


@pytest.mark.parametrize("padding,kept", [(0.0, False), (3.0, True)])
def test_padding_keeps_what_is_just_out_of_frame_and_behind(padding, kept):
    # The half-width is ~5.1 at 10 m; a mirror sees what is just behind the camera.
    stage = _stage(("/World/at_the_edge", (8, 0, -10)), ("/World/just_behind", (0, 0, 2)))
    _, active, _, _ = _prune(stage, padding=padding)
    assert active["/World/at_the_edge"] is kept and active["/World/just_behind"] is kept


def test_keep_lights_and_cameras_are_never_pruned():
    stage = _stage(("/World/blocker", (0, 50, 0)))
    UsdLux.DistantLight.Define(stage, "/World/sun")
    UsdLux.SphereLight.Define(stage, "/World/lamp").AddTranslateOp().Set(Gf.Vec3d(0, 0, 50))
    UsdGeom.Camera.Define(stage, "/World/other_cam").AddTranslateOp().Set(Gf.Vec3d(0, 0, 50))
    _, active, pruned, _ = _prune(stage, padding=0.0, keep="/World/blocker")
    for kept in ("/World/blocker", "/World/sun", "/World/lamp", "/World/other_cam"):
        assert active[kept], kept
    assert not pruned


def test_a_group_across_the_edge_loses_only_its_outside_children():
    stage = _stage(("/World/group/seen", (0, 0, -10)), ("/World/group/unseen", (0, 0, 30)))
    readout, active, pruned, _ = _prune(stage, padding=0.0)
    assert active["/World/group"] and active["/World/group/seen"]
    assert pruned == {"/World/group/unseen"}
    assert readout.startswith("1 prims pruned")


def _forest():
    stage = _stage()
    inst = UsdGeom.PointInstancer.Define(stage, "/World/trees")
    _cube(stage, "/World/trees/proto/tree", (0, 0, 0))
    inst.CreatePrototypesRel().SetTargets(["/World/trees/proto"])
    inst.CreatePositionsAttr([(0, 0, -10), (60, 0, -10), (0, 0, -20), (0, 0, 40)])
    inst.CreateProtoIndicesAttr([0, 0, 0, 0])
    return stage


def test_an_instancer_switches_off_its_outside_instances():
    readout, active, pruned, masks = _prune(_forest(), padding=0.0)
    assert active["/World/trees"] and not pruned
    assert masks["/World/trees"] == [True, False, True, False]
    assert "2 of 4 instances off" in readout


def test_prune_instances_off_leaves_the_instancer_whole():
    _, _, _, masks = _prune(_forest(), padding=0.0, prune_instances=False)
    assert masks["/World/trees"] in ([], [True] * 4)


@pytest.mark.parametrize("offset,seen,unseen", [(0.0, "/World/centre", "/World/high"),
                                                (30.0, "/World/high", "/World/centre")])
def test_a_shifted_window_moves_the_view(offset, seen, unseen):
    # 30 mm up at 35 mm: the frame at 10 m spans y 5.1..12 instead of -3.4..3.4.
    stage = _stage(("/World/centre", (0, 0, -10)), ("/World/high", (0, 9, -10)), offset=offset)
    _, active, _, _ = _prune(stage, padding=0.0)
    assert active[seen] and not active[unseen]


def test_no_camera_prunes_nothing():
    stage = _stage(("/World/behind", (0, 0, 30)))
    with Scene(stage) as scene:
        chunk = scene.create.frustum_prune(camera="")
        assert "no camera" in chunk.get_param("readout").value
        assert scene.get_stage().GetPrimAtPath("/World/behind").IsActive()


def _two_kinds(in_view, out_of_view, inactive=()):
    """An instancer with two prototypes: kind 0 at *in_view*, kind 1 at *out_of_view*."""
    stage = _stage()
    inst = UsdGeom.PointInstancer.Define(stage, "/World/plants")
    _cube(stage, "/World/plants/proto/fern/geo", (0, 0, 0))
    _cube(stage, "/World/plants/proto/bush/geo", (0, 0, 0))
    inst.CreatePrototypesRel().SetTargets(["/World/plants/proto/fern", "/World/plants/proto/bush"])
    inst.CreatePositionsAttr([*in_view, *out_of_view])
    inst.CreateProtoIndicesAttr([0] * len(in_view) + [1] * len(out_of_view))
    if inactive:
        inst.DeactivateIds(list(inactive))
    return stage


def test_a_prototype_no_instance_left_on_uses_is_pruned():
    # Every bush is out of view: the bush prototype would be built for nothing.
    stage = _two_kinds([(0, 0, -10), (1, 0, -12)], [(60, 0, -10), (0, 0, 40)])
    readout, active, pruned, _ = _prune(stage, padding=0.0)
    assert active["/World/plants/proto/fern"]
    assert not active["/World/plants/proto/bush"]
    assert "/World/plants/proto/bush" in pruned
    assert "1 prototypes unused" in readout


def test_a_prototype_used_in_view_stays():
    stage = _two_kinds([(0, 0, -10)], [(0, 0, -15), (60, 0, -10)])
    _, active, _, _ = _prune(stage, padding=0.0)
    assert active["/World/plants/proto/fern"] and active["/World/plants/proto/bush"]


def test_an_instance_already_off_does_not_keep_its_prototype():
    # The one bush in view was switched off before: nothing on uses the bush prototype.
    stage = _two_kinds([(0, 0, -10)], [(0, 0, -15)], inactive=(1,))
    _, active, _, _ = _prune(stage, padding=0.0)
    assert not active["/World/plants/proto/bush"]


def test_a_prototype_another_instancer_uses_stays():
    # A second instancer, wholly in view (never examined), targets the bush prototype.
    stage = _two_kinds([(0, 0, -10)], [(60, 0, -10)])
    other = UsdGeom.PointInstancer.Define(stage, "/World/hedge")
    other.CreatePrototypesRel().SetTargets(["/World/plants/proto/bush"])
    other.CreatePositionsAttr([(0, 0, -20)])
    other.CreateProtoIndicesAttr([0])
    _, active, _, _ = _prune(stage, padding=0.0)
    assert active["/World/plants/proto/bush"]


def test_prune_instances_off_keeps_every_prototype():
    stage = _two_kinds([(0, 0, -10)], [(60, 0, -10)])
    _, active, pruned, _ = _prune(stage, padding=0.0, prune_instances=False)
    assert active["/World/plants/proto/bush"] and not pruned
