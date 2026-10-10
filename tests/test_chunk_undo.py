"""chunk_undo: undo and redo for what chunks author, on any host.

Plain stages and Sdf edits -- no chunk system, no host. What is recorded is decided by the
stage (is the prim a Chunk?), so a hand-made `def Chunk` is as good as a real one here.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_undo


def _def_chunk(layer, path, size=None):
    spec = Sdf.CreatePrimInLayer(layer, path)
    spec.specifier, spec.typeName = Sdf.SpecifierDef, "Chunk"
    if size is not None:
        Sdf.AttributeSpec(spec, "inputs:size", Sdf.ValueTypeNames.Double).default = size
    return spec


def _size(layer, path):
    return layer.GetAttributeAtPath(f"{path}.inputs:size").default


@pytest.fixture
def stage():
    stage = Usd.Stage.CreateInMemory()
    _def_chunk(stage.GetRootLayer(), "/chunks/G", size=1.0)
    _def_chunk(stage.GetRootLayer(), "/chunks/G/C", size=2.0)
    return stage


@pytest.fixture
def history(stage):
    recorded = []
    history = chunk_undo.History(stage, on_record=lambda h: recorded.append(h.key()))
    history.recorded = recorded
    history.listen()
    yield history
    history.close()


def _touched(history):
    return {(lid, str(path)) for lid, path, _o, _n in history.changes[history.current]}


def test_a_change_records_only_the_chunk_prim_it_touched(stage, history):
    root = stage.GetRootLayer()
    root.GetAttributeAtPath("/chunks/G/C.inputs:size").default = 5.0
    assert _touched(history) == {(root.identifier, "/chunks/G/C")}
    assert history.recorded == [history.key()]


def test_a_prim_that_is_not_a_chunk_is_never_recorded(stage, history):
    Sdf.CreatePrimInLayer(stage.GetRootLayer(), "/geo").typeName = "Mesh"
    assert history.current == 0 and history.recorded == []


def test_undo_and_redo_put_an_edit_back(stage, history):
    root = stage.GetRootLayer()
    start = history.key()
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    edited = history.key()
    assert history.move_to(start)
    assert _size(root, "/chunks/G") == 1.0
    assert history.move_to(edited)
    assert _size(root, "/chunks/G") == 5.0


def test_undoing_a_parent_leaves_its_children_alone(stage, history):
    """A prim's own spec is restored, not its subtree: a child is a chunk of its own."""
    root = stage.GetRootLayer()
    root.GetAttributeAtPath("/chunks/G/C.inputs:size").default = 7.0
    after_child = history.key()
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    history.move_to(after_child)
    assert _size(root, "/chunks/G") == 1.0
    assert _size(root, "/chunks/G/C") == 7.0


def test_undoing_a_delete_brings_the_prim_and_its_children_back(stage, history):
    root = stage.GetRootLayer()
    start = history.key()
    edit = Sdf.BatchNamespaceEdit()
    edit.Add("/chunks/G", Sdf.Path.emptyPath)
    root.Apply(edit)
    assert not root.GetPrimAtPath("/chunks/G")
    history.move_to(start)
    assert _size(root, "/chunks/G") == 1.0
    assert _size(root, "/chunks/G/C") == 2.0


def test_undoing_an_add_removes_the_prim(stage, history):
    root = stage.GetRootLayer()
    start = history.key()
    _def_chunk(root, "/chunks/New", size=3.0)
    history.move_to(start)
    assert not root.GetPrimAtPath("/chunks/New")


def test_an_edit_after_an_undo_keeps_the_old_redo_reachable(stage, history):
    root = stage.GetRootLayer()
    start = history.key()
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    five = history.key()
    history.move_to(start)
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 8.0
    history.move_to(five)
    assert _size(root, "/chunks/G") == 5.0


def test_a_key_from_before_recording_is_the_stage_as_it_opened(stage, history):
    root = stage.GetRootLayer()
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    assert history.move_to(None)
    assert _size(root, "/chunks/G") == 1.0
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    assert history.move_to("deadbeef:1")
    assert _size(root, "/chunks/G") == 1.0


def test_a_restore_is_not_recorded_as_a_step(stage, history):
    root = stage.GetRootLayer()
    start = history.key()
    root.GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    steps = len(history.parents)
    history.move_to(start)
    assert len(history.parents) == steps
    assert history.key() == start


def test_a_chunk_outside_the_chunks_root_is_recorded(stage, history):
    """A network can live under an asset's own prim."""
    root = stage.GetRootLayer()
    _def_chunk(root, "/asset/net/Hosted", size=1.0)
    hosted = history.key()
    root.GetAttributeAtPath("/asset/net/Hosted.inputs:size").default = 4.0
    history.move_to(hosted)
    assert _size(root, "/asset/net/Hosted") == 1.0


def test_an_over_on_a_referenced_chunk_is_recorded(stage, history):
    """An edit to a network a Reference chunk brought in is an over with no type of its
    own: it counts because the stage says the prim is a chunk."""
    asset = Sdf.Layer.CreateAnonymous("asset.usda")
    _def_chunk(asset, "/Net")
    _def_chunk(asset, "/Net/A", size=1.0)
    ref = _def_chunk(stage.GetRootLayer(), "/asset/Ref")
    ref.referenceList.Prepend(Sdf.Reference(asset.identifier, "/Net"))
    before = history.key()
    size = stage.GetPrimAtPath("/asset/Ref/A").GetAttribute("inputs:size")
    size.Set(4.0)
    history.move_to(before)
    assert size.Get() == 1.0
    assert not stage.GetRootLayer().GetPrimAtPath("/asset/Ref/A")


def test_a_chunk_in_a_sublayer_added_later_is_recorded(stage, history):
    """The Edit Target can put chunk prims in a department layer added after opening."""
    dept = Sdf.Layer.CreateAnonymous("dept.usda")
    _def_chunk(dept, "/dept/C", size=1.0)
    stage.GetRootLayer().subLayerPaths.append(dept.identifier)
    before = history.key()
    dept.GetAttributeAtPath("/dept/C.inputs:size").default = 4.0
    assert _touched(history) == {(dept.identifier, "/dept/C")}
    history.move_to(before)
    assert _size(dept, "/dept/C") == 1.0


def test_close_stops_recording(stage, history):
    history.close()
    stage.GetRootLayer().GetAttributeAtPath("/chunks/G.inputs:size").default = 5.0
    assert history.current == 0


# ---------------------------------------------------------------------------
# UndoStack: a host with no undo of its own; a step runs from one input to the next
# ---------------------------------------------------------------------------

def _set(stage, path, value):
    stage.GetRootLayer().GetAttributeAtPath(f"{path}.inputs:size").default = value


@pytest.fixture
def undo_stack(stage):
    stack = chunk_undo.UndoStack(stage)
    yield stack
    stack.close()


def test_a_drag_is_one_undo(stage, undo_stack):
    undo_stack.note_input()  # the press
    for value in (2.0, 3.0, 4.0):  # its ticks
        _set(stage, "/chunks/G", value)
    assert undo_stack.undo()
    assert _size(stage.GetRootLayer(), "/chunks/G") == 1.0
    assert not undo_stack.can_undo()


def test_a_cook_after_an_edit_is_not_an_undo_of_its_own(stage, undo_stack):
    """A deferred cook writes the chunk prim a tick later, with no input in between."""
    undo_stack.note_input()
    _set(stage, "/chunks/G", 5.0)
    _set(stage, "/chunks/G/C", 9.0)  # the cook
    undo_stack.undo()
    root = stage.GetRootLayer()
    assert (_size(root, "/chunks/G"), _size(root, "/chunks/G/C")) == (1.0, 2.0)


def test_each_input_is_its_own_step_and_redo_walks_back(stage, undo_stack):
    root = stage.GetRootLayer()
    for value in (5.0, 6.0):
        undo_stack.note_input()
        _set(stage, "/chunks/G", value)
    undo_stack.undo()
    assert _size(root, "/chunks/G") == 5.0
    undo_stack.undo()
    assert _size(root, "/chunks/G") == 1.0
    undo_stack.redo()
    undo_stack.redo()
    assert _size(root, "/chunks/G") == 6.0
    assert not undo_stack.can_redo()


def test_an_edit_after_an_undo_drops_the_redo(stage, undo_stack):
    undo_stack.note_input()
    _set(stage, "/chunks/G", 5.0)
    undo_stack.undo()
    undo_stack.note_input()
    _set(stage, "/chunks/G", 7.0)
    assert not undo_stack.can_redo()
    assert not undo_stack.redo()


def test_a_cook_after_an_undo_keeps_the_redo(stage, undo_stack):
    """Ctrl+Z is not input: what the recook writes belongs to the step it went back to."""
    undo_stack.note_input()
    _set(stage, "/chunks/G", 5.0)
    undo_stack.undo()
    _set(stage, "/chunks/G/C", 9.0)  # the recook
    assert undo_stack.can_redo()
    undo_stack.redo()
    assert _size(stage.GetRootLayer(), "/chunks/G") == 5.0


def test_changes_before_any_input_are_where_undo_stops(stage, undo_stack):
    """Load-time cooks are the network as it opened."""
    _set(stage, "/chunks/G", 3.0)
    assert not undo_stack.can_undo()


def test_retrack_starts_an_empty_history_on_the_new_stage(stage, undo_stack):
    undo_stack.note_input()
    _set(stage, "/chunks/G", 5.0)
    other = Usd.Stage.CreateInMemory()
    _def_chunk(other.GetRootLayer(), "/chunks/X", size=1.0)
    undo_stack.retrack(other)
    assert not undo_stack.can_undo()
    undo_stack.note_input()
    _set(other, "/chunks/X", 2.0)
    undo_stack.undo()
    assert _size(other.GetRootLayer(), "/chunks/X") == 1.0
    assert _size(stage.GetRootLayer(), "/chunks/G") == 5.0
