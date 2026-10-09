"""scene.set_view(chunk) and scene.set_output(chunk): the graph's Set View and Set Output.

set_view shows a chunk in a View and cooks through it; set_output ends the graph at a
chunk and cooks to the Output. Before set_view, a script had to call
view.add_dependency(chunk): that kept the View's old wire as well, and did not re-cook
through the View, so the viewport could stay unchanged.
"""

from __future__ import annotations

import pytest

from usd_chunks import Scene, chunk_stage
from usd_chunks.graph import engine


def _wires(scene, end):
    prim = scene.get_stage().GetPrimAtPath(chunk_stage.get_chunk_prim_path(end))
    return [str(p.GetPrimPath()).rsplit("/", 1)[-1] for p in prim.GetAttribute("inputs:chunk").GetConnections()]


def _is_off(scene, chunk):
    layer = chunk.get_edit_context()
    return layer is not None and scene.get_stage().IsLayerMuted(layer.identifier)


def _y(scene, path):
    from pxr import UsdGeom

    prim = scene.get_stage().GetPrimAtPath(path)
    if not (prim and prim.IsDefined()):
        return None
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()[1]


def _active_slot(scene, chunk):
    context = str(chunk_stage.get_chunk_prim_path(chunk)).rsplit("/", 1)[0]
    return engine.read_active_slot(scene.get_stage(), context)


def _two_branches(scene):
    """A box lifted to 3 and a ball lifted to 7, not wired to each other."""
    box = scene.create.cube(prim_path="/box")
    lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)
    ball = scene.create.sphere(prim_path="/ball")
    raised = scene.create.transform(ball, primpath_location=ball.get_param("prim_path"), ty=7.0)
    return box, lift, ball, raised


def test_set_view_makes_view_2_when_there_is_none() -> None:
    with Scene.new() as scene:
        box = scene.create.cube(prim_path="/box")
        lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

        view = scene.set_view(lift)

        assert view is not None and chunk_stage.is_view_chunk(view)
        assert view.name == "View2"
        assert _wires(scene, view) == ["Transform"]
        assert _y(scene, "/box") == 3.0


def test_set_view_takes_the_first_view_not_made_yet() -> None:
    with Scene.new() as scene:
        _, lift, _, raised = _two_branches(scene)

        view2 = scene.set_view(lift)
        view3 = scene.set_view(raised)

        assert (view2.name, view3.name) == ("View2", "View3")
        assert _wires(scene, view3) == ["Transform1"]
        assert _y(scene, "/ball") == 7.0
        assert _is_off(scene, lift), "View 3 is active, so View 2's branch is off"


def test_set_view_reuses_the_view_already_showing_the_chunk() -> None:
    with Scene.new() as scene:
        _, lift, _, raised = _two_branches(scene)
        view2 = scene.set_view(lift)
        scene.set_view(raised)

        assert scene.set_view(lift) is view2
        assert _y(scene, "/box") == 3.0 and not _is_off(scene, lift)
        assert _is_off(scene, raised)


def test_with_both_views_in_use_set_view_moves_the_active_one() -> None:
    with Scene.new() as scene:
        box, lift, _, raised = _two_branches(scene)
        scene.set_view(lift)
        view3 = scene.set_view(raised)

        assert scene.set_view(box) is view3
        assert _wires(scene, view3) == ["Mesh"], "the old wire is replaced"


def test_set_view_with_a_number_uses_that_view() -> None:
    with Scene.new() as scene:
        box, lift, _, _ = _two_branches(scene)
        view2 = scene.set_view(box)

        assert scene.set_view(lift, view=2) is view2
        assert _wires(scene, view2) == ["Transform"]
        view3 = scene.set_view(box, view=3)
        assert view3.name == "View3" and _wires(scene, view3) == ["Mesh"]

        with pytest.raises(ValueError, match="2, 3"):
            scene.set_view(box, view=1)


def test_a_chunk_made_outside_the_view_does_not_cook() -> None:
    """Like a node made in the graph: blocked while it is not in the View's chain."""
    with Scene.new() as scene:
        box = scene.create.cube(prim_path="/box")
        view = scene.set_view(box)
        lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

        assert _wires(scene, view) == ["Mesh"], "the View is not moved"
        assert lift.is_blocked()
        assert _y(scene, "/box") == 0.0, "the viewport matches the graph: the box has not moved"

        scene.set_view(lift)
        assert not lift.is_blocked()
        assert _y(scene, "/box") == 3.0


def test_set_output_makes_an_output_and_cooks_to_it() -> None:
    with Scene.new() as scene:
        _, lift, _, raised = _two_branches(scene)

        output = scene.set_output(lift)

        assert output is not None and chunk_stage.is_output_chunk(output)
        assert _wires(scene, output) == ["Transform"]
        assert _y(scene, "/box") == 3.0
        assert _is_off(scene, raised), "only the Output's chain cooks"


def test_set_output_moves_the_output_and_leaves_the_view() -> None:
    """The graph cooks to the Output from then on; the View stays, just not active."""
    with Scene.new() as scene:
        _, lift, _, raised = _two_branches(scene)
        output = scene.set_output(lift)
        view = scene.set_view(raised)
        assert _active_slot(scene, lift) == 1 and _is_off(scene, lift)

        assert scene.set_output(raised) is output
        assert _wires(scene, output) == ["Transform1"]
        assert _active_slot(scene, lift) == 0, "no View is active any more"
        assert _wires(scene, view) == ["Transform1"], "the View is not touched"
        assert not _is_off(scene, raised) and _is_off(scene, lift)


def test_set_output_refuses_a_view_or_an_output() -> None:
    with Scene.new() as scene:
        box = scene.create.cube(prim_path="/box")
        output = scene.set_output(box)
        view = scene.set_view(box)

        assert scene.set_output(view) is None
        assert scene.set_output(output) is None
