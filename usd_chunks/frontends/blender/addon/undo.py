"""Ctrl+Z and Ctrl+Shift+Z for chunk networks.

Blender's undo restores the .blend -- node trees, every RNA value -- and knows nothing about
the stage. Undo used to only rebuild the tree from USD, so it never undid anything a chunk
network did: a param edit, a new node, a delete.

So the network rides along with Blender's undo. `chunk_undo.History` records each change
to the network's chunk prims as a step; the node tree holds only the id of the step the
network is at (`STEP_KEY`), and Blender stores that id with each undo step like any other
data on the tree. After an undo or redo, `restore` moves the history to the step Blender
put back, and the chunk system follows as it follows any layer edit.

Scope (docs/adr/0003): only chunk prims are undone. An edit to a prim that is not a chunk
-- scene data authored directly, another add-on or a script writing to the stage -- is
not recorded; a host that needs that undone uses the C++ usd-undo plugin or its own.

An ID property, not a declared RNA one: writing an ID property tags no depsgraph update,
so it cannot set off the add-on's own depsgraph handler on every change.
"""
from __future__ import annotations

import logging

import bpy

from usd_chunks import chunk_undo

logger = logging.getLogger(__name__)

#: The ID property on the node tree holding the id of the step the network is at. The
#: underscore hides it from Blender's Custom Properties panel.
STEP_KEY = "_usd_undo_step"

_state = {"history": None, "tree_name": None}


def track(tree, stage) -> None:
    """Record the network's chunk prims from now on, and put the step id on `tree`.

    Args:
        tree: The network's node tree.
        stage: The network's stage.
    """
    untrack()
    if tree is None or stage is None:
        return
    history = chunk_undo.History(stage, on_record=_on_record)
    _state["history"] = history
    _state["tree_name"] = tree.name
    history.listen()
    _write(tree, history.key())


def untrack() -> None:
    """Stop recording. Called when another network replaces this one."""
    if _state["history"] is not None:
        _state["history"].close()
    _state.update(history=None, tree_name=None)


def current_history() -> chunk_undo.History | None:
    """The history being recorded, if a network is open."""
    return _state["history"]


def _tree():
    name = _state["tree_name"]
    return bpy.data.node_groups.get(name) if name else None


def _write(tree, key: str) -> bool:
    """Store the step id on the tree. False when Blender refuses the write right now."""
    try:
        if tree.get(STEP_KEY) != key:
            tree[STEP_KEY] = key
    except AttributeError:
        # Blender forbids writing ID data while it draws. A change made from a draw
        # callback is stored on the next tick instead.
        return False
    return True


def _flush():
    tree, history = _tree(), _state["history"]
    if tree is not None and history is not None:
        _write(tree, history.key())
    return None


def _on_record(history: chunk_undo.History) -> None:
    tree = _tree()
    if tree is not None and not _write(tree, history.key()) \
            and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush, first_interval=0.0)


def restore(tree, stage) -> bool:
    """Put the network back to the step an undo or redo restored on `tree`.

    Args:
        tree: The network's node tree, as the undo step left it.
        stage: The network's stage.

    Returns:
        True if the layer changed.
    """
    history = _state["history"]
    if tree is None or stage is None or history is None \
            or history.stage != stage:
        return False
    return history.move_to(tree.get(STEP_KEY))
