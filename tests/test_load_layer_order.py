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
from usd_chunks.graph import engine


def _two_branches(scene, output=True):
    """A sphere, then a Merge of a one-chunk branch (radius 1) and a three-chunk one (7)."""
    ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
    first = scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 1", name="first")
    second = ball
    for i, radius in enumerate((5, 6, 7)):
        second = scene.create.property_editor(second, prim="/World/ball", edits=f"double radius = {radius}",
                                              name=f"second{i}")
    merge = scene.create.merge(second, first, name="merge")  # first in slot 0
    if output:
        scene.set_output(merge)
    return merge


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


def test_a_group_ranks_by_its_output_even_with_an_inner_view_saved(tmp_path) -> None:
    """Inside a group the graph's recook goes by the group's Output, not a View saved inside it
    (graph_driver). The load went by the saved View and the longer branch won."""
    path = str(tmp_path / "group.usda")
    with Scene() as scene:
        with scene.create.chunkgroup(name="grp") as group:
            ball = scene.create.prim(primpath_location="/World/ball", prim_type="Sphere", name="ball")
            first = scene.create.property_editor(ball, prim="/World/ball", edits="double radius = 1", name="first")
            second = ball
            for i, radius in enumerate((5, 6, 7)):
                second = scene.create.property_editor(second, prim="/World/ball", edits=f"double radius = {radius}",
                                                      name=f"second{i}")
            scene.set_output(scene.create.merge(second, first, name="merge"))
            scene.set_view(second)
        scene.set_output(group)
        built = _radius(scene)
        scene.save(path)
    assert built == 1.0

    with Scene.open(path) as scene:
        assert _radius(scene) == 1.0, "opened by a script, the inner View's branch won"
        stage = scene.get_stage()
        before = skip_cook.scene_state(stage)
        _deliver(scene)
        assert _radius(scene) == 1.0
        stale = [c.name for c in scene.get_chunks()
                 if skip_cook.record(c).scene is not None and skip_cook.record(c).scene != skip_cook.scene_state(stage)]
        assert skip_cook.scene_state(stage) == before and not stale, stale


def test_a_file_from_before_outputs_ranks_by_its_lowest_view(tmp_path) -> None:
    """No Output and no saved slot: the graph picks its lowest-slot View after opening
    (graph_driver), so the load ranks by that View too. It ranked by wiring depth."""
    path = str(tmp_path / "views.usda")
    with Scene() as scene:
        merge = _two_branches(scene, output=False)
        scene.set_view(merge)
        context_path, _, _, _ = scene._active_view_of(merge)
        engine.write_active_slot(scene.get_stage(), context_path, 0)
        scene.save(path)

    with Scene.open(path) as scene:
        stage = scene.get_stage()
        merge = next(c for c in scene.get_chunks() if c.name == "merge")
        context_path, context, slot, _ = scene._active_view_of(merge)
        assert slot == 0 and not chunk_stage.graph_outputs(stage, scene, context), "precondition"
        assert _radius(scene) == 1.0, "opened by a script, the longer second branch won"
        view = next(c for c in scene.get_chunks() if chunk_stage.is_view_chunk(c))
        before = skip_cook.scene_state(stage)
        chunk_stage.recook_for_view(stage, scene, context, view)
        assert _radius(scene) == 1.0
        assert skip_cook.scene_state(stage) == before, "the graph's first recook moved layers"
