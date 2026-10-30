"""What drives a graph: chunk_stage.graph_driver, the one rule the load, the graph and a group's recook share.

Shown, a graph goes by the View its saved slot names, else its Output, else its lowest-slot
View (a file from before Outputs). Handed to its parent, a group goes by its Output, else its
active View, else the end of its chain.
"""
from __future__ import annotations

from pxr import Usd

from usd_chunks import Scene, chunk_stage
from usd_chunks.graph import engine


def _set_slot(stage, view, slot: int) -> None:
    """Give a View another slot; 0 makes it inert, as a View made from the chunk menu is."""
    prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(view))
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        prim.GetAttribute("view:slot").Set(slot)


def test_a_shown_graph_takes_its_saved_view_then_its_output_then_its_lowest_view() -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        a = scene.create.prim(primpath_location="/World/a", name="a")
        b = scene.create.prim(primpath_location="/World/b", name="b")
        view3 = scene.set_view(a, view=3)
        view2 = scene.set_view(b, view=2)
        root = scene.peek_root_context()

        assert chunk_stage.graph_driver(stage, scene, root) == (view2, False), "the saved View"
        scene.set_view(a, view=3)
        assert chunk_stage.graph_driver(stage, scene, root) == (view3, False), "whichever slot is saved"

        output = scene.set_output(a)
        assert chunk_stage.graph_driver(stage, scene, root) == (output, False), "picking the Output clears the slot"

        scene.remove_chunk(output)
        assert chunk_stage.graph_driver(stage, scene, root) == (view2, False), "no slot, no Output: the lowest slot"

        _set_slot(stage, view2, 0)
        assert chunk_stage.graph_driver(stage, scene, root) == (view3, False), "an inert View can't be made active"


def test_a_group_with_no_output_or_saved_view_goes_by_its_lowest_slot_not_its_first_view() -> None:
    """It went by the first View the scene lists (here the last one made); now by the lowest
    slot, as a shown graph does."""
    with Scene() as scene:
        stage = scene.get_stage()
        with scene.create.chunkgroup(name="grp") as group:
            a = scene.create.prim(primpath_location="/World/a", name="a")
            b = scene.create.prim(primpath_location="/World/b", name="b")
            view2 = scene.set_view(a, view=2)
            scene.set_view(b, view=3)
        group_path = chunk_stage.get_chunk_prim_path(group)
        engine.write_active_slot(stage, group_path, 0)
        assert engine.read_active_slot(stage, group_path) == 0, "precondition: no saved slot"

        assert chunk_stage.graph_driver(stage, scene, group, deliver=True) == (view2, False)
        assert chunk_stage.find_group_active_view(group, stage, scene) is view2


def test_a_group_hands_on_its_output_before_a_saved_view() -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        with scene.create.chunkgroup(name="grp") as group:
            a = scene.create.prim(primpath_location="/World/a", name="a")
            output = scene.set_output(a)
            view = scene.set_view(a)
        assert chunk_stage.graph_driver(stage, scene, group) == (view, False), "shown: the saved View"
        assert chunk_stage.graph_driver(stage, scene, group, deliver=True) == (output, False), "handed on: the Output"
