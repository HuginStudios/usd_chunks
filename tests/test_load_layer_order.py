"""A loaded network's chunk layers are in the order its graph's recook keeps them in.

A load sorted the layers by wiring depth, which mixes a Merge's branches by length; the
graph's recook for its Output ranks each branch together, the first input strongest. So a
network opened by a script composed differently than in a graph, and in a graph the recook
moved the layers after the load had cooked, so the first edit after opening cooked every
chunk below it.
"""
from __future__ import annotations

from pxr import UsdGeom

from usd_chunks import Scene, chunk_stage, skip_cook


def _two_branches(scene):
    """A sphere, then a Merge of a one-chunk branch (radius 1) and a three-chunk one (7)."""
    ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
    first = scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 1", name="first")
    second = ball
    for i, radius in enumerate((5, 6, 7)):
        second = scene.create.property_editor(second, prim="/World/ball", edits=f"double radius = {radius}",
                                              name=f"second{i}")
    scene.set_output(scene.create.merge(first, second, name="merge"))


def _radius(scene):
    return UsdGeom.Sphere(scene.get_stage().GetPrimAtPath("/World/ball")).GetRadiusAttr().Get()


def _deliver(scene):
    """What a graph does once it is built: recook for its Output."""
    any_chunk = next(c for c in scene.get_chunks() if c.name == "first")
    _, context, _, _ = scene._active_view_of(any_chunk)
    chunk_stage.recook_for_view(scene.get_stage(), scene, context, None)


def test_a_reopened_merge_still_lets_its_first_input_win(tmp_path) -> None:
    path = str(tmp_path / "merge.usda")
    with Scene() as scene:
        _two_branches(scene)
        assert _radius(scene) == 1.0
        scene.save(path)

    with Scene.open(path) as scene:
        assert _radius(scene) == 1.0, "opened by a script, the longer second branch won"
        _deliver(scene)
        assert _radius(scene) == 1.0


def test_the_graphs_first_recook_after_opening_moves_no_layer(tmp_path) -> None:
    path = str(tmp_path / "merge.usda")
    with Scene() as scene:
        _two_branches(scene)
        scene.save(path)

    with Scene.open(path) as scene:
        stage = scene.get_stage()
        before = skip_cook.scene_state(stage)
        _deliver(scene)
        assert skip_cook.scene_state(stage) == before
        stale = [c.name for c in scene.get_chunks()
                 if skip_cook.record(c).scene is not None and skip_cook.record(c).scene != before]
        assert not stale, f"these could not skip their next cook: {stale}"
