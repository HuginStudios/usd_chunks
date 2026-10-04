"""Skipping the cooks no upstream change can reach.

A change upstream dirties everything below it, and everything below it cooks -- a Scatter
of five thousand trees cooks again because the Light two chunks above it turned. Most of
those cooks read nothing that changed. This module decides which ones did, by knowing two
things about every chunk:

- What its last cook could read. Each cook runs on a *cook stage*: the scene's layers,
  populated only at the chunk's inputs (the prim paths in its prim path params) and at the
  prims its last cook wrote. Anything else is not on the stage the module sees, so
  whatever it read is inside that mask -- nobody declares it, and nobody can forget to.
  The layers of chunks not upstream of it are muted there too (hidden_layers), so a cook
  depends only on what solved before it -- the isolation cooking siblings at once needs.
  A cook that reads a sibling it is not wired to is caught and given every layer back.
  A hidden prim reads as absent rather than failing, so lookups on a cook stage are
  watched: a cook that asks for anything outside its mask, or walks the whole stage, is
  run again on the whole stage, and the chunk is cooked that way from then on.
- What each cook changed. The chunk's layer before the cook against after, spec by spec,
  down to the prims that differ. Those paths are handed to every chunk below it.

A chunk dirtied only by the chunks above it cooking skips its cook when none of the paths
handed to it since its last cook is at, above or below anything in its mask, and the scene
itself -- which layers are muted, the root and session layers' metadata -- is as it was
when it last cooked. Its layer still holds the result of those inputs.

Anything this cannot account for falls back to cooking: a chunk's first cook (it has no
mask yet), a chunk that reads outside its inputs, a rewire, a chunk disabled or enabled
above it, one with no cook of its own (a chunkgroup), one with an Edit Target file layer,
a cook read back from the cook cache. Each hands "everything changed" down.

The framework's own work during a cook -- muting a gate's branches, a chunk prim's
bookkeeping -- is on the scene, not the cook stage: ChunkCommon.get_scene_stage.

Off unless USD_CHUNKS_SKIP_COOKS=1, while chunks that cannot cook on their inputs alone are
found and made to.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from pxr import Sdf, Usd

from usd_chunks import chunk_scheduler
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_naming import CHUNKS_ROOT

logger = logging.getLogger(__name__)

#: The switch. See the module docstring.
ENV = "USD_CHUNKS_SKIP_COOKS"

#: A change handed down that covers every path: nothing below it may skip.
EVERYTHING = frozenset({Sdf.Path.absoluteRootPath})

#: How many times a cook that wrote outside its mask is retried with the mask widened.
_MAX_WIDENINGS = 3

_GLOB_CHARS = set("*?[")

_CHUNKS_ROOT = Sdf.Path(CHUNKS_ROOT)


def enabled() -> bool:
    """Whether cooks go through the cook stage and may be skipped. See ENV."""
    return os.environ.get(ENV, "").strip().lower() in ("1", "on", "true", "yes")


# ------------------------------------------------------------------------------------------
# The cook stage
# ------------------------------------------------------------------------------------------

# The cook stages of the cooks now running, innermost last; None for a cook on the scene's
# own stage. A cook stage lives exactly as long as its cook. Kept between cooks, it kept
# every layer of a closed scene alive and went on hearing their changes -- in the test
# suite, into listeners whose widgets were already gone, which crashed the process.
_active: list[Usd.Stage | None] = []


def active_stage() -> Usd.Stage | None:
    """The stage the innermost running cook reads and writes, or None outside a masked cook."""
    return _active[-1] if _active else None


@contextmanager
def unmasked() -> Iterator[None]:
    """A cook's own scope, on the scene's stage until run() narrows it.

    A cook can start inside another's -- a module setting a param that another chunk reads
    -- and must not inherit the outer chunk's mask.
    """
    _active.append(None)
    try:
        yield
    finally:
        _active.pop()


def is_cook_stage(stage: Usd.Stage | None) -> bool:
    """Whether stage is one of the cook stages -- the scene's layers, but not the scene's stage.

    Stage listeners that compare stages by root layer have to ask: a cook stage shares the
    scene's root layer, and is not it.
    """
    if stage is None:
        return False
    return any(stage == watched for watch in _watches for watched in watch.stages)


def _open_cook_stage(main: Usd.Stage, mask: set[Sdf.Path], hidden: Iterable[str] = ()) -> Usd.Stage:
    """A stage of main's layers populated at mask, muted as main is and at hidden too."""
    stage = Usd.Stage.OpenMasked(
        main.GetRootLayer(),
        main.GetSessionLayer(),
        main.GetPathResolverContext(),
        _population_mask(mask),
        Usd.Stage.LoadAll,
    )
    if muted := {*main.GetMutedLayers(), *hidden}:
        stage.MuteAndUnmuteLayers(sorted(muted), [])
    return stage


def _population_mask(paths: Iterable[Sdf.Path] | None) -> Usd.StagePopulationMask:
    if paths is None:
        return Usd.StagePopulationMask.All()
    mask = Usd.StagePopulationMask()
    for path in paths:
        mask.Add(path)
    return mask


class _Watch:
    """What one masked cook looked up, on the stages it reads.

    outside is what it asked for beyond its mask. points and children are everything it
    looked up -- a prim by path, and a prim's children -- for telling whether it read
    something a hidden layer (one not upstream of it) authors.
    """

    def __init__(self, stage: Usd.Stage, mask: set[Sdf.Path], hidden: Iterable[str] = ()):
        self.stages = [stage]
        self.mask = mask
        self.hidden = tuple(hidden)
        self.outside: set[Sdf.Path] = set()
        self.points: set[Sdf.Path] = set()
        self.children: set[Sdf.Path] = set()


# The watches of the masked cooks now running, innermost last.
_watches: list[_Watch] = []
_watching_installed = False


def _watch_for(stage) -> _Watch | None:
    if not _watches:
        return None
    watch = _watches[-1]
    return watch if any(stage == watched for watched in watch.stages) else None


def open_for_reading(root: Sdf.Layer) -> Usd.Stage:
    """A stage of root for a cook to read from -- masked and watched when the cook is.

    For a module that composes a stage of its own to read (chunk_stage.get_upstream_stage),
    so that what it reads there is held to its inputs like what it reads on the cook stage.
    """
    if not _watches:
        return Usd.Stage.Open(root)
    watch = _watches[-1]
    stage = Usd.Stage.OpenMasked(root, _population_mask(watch.mask))
    # Hidden here as on the cook stage: a sibling not upstream of the cook is no more
    # readable through a stage the module composes itself.
    if watch.hidden:
        stage.MuteAndUnmuteLayers(list(watch.hidden), [])
    watch.stages.append(stage)
    return stage


def _install_watching() -> None:
    """Make lookups on a cook stage report what they asked for outside its mask.

    A mask hides what a chunk does not read, and a hidden prim reads as absent -- no error,
    just a chunk that decides there is nothing there. Asking by path for something outside
    the mask, listing a prim's children the mask only partly populates, or walking the
    whole stage is what a cook does to read outside its inputs, and each is caught here.
    Installed once, on first use; a lookup on any other stage costs one comparison.
    """
    global _watching_installed
    if _watching_installed:
        return
    _watching_installed = True

    def by_path(original):
        def lookup(stage, path, *args, **kwargs):
            if _watches and (watch := _watch_for(stage)) is not None:
                prim = Sdf.Path(str(path)).GetPrimPath().StripAllVariantSelections()
                watch.points.add(prim)
                if not _covered(prim, watch.mask):
                    watch.outside.add(prim)
            return original(stage, path, *args, **kwargs)
        return lookup

    def whole(original):
        def lookup(stage, *args, **kwargs):
            if _watches and (watch := _watch_for(stage)) is not None:
                watch.outside.add(Sdf.Path.absoluteRootPath)
            return original(stage, *args, **kwargs)
        return lookup

    def children(original):
        def lookup(prim, *args, **kwargs):
            if _watches and (watch := _watch_for(prim.GetStage())) is not None:
                watch.children.add(prim.GetPath().StripAllVariantSelections())
                if not _covered(prim.GetPath(), watch.mask):
                    watch.outside.add(prim.GetPath())
            return original(prim, *args, **kwargs)
        return lookup

    for name in ("GetPrimAtPath", "GetObjectAtPath", "GetPropertyAtPath",
                 "GetAttributeAtPath", "GetRelationshipAtPath"):
        setattr(Usd.Stage, name, by_path(getattr(Usd.Stage, name)))
    for name in ("Traverse", "TraverseAll", "GetPseudoRoot"):
        setattr(Usd.Stage, name, whole(getattr(Usd.Stage, name)))
    for name in ("GetChildren", "GetAllChildren", "GetFilteredChildren", "GetChildrenNames",
                 "GetAllChildrenNames", "GetFilteredChildrenNames"):
        setattr(Usd.Prim, name, children(getattr(Usd.Prim, name)))


@contextmanager
def cook_stage(main: Usd.Stage, layer: Sdf.Layer, mask: set[Sdf.Path],
               hidden: Iterable[str] = ()) -> Iterator[_Watch]:
    """The stage one cook runs on: main's layers, populated only at mask, authoring into layer.

    Muted as main is, so a disabled chunk's layer is as absent here as there, and muted at
    hidden -- the layers of chunks not upstream of this one (hidden_layers). Yields the
    watch that collects what the cook looked up.
    """
    _install_watching()
    stage = _open_cook_stage(main, mask, hidden)
    watch = _Watch(stage, mask, hidden)
    _active.append(stage)
    _watches.append(watch)
    try:
        with Usd.EditContext(stage, layer):
            yield watch
    finally:
        _watches.pop()
        _active.pop()


# ------------------------------------------------------------------------------------------
# What a chunk reads and writes
# ------------------------------------------------------------------------------------------


def _iter_params(container) -> Iterator:
    for param in container.get_my_params():
        # A nested chunk's params are its own.
        if _is_chunk(param):
            continue
        yield param
        if hasattr(param, "get_my_params"):
            yield from _iter_params(param)


def _paths_in(value) -> Iterator[Sdf.Path]:
    """The prim paths a PRIM_SELECTION value names, widened to cover what it could match.

    A glob covers the prims under its fixed part, including ones created after this cook.
    Anything that is not a plain absolute path -- a collection, an expression still
    unresolved -- covers everything.
    """
    items = value if isinstance(value, (list, tuple)) else str(value or "").replace(",", " ").split()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not text.startswith("/"):
            yield Sdf.Path.absoluteRootPath
            continue
        parts = []
        for part in text.split("/")[1:]:
            if _GLOB_CHARS & set(part):
                break
            parts.append(part)
        path = Sdf.Path("/" + "/".join(parts)) if parts else Sdf.Path.absoluteRootPath
        if not path or not path.IsAbsolutePath():
            yield Sdf.Path.absoluteRootPath
            continue
        yield path.StripAllVariantSelections()


def input_paths(chunk) -> set[Sdf.Path]:
    """The prim paths chunk's prim path params name, as they are now.

    A prim path param is one flagged PRIM_SELECTION, or a primpath_location
    (ChunkParamBase._is_prim_path_param), or one flagged PRIM_PATHS -- prim paths the
    chunk reads that are not a selection, like a Render Product's vars.
    """
    paths: set[Sdf.Path] = set()
    for param in _iter_params(chunk):
        is_prim_path = getattr(param, "_is_prim_path_param", None)
        if (is_prim_path is not None and is_prim_path()) or param.has_flag(ChunkFlags.ParamFlags.PRIM_PATHS):
            paths.update(_paths_in(getattr(param, "value", None)))
    return paths


def output_paths(layer: Sdf.Layer) -> set[Sdf.Path]:
    """The prims a cook wrote to layer, leaving out the ones that are only someone's parent.

    A mask populates a path's ancestors anyway, and counting a bare /World a Define made on
    the way to /World/Grid would put all of /World in the chunk's mask.
    """
    paths: set[Sdf.Path] = set()

    def visit(path: Sdf.Path) -> None:
        if not path.IsPrimPath() and not path.IsPrimVariantSelectionPath():
            return
        spec = layer.GetPrimAtPath(path)
        if spec is None:
            return
        if spec.properties or not spec.nameChildren or spec.variantSets:
            paths.add(path.StripAllVariantSelections())

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    paths.discard(Sdf.Path.absoluteRootPath)
    return paths


def _chunk_of(node):
    """The chunk a DAG node's propagator is or belongs to; None for the scene and the like."""
    prop = node.propagator
    if prop is None or _is_chunk(prop):
        return prop
    # A param or group (ChunkCommon); the scene's own get_chunk looks a chunk up by name.
    return prop.get_chunk() if hasattr(prop, "get_scene_stage") else None


def upstream_chunks(chunk) -> list:
    """chunk and every chunk that solves before it: wired to it by exec or by a param.

    A param reached upstream brings its chunk along, and that chunk's own upstream: what the
    chunk authored depends on all of it.
    """
    found = {id(chunk): chunk}
    pending = [chunk]
    while pending:
        for node in chunk_scheduler.upstream_closure(pending.pop().get_dag_node()):
            owner = _chunk_of(node)
            if owner is not None and id(owner) not in found:
                found[id(owner)] = owner
                pending.append(owner)
    return list(found.values())


def hidden_layers(chunk, layer: Sdf.Layer, layer_of: Callable) -> dict[str, tuple]:
    """The layers a cook of chunk does not see: those of its siblings.

    A sibling is a chunk neither upstream nor downstream of chunk -- one that may be cooking
    at the same time, so a cook must not depend on it. What is downstream stays: it never
    cooks while chunk does, and an upstream chunk reading a prim a chunk below it also edits
    (a Mesh and the Transform after it) is how networks are built. Only a chunk's own
    anonymous layer is hidden -- a file layer an Edit Target hands to several chunks is the
    user's, and may hold what an upstream chunk wrote too.

    Args:
        chunk: The chunk about to cook.
        layer: Its layer, never hidden.
        layer_of: The layer of a chunk, or None (ChunkLayerManager.get_layer).

    Returns:
        {layer identifier: (chunk, layer)} for every hidden layer.
    """
    related = upstream_chunks(chunk)
    related.extend(c for node in chunk_scheduler.downstream_closure(chunk.get_dag_node())
                   if (c := _chunk_of(node)) is not None)
    kept = {layer.identifier}
    kept.update(own.identifier for c in related if (own := layer_of(c)) is not None)
    related_ids = {id(c) for c in related}
    hidden: dict[str, tuple] = {}
    for other in chunk.get_scene().get_chunks():
        if id(other) in related_ids:
            continue
        own = layer_of(other)
        if own is None or not own.anonymous or own.identifier in kept:
            continue
        hidden[own.identifier] = (other, own)
    return hidden


def read_hidden(watch: _Watch, inputs: Iterable[Sdf.Path], hidden: dict[str, tuple]) -> list:
    """The chunks of hidden whose layers author something the cook read.

    A prim looked up by path is read with its ancestors -- a transform inherits theirs --
    but not its children. A prim whose children were listed, and every input (a glob reaches
    whatever is below its fixed part, and a schema's Get bypasses the watch), is read with
    everything below it.
    """
    subtrees = {*watch.children, *inputs} - {Sdf.Path.absoluteRootPath}
    above: set[Sdf.Path] = set()
    for path in (*watch.points, *subtrees):
        if path != Sdf.Path.absoluteRootPath:
            above.update(path.GetAncestorsRange())
    readers = []
    for other, own in hidden.values():
        listed = any((spec := own.GetPrimAtPath(path)) is not None and spec.nameChildren
                     for path in watch.children)
        if listed or any(path in above or _covered(path, subtrees) for path in output_paths(own)):
            readers.append(other)
    return readers


def _outside(layer: Sdf.Layer, mask: set[Sdf.Path]) -> set[Sdf.Path]:
    """The prims layer holds that mask does not cover -- a cook that wrote somewhere new."""
    return {path for path in output_paths(layer) if not _covered(path, mask)}


def _covered(path: Sdf.Path, mask: set[Sdf.Path]) -> bool:
    """Whether path is at or below a masked one.

    Walks path's parents and looks each up in mask, rather than testing every masked path
    as a prefix: a cook that writes thousands of prims also has thousands in its mask, and
    checking each against each was 64 million prefix tests for a fracture of 8,000 specs.
    """
    while not path.isEmpty:
        if path in mask:
            return True
        if path == Sdf.Path.absoluteRootPath:
            return False
        path = path.GetParentPath()
    return False


# ------------------------------------------------------------------------------------------
# What a cook changed
# ------------------------------------------------------------------------------------------


def snapshot(layer: Sdf.Layer) -> Sdf.Layer:
    """A copy of layer as it is, to compare the cook's result against."""
    copy = Sdf.Layer.CreateAnonymous("skip_cook_before")
    copy.TransferContent(layer)
    return copy


def _spec_paths(layer: Sdf.Layer) -> set[Sdf.Path]:
    paths: set[Sdf.Path] = set()
    layer.Traverse(Sdf.Path.absoluteRootPath, paths.add)
    paths.discard(Sdf.Path.absoluteRootPath)
    return paths


#: Fields the Python bindings cannot hand back. Sublayer offsets go with the sublayer
#: paths, which are compared on their own.
_UNREADABLE_INFO = frozenset({"subLayerOffsets"})


def _info(spec) -> dict:
    return {key: spec.GetInfo(key) for key in spec.ListInfoKeys() if key not in _UNREADABLE_INFO}


def _same_spec(before: Sdf.Layer, after: Sdf.Layer, path: Sdf.Path) -> bool:
    old = before.GetObjectAtPath(path)
    new = after.GetObjectAtPath(path)
    if old is None or new is None:
        return old is None and new is None
    return _info(old) == _info(new)


def changed_paths(before: Sdf.Layer, after: Sdf.Layer) -> set[Sdf.Path]:
    """The prims whose specs differ between two versions of a layer.

    A property's change is its prim's. Child lists are not compared -- a prim gaining a
    child is the child's change, not its parent's, so a /World two chunks both write under
    does not change every time either of them cooks. The layer's own metadata -- its
    sublayers, a default prim -- is everyone's: a change there is EVERYTHING.
    """
    if not _same_spec(before, after, Sdf.Path.absoluteRootPath) or \
            list(before.subLayerPaths) != list(after.subLayerPaths):
        return set(EVERYTHING)
    changed: set[Sdf.Path] = set()
    for path in _spec_paths(before) | _spec_paths(after):
        if not _same_spec(before, after, path):
            prim = path.GetPrimPath() if not path.IsPrimPath() else path
            changed.add(prim.StripAllVariantSelections())
    changed.discard(Sdf.Path.absoluteRootPath)
    return changed


def scene_state(stage: Usd.Stage) -> tuple:
    """What a cook can change on the scene outside its own layer, for comparing after it.

    Which layers are muted -- a gate suppressing a branch -- and the root and session
    layers' own metadata -- a Stage Expression setting a variable. Either changes what
    every chunk below reads, and neither is in the cook's layer.
    """
    layers = (stage.GetRootLayer(), stage.GetSessionLayer())
    return (
        frozenset(stage.GetMutedLayers()),
        # Its order, too: an Edit Target repointed moves chunk layers under another layer.
        tuple(layer.identifier for layer in stage.GetLayerStack(includeSessionLayers=True)),
        tuple(
            (tuple(layer.subLayerPaths),
             tuple(sorted((key, repr(value)) for key, value in _info(layer.pseudoRoot).items())))
            for layer in layers if layer is not None
        ),
    )


# ------------------------------------------------------------------------------------------
# Handing changes down, and deciding
# ------------------------------------------------------------------------------------------


def _is_chunk(prop) -> bool:
    # ChunkBase alone defines can_cook; importing it here would be circular, chunk_core
    # being what calls this module.
    return callable(getattr(prop, "can_cook", None))


def hand_down(chunk, changed: Iterable[Sdf.Path]) -> None:
    """Tell every chunk below chunk that these paths changed since it last looked."""
    changed = frozenset(changed)
    if not changed:
        return
    for node in chunk_scheduler.downstream_closure(chunk.get_dag_node()):
        prop = node.propagator
        if prop is None or prop is chunk or not _is_chunk(prop):
            continue
        pending = getattr(prop, "_skip_pending", None)
        if pending is None:
            prop._skip_pending = set(changed)
        else:
            pending.update(changed)


def inputs_rewired(node) -> None:
    """A chunk's inputs changed: what it and everything below it saw is no longer known."""
    prop = node.propagator
    if prop is None or not _is_chunk(prop):
        return
    prop._skip_mask = None
    # Wired now, perhaps, to the sibling it read without being wired to.
    prop._skip_sees_all = False
    hand_down(prop, EVERYTHING)


def note_state(chunk) -> None:
    """Hand "everything" down when chunk was disabled or enabled since it was last solved.

    A disabled chunk's layer is muted: what it wrote leaves the stage without a cook, and
    comes back the same way.
    """
    disabled = chunk.has_state(ChunkFlags.State.DISABLED)
    if disabled != getattr(chunk, "_skip_was_disabled", False):
        chunk._skip_was_disabled = disabled
        hand_down(chunk, EVERYTHING)


def _touches(changed: set[Sdf.Path], mask: Iterable[Sdf.Path]) -> bool:
    """Whether a changed path is at, above or below a masked one."""
    if not changed:
        return False
    if Sdf.Path.absoluteRootPath in changed:
        return True
    for m in mask:
        if m == Sdf.Path.absoluteRootPath:
            return True
        # At or above: m or one of its ancestors changed.
        for ancestor in m.GetAncestorsRange():
            if ancestor in changed:
                return True
    # Below: a changed path under a masked one.
    return any(_covered(c, set(mask)) for c in changed)


def should_skip(chunk, reasons: list) -> bool:
    """Whether chunk's cook can be skipped: only chunks above it changed, and not what it reads.

    Args:
        chunk: The chunk chunk_cook.flush is about to cook.
        reasons: The (alias, propagator) pairs that dirtied it since it last cooked, from
            DagNode.iter_changes, gathered by chunk_cook.dispatch.
    """
    if not enabled() or not reasons:
        return False
    mask = getattr(chunk, "_skip_mask", None)
    if mask is None:
        return False
    for _alias, source in reasons:
        # Only a chunk above it: a change of its own params, or of a param another chunk
        # drives, is an input it reads by definition.
        if source is chunk or not _is_chunk(source):
            return False
    # The scene as it was when it last cooked. Not only a cook changes it: a param callback
    # writes a Stage Expression's variables the moment a row is typed, before any cook, and
    # a user can mute a layer. No cook above it would say so.
    stage = chunk.get_scene_stage()
    if stage is None or getattr(chunk, "_skip_scene", None) != scene_state(stage):
        return False
    # Where it writes, as well as what it reads: an Edit Target above it repointed hands it
    # another layer to author into, and its result has to move there.
    layer = chunk.get_edit_context()
    if layer is None or layer.identifier != getattr(chunk, "_skip_layer", None):
        return False
    pending = getattr(chunk, "_skip_pending", None) or set()
    if _touches(pending, mask):
        return False
    chunk._skip_pending = set()
    logger.debug("skipped cooking %s: nothing it reads changed", chunk.name)
    return True


# ------------------------------------------------------------------------------------------
# Running a cook
# ------------------------------------------------------------------------------------------


def run(chunk, main: Usd.Stage, layer: Sdf.Layer, execute, layer_of: Callable | None = None) -> None:
    """Run execute on chunk's cook stage, record what it read, hand down what it changed.

    The mask is the chunk's inputs and what its last cook wrote. A cook that writes
    somewhere new (a renamed prim path) fails to define it on the masked stage -- the spec
    lands in the layer all the same -- and is run again with that path in the mask. A
    masked cook that looked something up outside its mask, or failed for any other reason,
    is run again on the whole stage, and the chunk is cooked that way from then on: it
    reads something its inputs do not name.

    Given layer_of, a masked cook also sees only the layers of the chunks upstream of it
    (hidden_layers). One that reads what a sibling it is not wired to authors is run again
    with every chunk's layer, and cooked that way until it is rewired.

    A first cook, with nothing written yet to say where it writes, runs on the whole stage.
    """
    outputs = getattr(chunk, "_skip_outputs", None)
    unmaskable = getattr(chunk, "_skip_unmaskable", False)
    mask: set[Sdf.Path] | None = None
    if outputs is not None and not unmaskable:
        mask = input_paths(chunk) | set(outputs)
    hidden: dict[str, tuple] = {}
    if not _whole(mask) and layer_of is not None and not getattr(chunk, "_skip_sees_all", False):
        hidden = hidden_layers(chunk, layer, layer_of)

    chunk._skip_pending = set()
    state = scene_state(main)
    widenings = 0
    while True:
        failed = False
        watches: list[_Watch] = []
        try:
            _execute(chunk, main, layer, mask, execute, watches, hidden)
        except Exception:
            if _whole(mask):
                raise
            failed = True
        if _whole(mask):
            break
        if hidden and watches and (siblings := read_hidden(watches[0], input_paths(chunk), hidden)):
            layer.Clear()
            logger.info(
                "%s reads what %s author%s without being wired to %s; cooking it on every "
                "chunk's layers until it is", chunk.name,
                ", ".join(sorted(s.name for s in siblings)), "s" if len(siblings) == 1 else "",
                "it" if len(siblings) == 1 else "them",
            )
            chunk._skip_sees_all = True
            hidden = {}
            continue
        looked_up = watches[0].outside if watches else set()
        # Written outside the mask -- raised, or caught by a module that handles its own
        # errors. Run again where it can see what it wrote.
        wider = _outside(layer, mask)
        read_outside = {path for path in looked_up if not _covered(path, mask | wider)}
        if not failed and not wider and not read_outside:
            break
        layer.Clear()
        if wider and widenings < _MAX_WIDENINGS:
            mask |= wider
            widenings += 1
            continue
        logger.info(
            "%s cannot cook on a stage of its inputs alone%s; cooking it on the whole stage "
            "from now on", chunk.name,
            f" (it looked up {', '.join(sorted(map(str, read_outside)))})" if read_outside else "",
        )
        chunk._skip_unmaskable = True
        mask = None

    chunk._skip_outputs = frozenset(output_paths(layer))
    chunk._skip_mask = frozenset(mask) if mask is not None else None
    chunk._skip_scene = scene_state(main)
    chunk._skip_layer = layer.identifier
    # What changed is handed down by finish(), once the cook is wholly done.
    chunk._skip_everything = chunk._skip_scene != state


def baseline(chunk, layer: Sdf.Layer) -> Sdf.Layer:
    """What the chunks below chunk last saw of its layer, to tell them what a cook changed.

    Not the layer as the cook starts: a param bound to a prim attribute writes into the
    layer the moment it changes, before any cook (a Light's intensity), and against the
    layer at the start of the cook that change was never a change at all.
    """
    seen = getattr(chunk, "_skip_seen", None)
    return seen if seen is not None else snapshot(layer)


def finish(chunk, layer: Sdf.Layer, before: Sdf.Layer | None) -> None:
    """Hand down what chunk's layer changed since the chunks below last saw it.

    Called once the cook is wholly done -- its driven attributes re-authored too -- and
    before chunk_cook.flush decides on the chunks below it.
    """
    everything = before is None or getattr(chunk, "_skip_everything", True)
    chunk._skip_everything = True
    hand_down(chunk, EVERYTHING if everything else changed_paths(before, layer))
    chunk._skip_seen = snapshot(layer)


def _whole(mask: set[Sdf.Path] | None) -> bool:
    return mask is None or Sdf.Path.absoluteRootPath in mask


def _execute(chunk, main: Usd.Stage, layer: Sdf.Layer, mask: set[Sdf.Path] | None, execute,
             watches: list[_Watch], hidden: dict[str, tuple]) -> None:
    """Run execute on a stage of mask, or on main itself when mask covers everything.

    The whole stage is main: composing all of it a second time on the cook stage would cost
    the scene's composition, every cook, for no mask. The chunk prims under /chunks are
    framework bookkeeping, read by the helpers a module calls (exec_dependencies, variant
    contexts); every mask carries them, and no cook's output is ever under them.

    The cook's watch, which collects what it looked up, is appended to watches before it
    runs, so it is there also when the cook raises.
    """
    if _whole(mask):
        execute(chunk)  # on main: the cook's unmasked() scope is the innermost
        return
    with cook_stage(main, layer, mask | {_CHUNKS_ROOT}, hidden) as watch:
        watches.append(watch)
        try:
            execute(chunk)
        finally:
            # The watch outlives the cook; its stages must not. A cook stage still open
            # goes on hearing the layer's changes -- the Clear of a re-run among them.
            watch.stages.clear()


def restored(chunk, layer: Sdf.Layer) -> None:
    """A cook read back from the cook cache: forget what it read. finish() hands down.

    The cache vouches for the result, not for what the module would have read to make it,
    so a restored chunk is cooked, not skipped, the next time something above it changes.
    """
    chunk._skip_pending = set()
    chunk._skip_outputs = frozenset(output_paths(layer))
    chunk._skip_mask = None
    chunk._skip_everything = False


def unaccounted(chunk) -> None:
    """A cook this module did not run -- into a shared file layer, or with no layer at all.

    Nothing is known about what it read or changed.
    """
    chunk._skip_pending = set()
    chunk._skip_mask = None
    hand_down(chunk, EVERYTHING)
