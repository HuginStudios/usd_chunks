"""Duplicate Prim makes many copies, each moved by a per-copy offset one more time.

There was no way to lay out N copies of a prim -- a row, a ring, a spiral staircase -- short
of a chunk per copy or a script. An agent asked for a spiral staircase wrote a chunk of its
own for it.
"""
from __future__ import annotations

import contextlib
import math

import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene, chunk


def _staircase(scene, copies=20):
    step = scene.create.cube(prim_path="/World/step", width=1.0, height=0.2, depth=0.4)
    placed = scene.create.transform(step, primpath_location="/World/step", tx=2.0)
    stairs = scene.create.duplicate_prim(placed, source_prim="/World/step", dest_path="/World/stairs",
                                         copies=copies, copy_rotate=(0.0, -18.0, 0.0),
                                         copy_translate=(0.0, 0.2, 0.0))
    scene.set_output(stairs)
    return stairs


def _box(stage, path):
    box = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_]).ComputeWorldBound(stage.GetPrimAtPath(path))
    m, r = box.GetMatrix(), box.GetRange()
    return m.Transform(r.GetMidpoint()), m.TransformDir(Gf.Vec3d(r.GetSize()[0], 0, 0))


def test_a_spiral_staircase() -> None:
    with Scene() as scene:
        _staircase(scene)
        stage = scene.get_stage()
        names = [p.GetName() for p in stage.GetPrimAtPath("/World/stairs").GetChildren()]
        assert names == [f"step_{i:02d}" for i in range(20)]
        for i in range(20):
            a = math.radians(18 * i)
            centre, long_side = _box(stage, f"/World/stairs/step_{i:02d}")
            assert Gf.IsClose(centre, Gf.Vec3d(2 * math.cos(a), 0.1 + 0.2 * i, 2 * math.sin(a)), 1e-5), (i, centre)
            radial = Gf.Vec3d(math.cos(a), 0, math.sin(a))
            assert abs(Gf.Dot(long_side.GetNormalized(), radial)) > 0.9999, (i, long_side)


def test_changing_the_count_rebuilds_the_copies() -> None:
    with Scene() as scene:
        stairs = _staircase(scene)
        scene.set(stairs, copies=5)
        children = scene.get_stage().GetPrimAtPath("/World/stairs").GetChildren()
        assert [p.GetName() for p in children] == [f"step_{i:02d}" for i in range(5)]


def test_one_copy_is_the_plain_duplicate_it_always_was() -> None:
    with Scene() as scene:
        step = scene.create.cube(prim_path="/World/step")
        moved = scene.create.transform(step, primpath_location="/World/step", tx=2.0)
        scene.set_output(scene.create.duplicate_prim(moved, source_prim="/World/step", dest_path="/World/twin",
                                                     copy_translate=(5.0, 0.0, 0.0)))
        stage = scene.get_stage()
        twin = stage.GetPrimAtPath("/World/twin")
        assert twin.GetTypeName() == "Mesh" and not twin.GetChildren()
        assert (UsdGeom.Xformable(twin).GetLocalTransformation()
                == UsdGeom.Xformable(stage.GetPrimAtPath("/World/step")).GetLocalTransformation())


@pytest.mark.parametrize("in_block", [False, True])
def test_a_destination_given_with_the_source_is_kept(in_block) -> None:
    """In a change_block, Source Prim's callback ran at the block's end and its suggestion
    replaced a destination given in the same create."""
    with Scene() as scene:
        with chunk.change_block() if in_block else contextlib.nullcontext():
            step = scene.create.cube(prim_path="/World/step")
            twin = scene.create.duplicate_prim(step, source_prim="/World/step", dest_path="/World/twin")
            scene.set_output(twin)
        assert twin.get_param("dest_path").value == "/World/twin"
        assert scene.get_stage().GetPrimAtPath("/World/twin").IsValid()


def test_an_empty_destination_still_gets_a_suggestion() -> None:
    with Scene() as scene:
        step = scene.create.cube(prim_path="/World/step")
        twin = scene.create.duplicate_prim(step, source_prim="/World/step")
        scene.set_output(twin)
        assert twin.get_param("dest_path").value == "/World/step_copy"
