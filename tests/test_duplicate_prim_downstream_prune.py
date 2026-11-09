"""Duplicate Prim's copies stay active when a Prune Prim below it switches the source off.

A cook sees only what is wired above it. On Python 3.13 every masked cook raised inside
skip_cook (a comprehension over Sdf.Path.GetAncestorsRange()), so every chunk fell back to
cooking on the whole stage, where the layers of the chunks below it are not hidden. Duplicate
Prim then copied the source as Prune Prim had left it, and every copy came out inactive. An
agent laying out a ring of columns hit it.
"""
from __future__ import annotations

import contextlib

import pytest

from usd_chunks import Scene, chunk


@pytest.mark.parametrize("block", [False, True], ids=["one-by-one", "change_block"])
def test_copies_stay_active_when_the_source_is_pruned_below(block: bool) -> None:
    with Scene() as scene:
        with chunk.change_block() if block else contextlib.nullcontext():
            src = scene.create.usd_cylinder(prim_path="/World/column")
            ring = scene.create.duplicate_prim(src, source_prim="/World/column", dest_path="/World/columns",
                                               copies=4, copy_rotate=(0.0, 90.0, 0.0))
            pruned = scene.create.prune_prim(ring, prim_path="/World/column")
        scene.set_output(pruned)
        stage = scene.get_stage()
        copies = stage.GetPrimAtPath("/World/columns").GetChildren()
        assert [p.GetName() for p in copies] == [f"column_{i:02d}" for i in range(4)]
        assert all(p.IsActive() for p in copies), [str(p.GetPath()) for p in copies if not p.IsActive()]
        assert not stage.GetPrimAtPath("/World/column").IsActive()


def test_copies_stay_active_when_recooked_after_the_prune() -> None:
    """Not only a change_block: any cook of Duplicate Prim after the Prune below it exists."""
    with Scene() as scene:
        src = scene.create.usd_cylinder(prim_path="/World/column")
        ring = scene.create.duplicate_prim(src, source_prim="/World/column", dest_path="/World/columns",
                                           copies=4, copy_rotate=(0.0, 90.0, 0.0))
        pruned = scene.create.prune_prim(ring, prim_path="/World/column")
        scene.set_output(pruned)
        scene.set(ring, copies=6, copy_rotate=(0.0, 60.0, 0.0))
        copies = scene.get_stage().GetPrimAtPath("/World/columns").GetChildren()
        assert len(copies) == 6
        assert all(p.IsActive() for p in copies), [str(p.GetPath()) for p in copies if not p.IsActive()]
