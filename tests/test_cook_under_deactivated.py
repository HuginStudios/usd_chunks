"""A chunk cooks again under a prim a chunk below it deactivated, as it does in a fresh run.

Merge Instancers deactivates the Scatters it merges, in its own layer. A fresh run cooks the
Scatter while that layer is still empty; editing the Scatter afterwards cooked it with the
deactivation in place, and USD refused to define its prototypes under its own, now inactive,
instancer ("Failed to define UsdPrim").
"""

from __future__ import annotations

from pxr import UsdGeom

from usd_chunks.chunk_script import Scene


def _width(stage):
    points = UsdGeom.Mesh(stage.GetPrimAtPath("/I/box")).GetPointsAttr().Get()
    return round(max(p[0] for p in points) - min(p[0] for p in points), 3)


def test_an_edit_above_a_prune_of_its_parent_cooks_and_stays_pruned():
    with Scene() as scene:
        stage = scene.get_stage()
        box = scene.create.cube(prim_path="/I/box", name="box")
        prune = scene.create.prune_prim(box, prim_path="/I", name="prune")
        assert not stage.GetPrimAtPath("/I").IsActive()

        box.get_param("cube.width").value = 3.0

        assert not stage.GetPrimAtPath("/I").IsActive()
        # What the cook wrote under the prune, seen once it is lifted.
        prune.get_param("prim_path").value = ""
        assert _width(stage) == 3.0


def test_a_prune_below_that_does_not_cook_again_keeps_its_opinion(monkeypatch):
    """Lifted for the cook above it and put back: a chunk below that skips still holds it."""
    with Scene() as scene:
        stage = scene.get_stage()
        box = scene.create.cube(prim_path="/I/box", name="box")
        prune = scene.create.prune_prim(box, prim_path="/I", name="prune")
        monkeypatch.setattr(type(prune), "_cook", lambda self: False)

        box.get_param("cube.width").value = 3.0

        assert not stage.GetPrimAtPath("/I").IsActive()
