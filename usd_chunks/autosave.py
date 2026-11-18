"""Quick saves: a copy of an edited network, written beside its file every few minutes.

A host (usdview) writes the network to `<stem>.autosave.usda` next to `<stem>.usda` while
it has changes the user has not saved, and offers it back when the network is opened again
after a crash. This module is the part with no UI toolkit in it:

- where the files go (`autosave_path`, `next_target`, `quick_save_paths`, `is_quick_save`),
- writing one (`write_quick_save`): the same layer Save Scene writes, with none of a save's
  side effects on the open stage,
- finding and restoring one (`pending_recovery`, `recover_into`) and deleting them
  (`discard`),
- telling an edit of the network from a cook (`ChangeTracker`), so a playing,
  time-dependent network never writes one,
- how often (`autosave_minutes`, from USD_CHUNKS_AUTOSAVE_MINUTES).

A quick save sits in the network's own folder, so its relative asset paths and sublayer
paths mean what the network's do, and it opens like any saved network.
"""
from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Iterable
from pathlib import Path

from pxr import Sdf, Tf, Usd

from usd_chunks import chunk_naming, chunk_registry, chunk_stage, chunk_undo, live

logger = logging.getLogger(__name__)

#: Minutes between quick saves when USD_CHUNKS_AUTOSAVE_MINUTES is unset.
DEFAULT_MINUTES = 2.0

#: What a cook writes on a chunk prim outside the chunk's own layer: the module hash it
#: cooked with (chunk_utils.stamp_module_hash) and the layer an Edit Target chunk points at
#: (chunk_stage.publish_edit_target), both into the prim's bookkeeping layer. Neither is an
#: edit of the network, so a change to only these is not a change.
COOK_PROPERTIES = (chunk_naming.MODULE_HASH_ATTR, chunk_naming.EDIT_TARGET_MARKER)

_SUFFIX = ".autosave"
_QUICK_SAVE_NAME = re.compile(r"^(?P<stem>.+)\.autosave(?:\.(?P<n>\d+))?\.usda$", re.IGNORECASE)
_warned_values: set[str] = set()


# -- paths ------------------------------------------------------------------------------


def autosave_path(network_path: str | os.PathLike) -> str:
    """The network's main quick save: shot.usda -> shot.autosave.usda, in the same folder."""
    path = Path(network_path)
    return str(path.with_name(f"{path.stem}{_SUFFIX}.usda"))


def _numbered_path(network_path: str | os.PathLike, n: int) -> str:
    """The network's quick save number n: shot.usda -> shot.autosave.<n>.usda."""
    path = Path(network_path)
    return str(path.with_name(f"{path.stem}{_SUFFIX}.{n}.usda"))


def is_quick_save(path: str | os.PathLike) -> bool:
    """Whether path names a quick save (`*.autosave.usda` or `*.autosave.<n>.usda`).

    A host opening one directly neither offers to recover it nor writes quick saves of it.
    """
    return bool(_QUICK_SAVE_NAME.match(os.path.basename(os.fspath(path))))


def _same_file_key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def next_target(network_path: str | os.PathLike, pinned: Iterable[str | os.PathLike] = ()) -> str:
    """The file the next quick save goes to.

    The main quick save, unless it is pinned (kept by the user, not to be written over);
    then the lowest-numbered `<stem>.autosave.<n>.usda` (n >= 1) that is not pinned.

    Args:
        network_path: The network's file.
        pinned: Quick-save files not to write over. Compared as files, so any spelling of
            the same path counts.
    """
    taken = {_same_file_key(p) for p in pinned}
    target = autosave_path(network_path)
    n = 0
    while _same_file_key(target) in taken:
        n += 1
        target = _numbered_path(network_path, n)
    return target


def _quick_saves_with_mtime(network_path: str | os.PathLike) -> list[tuple[float, str]]:
    """(mtime, path) of each quick save of the network on disk, newest first."""
    path = Path(network_path)
    folder = path.parent
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    stem = os.path.normcase(path.stem)
    found = []
    for name in names:
        match = _QUICK_SAVE_NAME.match(name)
        if match is None or os.path.normcase(match.group("stem")) != stem:
            continue
        full = str(folder / name)
        try:
            found.append((os.path.getmtime(full), full))
        except OSError:  # deleted since the listing
            continue
    found.sort(key=lambda item: (-item[0], item[1]))
    return found


def quick_save_paths(network_path: str | os.PathLike) -> list[str]:
    """The network's quick-save files on disk (main and numbered), newest first by mtime."""
    return [path for _, path in _quick_saves_with_mtime(network_path)]


def pending_recovery(network_path: str | os.PathLike) -> list[str]:
    """The quick saves written after the network file was last saved, newest first.

    What a host offers to recover when the network opens. A quick save older than the
    network file holds nothing the file does not. With the network file missing (deleted,
    or never saved after a quick save was written), every quick save is returned.
    """
    saves = _quick_saves_with_mtime(network_path)
    try:
        saved_at = os.path.getmtime(network_path)
    except OSError:
        return [path for _, path in saves]
    return [path for mtime, path in saves if mtime > saved_at]


def discard(network_path: str | os.PathLike) -> None:
    """Delete every quick save of the network. One that is already gone is skipped."""
    for path in quick_save_paths(network_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# -- writing and recovering ---------------------------------------------------------------


def write_quick_save(scene, target_path: str | os.PathLike, last_text: str | None = None) -> str | None:
    """Write the scene's network to target_path, as Save Scene would write it.

    The layer is chunk_stage.saved_layer's, the one export_chunks writes. Unlike a save,
    nothing on the stage changes: no nodes are laid out, no layer is reloaded or dirtied,
    and nothing goes into the open cache. The file is replaced in one step (a temporary
    file beside it, then a rename), so a crash mid-write leaves the previous quick save.

    Args:
        scene: The scene (chunk_scene.SceneSender) whose network to write.
        target_path: The file to write, normally next_target(network_path, pinned).
        last_text: The text this returned last time. When the network still serializes to
            exactly that, nothing is written.

    Returns:
        The text written, to pass as last_text next time; None when nothing was written.

    Raises:
        ValueError: The scene has no stage.
        OSError: The file could not be written. No temporary file is left behind; the
            caller decides how to report it.
    """
    if scene.get_stage() is None:
        raise ValueError("the scene has no stage to write a quick save of")
    text = _usda_text(chunk_stage.saved_layer(scene, target_path))
    if text == last_text:
        return None
    live.write_atomic(Path(target_path), text)
    return text


def _usda_text(layer: Sdf.Layer) -> str:
    """layer as the text of a .usda file.

    An anonymous layer is not always in the usda format: USD 25.05 makes it "sdf", whose
    header (`#sdf 1.4.32`) a file named .usda cannot be opened with. Such a layer is copied
    into a usda one first.
    """
    if layer.GetFileFormat().formatId == "usda":
        return layer.ExportToString()
    usda = Sdf.Layer.CreateAnonymous(".usda")
    usda.TransferContent(layer)
    return usda.ExportToString()


def _network_specs(layer: Sdf.Layer) -> list[Sdf.Path]:
    """The topmost specs in layer that Save Scene writes, each with its subtree.

    Every Chunk prim (a chunkgroup brings its chunks, backdrops and notes along), and what
    sits directly under /chunks beside them: backdrops and notes, and the untyped `over` a
    shot keeps on a chunkgroup composed in from a sublayered file.
    """
    root = Sdf.Path(chunk_naming.CHUNKS_ROOT)
    found = []
    stack = list(layer.rootPrims)
    while stack:
        spec = stack.pop()
        beside_chunks = spec.path.GetParentPath() == root
        if spec.typeName == chunk_naming.CHUNK_PRIM_TYPENAME or (
                beside_chunks and spec.typeName in ("", chunk_naming.BACKDROP_TYPENAME)):
            found.append(spec.path)
        else:
            stack.extend(spec.nameChildren)
    return sorted(found)


def recover_into(root_layer: Sdf.Layer, quick_save_path: str | os.PathLike) -> None:
    """Put a quick save's network into root_layer in place of the one it holds.

    The chunk, backdrop and note specs Save Scene writes are removed from root_layer and
    the quick save's are copied in, in one change block. Everything else in root_layer --
    prims no chunk made, its sublayers, its metadata -- is kept. The layer is left dirty
    and is not saved: the user saves it, or closes without, as after any edit.

    Args:
        root_layer: The network's root layer, normally before its stage is opened.
        quick_save_path: The quick save to recover.

    Raises:
        OSError: The quick save cannot be read.
    """
    saved = Sdf.Layer.OpenAsAnonymous(os.fspath(quick_save_path))
    if saved is None:
        raise OSError(f"cannot read the quick save {quick_save_path}")
    with Sdf.ChangeBlock():
        for path in reversed(_network_specs(root_layer)):
            parent = root_layer.GetPrimAtPath(path.GetParentPath()) or root_layer.pseudoRoot
            del parent.nameChildren[path.name]
        for path in _network_specs(saved):
            parent = path.GetParentPath()
            if parent != Sdf.Path.absoluteRootPath and not root_layer.GetPrimAtPath(parent):
                Sdf.CreatePrimInLayer(root_layer, parent)
            Sdf.CopySpec(saved, path, root_layer, path)
            chunk_stage._carry_ancestor_specs(saved, root_layer, path)


# -- settings -------------------------------------------------------------------------------


def autosave_minutes() -> float:
    """Minutes between quick saves, from USD_CHUNKS_AUTOSAVE_MINUTES; 0 means never.

    Unset or empty: DEFAULT_MINUTES. Zero or less: 0, quick saves off. Not a number: a
    warning (once per value) and DEFAULT_MINUTES.
    """
    raw = chunk_registry.USD_CHUNKS_AUTOSAVE_MINUTES()
    if raw is None or not str(raw).strip():
        return DEFAULT_MINUTES
    try:
        minutes = float(raw)
    except ValueError:
        minutes = math.nan
    if math.isnan(minutes):
        if raw not in _warned_values:
            _warned_values.add(raw)
            logger.warning("%s=%r is not a number of minutes; quick saves every %g minutes instead.",
                           chunk_registry.USD_CHUNKS_AUTOSAVE_MINUTES.key, raw, DEFAULT_MINUTES)
        return DEFAULT_MINUTES
    return max(minutes, 0.0)


# -- change tracking ------------------------------------------------------------------------


class ChangeTracker:
    """Whether a stage's network has changed since the last quick save or save.

    Counts a change to what Save Scene writes: each chunk prim's params, wires, position
    and other specs, and backdrops and notes, in any layer of the stage's local stack. It
    compares each touched prim's own spec with the last one seen (chunk_undo.ChunkSpecs,
    the same record undo keeps), so writing a value a prim already has is no change.

    A cook is never a change: its output goes into the chunk's own layer, which is not
    looked at, and what a cook writes on its chunk prim elsewhere (COOK_PROPERTIES) is left
    out of the comparison. A playing, time-dependent network stays unchanged.

    A reload is never a change either. Sdf sends LayerDidReloadContent before the stage
    reports the reload in its ObjectsChanged, so a reload of one of the stage's local
    layers clears `changed` and the ObjectsChanged that follows only re-reads every spec,
    once the stage has recomposed. That holds for `stage.Reload()` and `layer.Reload()`.

    Args:
        stage: The network's stage. Listening starts at once; `close()` stops it.
    """

    def __init__(self, stage: Usd.Stage):
        self.stage = stage
        #: True once the network has changed; the host clears it with mark_clean().
        self.changed = False
        self._specs = chunk_undo.ChunkSpecs(stage, ignored=COOK_PROPERTIES)
        self._reloaded = False
        self._listeners = [
            Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on_objects_changed, stage),
            Tf.Notice.RegisterGlobally(Sdf.Notice.LayerDidReloadContent, self._on_reload),
        ]

    def mark_clean(self) -> None:
        """The network was just saved or quick-saved: nothing has changed since."""
        self.changed = False

    def mark_changed(self) -> None:
        """Count a change the stage did not report, such as one made before tracking began."""
        self.changed = True

    def close(self) -> None:
        """Stop listening. Safe to call more than once."""
        for listener in self._listeners:
            listener.Revoke()
        self._listeners = []

    revoke = close

    def _on_reload(self, _notice, layer) -> None:
        if layer is None or not self._listeners or not self.stage.HasLocalLayer(layer):
            return
        self.changed = False
        self._reloaded = True

    def _on_objects_changed(self, notice, _sender) -> None:
        if self._reloaded:
            # The reload's own notice: what is in the layers now is the new baseline.
            self._reloaded = False
            self._specs.reset()
            return
        if self._specs.diff(self._specs.touched(notice)):
            self.changed = True
