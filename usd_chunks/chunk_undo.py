"""Undo and redo for what chunks author, on any host.

A chunk prim -- its params, its wires, which view is active -- is authored in the stage's
local layers: the root layer, the session layer, a department sublayer the Edit Target
picked, or as overs on the prims a Reference chunk brought in. `History` listens to the
stage and records, for each change, the chunk prims it touched: each prim's own spec (no
child prims) in each layer, before and after. Steps form a tree, so an edit made after an
undo starts a branch rather than throwing redo away. `History.move_to` walks from where the
stage is to any step, writing each prim's spec back, and the chunk system follows as it
follows any layer edit. Cooked output is not recorded: it lives in each chunk's own layer
and cooking makes it again.

A host with its own undo stack stores only `History.key()` with each of its steps and moves
there after an undo or redo (Blender: frontends/blender/addon/undo.py). A host without one
uses `UndoStack`, which groups by user input (usdview and any Qt app: frontends/qt/undo.py).

Scope (docs/undo.md): only chunk prims are undone. An edit to a prim that is not a chunk
-- scene data authored directly, another plugin or a script writing to the stage -- is not
recorded; a host that needs that undone uses the C++ usd-undo plugin or its own.

What a change costs is the prims it touched, not the layer: a scene authored beside the
chunks is never copied. The recorder has the shape of usd-undo's (record, move to a step)
so that one could stand in for it where a native build ships.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk_naming, chunk_registry

#: Steps kept. A slider drag records one step per tick, so this is many drags deep; older
#: steps are dropped and undo stops there.
MAX_STEPS = 5000


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
        on_record: Called with this history after each step is recorded -- where a host
            stores `key()` with its own undo step. Recording starts with `listen()`.
    """

    def __init__(self, stage: Usd.Stage, on_record: Callable[[History], None] | None = None):
        self.stage = stage
        self.on_record = on_record
        self.token = uuid.uuid4().hex[:8]  # a step id an earlier session saved is not ours
        self.parents: dict[int, int | None] = {0: None}
        self.changes: dict[int, tuple] = {0: ()}
        self.current = 0
        self.restoring = False
        self._next = 1
        #: Group by user input: a step starts at the first change after `note_input()`, and
        #: what arrives without one folds into the step before. Off where the host's own
        #: undo steps are the boundaries (Blender).
        self.grouping = False
        self._input = False
        self._listener = None
        self._scratch = Sdf.Layer.CreateAnonymous("usd_chunks_undo.usda")
        #: chunk prim path -> {layer identifier -> text of its own spec in that layer}
        self.specs: dict[Sdf.Path, dict[str, str]] = {}
        self.layers: dict[str, Sdf.Layer] = {}
        self._refresh_layers()

    # -- listening --------------------------------------------------------------------

    def listen(self) -> None:
        """Record every change the stage reports from now on."""
        if self._listener is None:
            self._listener = Tf.Notice.Register(
                Usd.Notice.ObjectsChanged, self._on_objects_changed, self.stage)

    def close(self) -> None:
        """Stop recording."""
        if self._listener is not None:
            self._listener.Revoke()
            self._listener = None

    def _on_objects_changed(self, notice, _sender) -> None:
        if self.observe(self.touched(notice)) and self.on_record is not None:
            self.on_record(self)

    # -- ids --------------------------------------------------------------------------

    def key(self, step: int | None = None) -> str:
        """The id a host stores for `step` (the current one by default)."""
        return f"{self.token}:{self.current if step is None else step}"

    def _step_of(self, key) -> int | None:
        """The step `key` names; None for one of ours that was pruned.

        A step with no id of this session -- none, or one an earlier session saved -- was
        pushed before recording began: Blender pushes a .blend's first undo step before
        the network is tracked. The network was then as it opened: step 0.
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
        # chunk_layers exists once the manager is initialized; a bare stage has none.
        owned = getattr(chunk_registry.ChunkLayerManager(), "chunk_layers", {})
        cooked = {layer.identifier for layer in owned.values() if layer is not None}
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
        if self.grouping and not self._input:
            # Nothing the user did since the last step: a drag's next tick, or a deferred
            # cook writing its outputs. It is part of the step before, not one of its own.
            self._merge(self.current, changes)
            return False
        self._input = False
        step = self._next
        self._next += 1
        self.parents[step] = self.current
        self.changes[step] = tuple(changes)
        self.current = step
        self._prune()
        return True

    def note_input(self) -> None:
        """The user pressed something: the next change starts a step (with `grouping`)."""
        self._input = True

    def _merge(self, step: int, changes: list) -> None:
        """Fold `changes` into `step`: each spec keeps its oldest before, takes the newest
        after."""
        merged = {(lid, path): (old, new) for lid, path, old, new in self.changes[step]}
        for lid, path, old, new in changes:
            first = merged[lid, path][0] if (lid, path) in merged else old
            merged[lid, path] = (first, new)
        self.changes[step] = tuple((lid, path, old, new)
                                   for (lid, path), (old, new) in merged.items())

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


class UndoStack:
    """Linear undo and redo, for a host with no undo stack of its own (usdview, the Qt app).

    A step is everything from one user input to the next: the host calls `note_input()` on
    each press (frontends/qt/undo.py does it for any Qt app), and a change that arrives
    without one -- a drag's next tick, a deferred cook writing its outputs -- folds into the
    step before. So a drag is one undo, and a cook is never an undo of its own.

    Args:
        stage: The stage to record; `retrack` follows a replaced one.
    """

    def __init__(self, stage: Usd.Stage | None):
        self.history: History | None = None
        self._redo: list[int] = []
        self.retrack(stage)

    def retrack(self, stage: Usd.Stage | None) -> None:
        """Record `stage` from now on, with an empty history."""
        self.close()
        if stage is not None:
            self.history = History(stage, on_record=self._on_record)
            self.history.grouping = True
            self.history.listen()

    def close(self) -> None:
        """Stop recording and forget the history."""
        if self.history is not None:
            self.history.close()
            self.history = None
        self._redo.clear()

    def note_input(self) -> None:
        """The user pressed something: the next change is a new undo step."""
        if self.history is not None:
            self.history.note_input()

    def _on_record(self, _history: History) -> None:
        self._redo.clear()  # a new edit after an undo: the old redo is gone, as anywhere

    def can_undo(self) -> bool:
        history = self.history
        return history is not None and history.parents.get(history.current) is not None

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        """Go back one step.

        Returns:
            True if anything was undone.
        """
        if not self.can_undo():
            return False
        history = self.history
        step = history.current
        if not history.move_to(history.key(history.parents[step])):
            return False
        self._redo.append(step)
        return True

    def redo(self) -> bool:
        """Go forward one step.

        Returns:
            True if anything was redone.
        """
        history = self.history
        while history is not None and self._redo:
            if history.move_to(history.key(self._redo.pop())):
                return True
        return False
