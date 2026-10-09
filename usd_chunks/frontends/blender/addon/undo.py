"""Ctrl+Z and Ctrl+Shift+Z for chunk networks.

Blender's undo restores the .blend -- node trees, every RNA value -- and knows nothing about
the stage. Undo used to only rebuild the tree from USD, so it never undid anything a chunk
network did: a param edit, a new node, a delete.

So the network rides along with Blender's undo. Every chunk prim -- its params, its wires,
which view is active -- lives in the stage's root layer, and that layer's text is copied
into the node tree (`STATE_KEY`) whenever it changes. Blender stores the copy with each
undo step like any other data on the tree. After an undo or redo, `restore` puts the layer
back from the copy, and the chunk system follows as it follows any layer reload (the way
usdview's usd-undo works). Cooked output is not copied: it lives in each chunk's own layer
and cooking makes it again.

An ID property, not a declared RNA one: writing an ID property tags no depsgraph update,
so it cannot set off the add-on's own depsgraph handler on every change.
"""
from __future__ import annotations

import logging

import bpy
from pxr import Sdf, Tf

logger = logging.getLogger(__name__)

#: The ID property on the node tree holding the root layer's text. The underscore hides it
#: from Blender's Custom Properties panel.
STATE_KEY = "_usd_state"

_state = {"listener": None, "layer": None, "tree_name": None, "restoring": False}


def track(tree, stage) -> None:
    """Copy `stage`'s root layer into `tree` now and whenever it changes.

    Args:
        tree: The network's node tree.
        stage: The network's stage.
    """
    untrack()
    if tree is None or stage is None:
        return
    layer = stage.GetRootLayer()
    _state["layer"] = layer
    _state["tree_name"] = tree.name
    _state["listener"] = Tf.Notice.Register(
        Sdf.Notice.LayersDidChangeSentPerLayer, _on_layers_changed, layer)
    _write(tree, layer)


def untrack() -> None:
    """Stop copying. Called when another network replaces this one."""
    if _state["listener"] is not None:
        _state["listener"].Revoke()
    _state.update(listener=None, layer=None, tree_name=None)


def _tree():
    name = _state["tree_name"]
    return bpy.data.node_groups.get(name) if name else None


def _write(tree, layer) -> bool:
    """Store the layer's text on the tree. False when Blender refuses the write right now."""
    text = layer.ExportToString()
    try:
        if tree.get(STATE_KEY) != text:
            tree[STATE_KEY] = text
    except AttributeError:
        # Blender forbids writing ID data while it draws. A change made from a draw
        # callback is stored on the next tick instead.
        return False
    return True


def _flush():
    tree, layer = _tree(), _state["layer"]
    if tree is not None and layer is not None:
        _write(tree, layer)
    return None


def _on_layers_changed(_notice, _sender) -> None:
    if _state["restoring"]:
        return
    tree, layer = _tree(), _state["layer"]
    if tree is None or layer is None:
        return
    if not _write(tree, layer) and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush, first_interval=0.0)


def restore(tree, stage) -> bool:
    """Put the root layer back to the copy an undo or redo step restored on `tree`.

    Args:
        tree: The network's node tree, as the undo step left it.
        stage: The network's stage.

    Returns:
        True if the layer changed.
    """
    if tree is None or stage is None:
        return False
    text = tree.get(STATE_KEY)
    if not text:
        return False
    layer = stage.GetRootLayer()
    if layer.ExportToString() == text:
        return False
    _state["restoring"] = True
    try:
        layer.ImportFromString(text)
    finally:
        _state["restoring"] = False
    return True
