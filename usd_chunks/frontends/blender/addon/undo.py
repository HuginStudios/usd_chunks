"""Ctrl+Z and Ctrl+Shift+Z for chunk networks.

Blender's undo restores the .blend -- node trees, every RNA value -- and knows nothing about
the stage. Undo used to only rebuild the tree from USD, so it never undid anything a chunk
network did: a param edit, a new node, a delete.

So the network rides along with Blender's undo. Every chunk prim -- its params, its wires,
which view is active -- lives in the stage's root layer. Each time the stage reports a
change, the chunk prims it touched -- any Chunk prim in the layer, under CHUNKS_ROOT or an
asset's own prim -- are recorded as a step: each prim's own spec (no child
prims) before and after. Steps form a tree, so an edit made after an undo starts a branch
rather than throwing redo away. The node tree holds only the id of the step the network is
at (`STEP_KEY`), and Blender stores that id with each undo step like any other data on the
tree. After an undo or redo, `restore` walks the history from where the network is to the
step Blender put back, writing each prim's spec back, and the chunk system follows as it
follows any layer edit (the way usdview's usd-undo works). Cooked output is not recorded:
it lives in each chunk's own layer and cooking makes it again.

What a change costs is the prims it touched, not the layer: a scene authored into the root
layer beside the chunks is never copied. The recorder has the shape of usd-undo's (record,
move to a step) so that one could stand in for it where a native build ships.

An ID property, not a declared RNA one: writing an ID property tags no depsgraph update,
so it cannot set off the add-on's own depsgraph handler on every change.
"""
from __future__ import annotations

import logging
import uuid

import bpy
from pxr import Sdf, Tf, Usd

from usd_chunks import chunk_naming

logger = logging.getLogger(__name__)

#: The ID property on the node tree holding the id of the step the network is at. The
#: underscore hides it from Blender's Custom Properties panel.
STEP_KEY = "_usd_undo_step"

#: Steps kept. Blender keeps 32 undo steps by default and a slider drag records one step
#: per tick, so this is many drags deep; older steps are dropped and undo stops there.
MAX_STEPS = 5000

_state = {"history": None, "listener": None, "tree_name": None}


def _copy_value(*_args) -> bool:
    return True


def _own_children(field, *_args) -> bool:
    """A prim's own spec: its properties and variants, not its child prims -- a child chunk
    is a prim of its own, recorded and restored on its own."""
    return field != "primChildren"


class History:
    """The chunk prims of one layer, recorded as a tree of steps.

    Args:
        layer: The layer the chunk prims are authored in -- the stage's root layer.
    """

    def __init__(self, layer: Sdf.Layer):
        self.layer = layer
        self.token = uuid.uuid4().hex[:8]  # a step id saved in an old .blend is not ours
        self.parents: dict[int, int | None] = {0: None}
        self.changes: dict[int, tuple] = {0: ()}
        self.current = 0
        self.restoring = False
        self._next = 1
        self._scratch = Sdf.Layer.CreateAnonymous("usd_chunks_undo.usda")
        self.specs: dict[Sdf.Path, str] = {}
        for path in self._chunk_paths_under(Sdf.Path.absoluteRootPath):
            self.specs[path] = self._snapshot(path)

    # -- ids --------------------------------------------------------------------------

    def key(self, step: int | None = None) -> str:
        """The id stored on the tree for `step` (the current one by default)."""
        return f"{self.token}:{self.current if step is None else step}"

    def _step_of(self, key) -> int | None:
        """The step `key` names; None for one of ours that was pruned.

        A step with no id of this session -- none, or one saved in the .blend by an earlier
        one -- was pushed before recording began: opening a .blend pushes its first undo
        step before the network is tracked. The network was then as it opened: step 0.
        """
        token, _, step = str(key or "").partition(":")
        if token != self.token:
            return 0 if 0 in self.parents else None
        if not step.isdigit() or int(step) not in self.parents:
            return None
        return int(step)

    # -- specs ------------------------------------------------------------------------

    def _snapshot(self, path: Sdf.Path) -> str | None:
        """The text of the chunk prim's own spec at `path`, or None if there is none."""
        spec = self.layer.GetPrimAtPath(path)
        if spec is None or spec.typeName != chunk_naming.CHUNK_PRIM_TYPENAME:
            return None
        scratch = self._scratch
        scratch.Clear()
        Sdf.CreatePrimInLayer(scratch, path)
        Sdf.CopySpec(self.layer, path, scratch, path, _copy_value, _own_children)
        return scratch.ExportToString()

    def _put(self, path: Sdf.Path, text: str | None) -> None:
        """Make the prim's own spec at `path` what `text` says, keeping its child prims."""
        if text is None:
            spec = self.layer.GetPrimAtPath(path)
            if spec is not None:
                parent = self.layer.GetPrimAtPath(path.GetParentPath()) or self.layer.pseudoRoot
                parent.RemoveNameChild(spec)
            self.specs.pop(path, None)
            return
        scratch = self._scratch
        scratch.Clear()
        scratch.ImportFromString(text)
        Sdf.CreatePrimInLayer(self.layer, path)
        Sdf.CopySpec(scratch, path, self.layer, path, _copy_value, _own_children)
        self.specs[path] = text

    def _chunk_paths_under(self, path: Sdf.Path):
        """Every chunk prim the layer holds at or below `path`."""
        root = self.layer.GetPrimAtPath(path)
        stack = [root] if root is not None else []
        while stack:
            spec = stack.pop()
            if spec.typeName == chunk_naming.CHUNK_PRIM_TYPENAME:
                yield spec.path
            stack.extend(spec.nameChildren)

    def touched(self, notice: Usd.Notice.ObjectsChanged) -> set[Sdf.Path]:
        """The chunk prims a stage change may have touched -- anywhere in the layer, not
        only under CHUNKS_ROOT: a network can live under an asset's own prim."""
        paths = {p.GetPrimPath() for p in notice.GetChangedInfoOnlyPaths()}
        for resynced in notice.GetResyncedPaths():
            prim = resynced.GetPrimPath()
            paths.update(p for p in self.specs if p.HasPrefix(prim))
            paths.update(self._chunk_paths_under(prim))
        return paths

    # -- recording --------------------------------------------------------------------

    def observe(self, paths) -> bool:
        """Record what changed at `paths` since the last look.

        Returns:
            True if a step was recorded.
        """
        changes = []
        for path in sorted(paths):
            new = self._snapshot(path)
            old = self.specs.get(path)
            if new == old:
                continue
            if new is None:
                self.specs.pop(path, None)
            else:
                self.specs[path] = new
            changes.append((path, old, new))
        # The chunk system answering a restore (mirroring params back, re-syncing inputs)
        # belongs to the step being restored, not to a new one.
        if not changes or self.restoring:
            return False
        step = self._next
        self._next += 1
        self.parents[step] = self.current
        self.changes[step] = tuple(changes)
        self.current = step
        self._prune()
        return True

    def _prune(self) -> None:
        oldest = self._next - MAX_STEPS
        for step in [s for s in self.parents if s < oldest]:
            del self.parents[step], self.changes[step]
        for step, parent in self.parents.items():
            if parent is not None and parent not in self.parents:
                self.parents[step] = None

    # -- moving -----------------------------------------------------------------------

    def _ancestry(self, step: int) -> list[int]:
        chain = [step]
        while (parent := self.parents.get(chain[-1])) is not None:
            chain.append(parent)
        return chain

    def move_to(self, key) -> bool:
        """Put the layer back to step `key`: undo up to where the two branches meet, then
        redo down to it.

        Returns:
            True if the layer changed.
        """
        target = self._step_of(key)
        if target is None or target == self.current:
            return False
        up = self._ancestry(self.current)
        down = self._ancestry(target)
        shared = set(up).intersection(down)
        if not shared:  # pruned past where they meet
            return False
        meet = next(s for s in up if s in shared)
        writes = {}
        for step in up[:up.index(meet)]:
            for path, old, _new in self.changes[step]:
                writes[path] = old  # an earlier step's undo overwrites a later one's
        for step in reversed(down[:down.index(meet)]):
            for path, _old, new in self.changes[step]:
                writes[path] = new
        self.restoring = True
        try:
            with Sdf.ChangeBlock():
                # Parents before children when making prims, children first when removing.
                for path in sorted(p for p, t in writes.items() if t is not None):
                    self._put(path, writes[path])
                for path in sorted((p for p, t in writes.items() if t is None), reverse=True):
                    self._put(path, None)
        finally:
            self.restoring = False
        self.current = target
        return True


def track(tree, stage) -> None:
    """Record the network's chunk prims from now on, and put the step id on `tree`.

    Args:
        tree: The network's node tree.
        stage: The network's stage.
    """
    untrack()
    if tree is None or stage is None:
        return
    history = History(stage.GetRootLayer())
    _state["history"] = history
    _state["tree_name"] = tree.name
    _state["listener"] = Tf.Notice.Register(
        Usd.Notice.ObjectsChanged, _on_objects_changed, stage)
    _write(tree, history.key())


def untrack() -> None:
    """Stop recording. Called when another network replaces this one."""
    if _state["listener"] is not None:
        _state["listener"].Revoke()
    _state.update(history=None, listener=None, tree_name=None)


def current_history() -> History | None:
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


def _on_objects_changed(notice, _sender) -> None:
    history = _state["history"]
    if history is None or not history.observe(history.touched(notice)):
        return
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
            or history.layer != stage.GetRootLayer():
        return False
    return history.move_to(tree.get(STEP_KEY))
