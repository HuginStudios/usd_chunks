"""Ctrl+Z and Ctrl+Shift+Z for chunk networks.

Blender's undo restores the .blend -- node trees, every RNA value -- and knows nothing about
the stage. Undo used to only rebuild the tree from USD, so it never undid anything a chunk
network did: a param edit, a new node, a delete.

So the network rides along with Blender's undo. A chunk prim -- its params, its wires,
which view is active -- is authored in the stage's local layers: the root layer, the
session layer, a department sublayer the Edit Target picked, or as overs on the prims a
Reference chunk brought in. Each time the stage reports a change, the chunk prims it
touched, anywhere in those layers, are recorded as a step: each prim's own spec (no child
prims) in each layer, before and after. Steps form a tree, so an edit made after an undo starts a branch
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

from usd_chunks import chunk_naming, chunk_registry

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
    """The chunk prims of one stage's layers, recorded as a tree of steps.

    A chunk prim's specs can sit in any layer of the stage's local layer stack: the root
    layer, the session layer, a department sublayer, or an over on a prim a Reference chunk
    brought in. So a spec counts when the *stage* says its prim is a chunk -- an over has no
    type of its own. Each chunk's own layer is left out: it holds cooked output, and cooking
    makes it again.

    Args:
        stage: The network's stage.
    """

    def __init__(self, stage: Usd.Stage):
        self.stage = stage
        self.token = uuid.uuid4().hex[:8]  # a step id saved in an old .blend is not ours
        self.parents: dict[int, int | None] = {0: None}
        self.changes: dict[int, tuple] = {0: ()}
        self.current = 0
        self.restoring = False
        self._next = 1
        self._scratch = Sdf.Layer.CreateAnonymous("usd_chunks_undo.usda")
        #: chunk prim path -> {layer identifier -> text of its own spec in that layer}
        self.specs: dict[Sdf.Path, dict[str, str]] = {}
        self.layers: dict[str, Sdf.Layer] = {}
        self._refresh_layers()

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

    # -- layers -----------------------------------------------------------------------

    def _refresh_layers(self) -> None:
        """Follow the layer stack. A layer that came is read as it is; one that went is
        forgotten. Neither is a step: adding or removing a sublayer is not a chunk edit."""
        cooked = {layer.identifier for layer in
                  chunk_registry.ChunkLayerManager().chunk_layers.values() if layer is not None}
        now = {layer.identifier: layer
               for layer in self.stage.GetLayerStack(includeSessionLayers=True)
               if layer.identifier not in cooked}
        for gone in set(self.layers) - set(now):
            for by_layer in self.specs.values():
                by_layer.pop(gone, None)
        added = [lid for lid in now if lid not in self.layers]
        self.layers = now
        for lid in added:
            for path in self._chunk_paths_under(now[lid], Sdf.Path.absoluteRootPath):
                self.specs.setdefault(path, {})[lid] = self._snapshot(lid, path)

    def _is_chunk(self, path: Sdf.Path) -> bool:
        prim = self.stage.GetPrimAtPath(path)
        return bool(prim) and prim.GetTypeName() == chunk_naming.CHUNK_PRIM_TYPENAME

    def _chunk_paths_under(self, layer: Sdf.Layer, path: Sdf.Path):
        """Every chunk prim `layer` holds a spec for at or below `path`."""
        root = layer.GetPrimAtPath(path)
        stack = [root] if root is not None else []
        while stack:
            spec = stack.pop()
            if self._is_chunk(spec.path):
                yield spec.path
            stack.extend(spec.nameChildren)

    # -- specs ------------------------------------------------------------------------

    def _snapshot(self, lid: str, path: Sdf.Path) -> str | None:
        """The text of the prim's own spec at `path` in layer `lid`, or None if none."""
        layer = self.layers.get(lid)
        if layer is None or layer.GetPrimAtPath(path) is None:
            return None
        scratch = self._scratch
        scratch.Clear()
        Sdf.CreatePrimInLayer(scratch, path)
        Sdf.CopySpec(layer, path, scratch, path, _copy_value, _own_children)
        return scratch.ExportToString()

    def _remember(self, lid: str, path: Sdf.Path, text: str | None) -> None:
        if text is not None:
            self.specs.setdefault(path, {})[lid] = text
        elif path in self.specs:
            self.specs[path].pop(lid, None)
            if not self.specs[path]:
                del self.specs[path]

    def _put(self, lid: str, path: Sdf.Path, text: str | None) -> None:
        """Make the prim's own spec at `path` in layer `lid` what `text` says, keeping its
        child prims. A layer no longer in the stack is left alone."""
        layer = self.layers.get(lid)
        if layer is None:
            return
        if text is None:
            if layer.GetPrimAtPath(path) is not None:
                # PrimSpec has no RemoveNameChild in Python; a namespace edit removes it.
                edit = Sdf.BatchNamespaceEdit()
                edit.Add(path, Sdf.Path.emptyPath)
                layer.Apply(edit)
        else:
            scratch = self._scratch
            scratch.Clear()
            scratch.ImportFromString(text)
            Sdf.CreatePrimInLayer(layer, path)
            Sdf.CopySpec(scratch, path, layer, path, _copy_value, _own_children)
        self._remember(lid, path, text)

    def touched(self, notice: Usd.Notice.ObjectsChanged) -> set[tuple[str, Sdf.Path]]:
        """The (layer, chunk prim) pairs a stage change may have touched -- anywhere in
        the layer stack, not only under CHUNKS_ROOT: a network can live under an asset's
        own prim."""
        resynced = [p.GetPrimPath() for p in notice.GetResyncedPaths()]
        if resynced:  # a sublayer coming or going resyncs what it holds, not always "/"
            self._refresh_layers()
        paths = {p for p in (q.GetPrimPath() for q in notice.GetChangedInfoOnlyPaths())
                 if p in self.specs or self._is_chunk(p)}
        for prim in resynced:
            paths.update(p for p in self.specs if p.HasPrefix(prim))
            for layer in self.layers.values():
                paths.update(self._chunk_paths_under(layer, prim))
        return {(lid, path) for path in paths for lid in self.layers}

    # -- recording --------------------------------------------------------------------

    def observe(self, keys) -> bool:
        """Record what changed at the (layer, path) `keys` since the last look.

        Returns:
            True if a step was recorded.
        """
        changes = []
        for lid, path in sorted(keys, key=lambda k: (k[1], k[0])):
            new = self._snapshot(lid, path)
            old = self.specs.get(path, {}).get(lid)
            if new == old:
                continue
            self._remember(lid, path, new)
            changes.append((lid, path, old, new))
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
        """Put the layers back to step `key`: undo up to where the two branches meet, then
        redo down to it.

        Returns:
            True if a layer changed.
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
            for lid, path, old, _new in self.changes[step]:
                writes[lid, path] = old  # an earlier step's undo overwrites a later one's
        for step in reversed(down[:down.index(meet)]):
            for lid, path, _old, new in self.changes[step]:
                writes[lid, path] = new
        self.restoring = True
        try:
            with Sdf.ChangeBlock():
                # Parents before children when making prims, children first when removing.
                made = sorted((k for k, t in writes.items() if t is not None),
                              key=lambda k: (k[1], k[0]))
                for lid, path in made:
                    self._put(lid, path, writes[lid, path])
                gone = sorted((k for k, t in writes.items() if t is None),
                              key=lambda k: (k[1], k[0]), reverse=True)
                for lid, path in gone:
                    self._put(lid, path, None)
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
    history = History(stage)
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
            or history.stage != stage:
        return False
    return history.move_to(tree.get(STEP_KEY))
