"""Skipping the cooks no upstream change can reach.

A change upstream dirties everything below it, and everything below it cooks -- a Scatter
of five thousand trees cooks again because the Light two chunks above it turned. Most of
those cooks read nothing that changed. This module decides which ones did, by knowing two
things about every chunk:

- What its last cook could read. Each cook runs on a *cook stage*: the scene's layers,
  populated only at the chunk's inputs (the prim paths in its prim path params) and at the
  prims its last cook wrote. Anything else is not on the stage the module sees, so
  whatever it read is inside that mask -- nobody declares it, and nobody can forget to.
  The layers of chunks not upstream of it are muted there too (hidden_layers): neither
  what is below it nor another branch. A cook sees only what is wired above it, live and
  after reopening alike. One that reads another branch does not see it, and a warning
  says so.
  A hidden prim reads as absent rather than failing, so lookups on a cook stage are
  watched: a cook that asks for anything outside its mask, or walks the whole stage, is
  run again on the whole stage, and the chunk is cooked that way from then on.
- What each cook changed. The chunk's layer before the cook against after, spec by spec,
  down to the prims that differ. Those paths are handed to every chunk below it.

A chunk dirtied only by the chunks above it cooking skips its cook when none of the paths
handed to it since its last cook is at, above or below anything in its mask, and the scene
itself -- which layers are muted, the root and session layers' metadata -- is as it was
when it last cooked. Its layer still holds the result of those inputs.

A chunk's first cook runs on the whole stage, with nothing written yet to mask it by, and
records its inputs and what it wrote as its mask; its next cook is masked by the same. A
first cook that read what a chunk not upstream of it authors is run again masked, with
that chunk's layer hidden.

Anything this cannot account for falls back to cooking: a chunk that never cooked (it has
no mask yet), a chunk that reads outside its inputs, a rewire, a chunk disabled or enabled
above it, one with no cook of its own (a chunkgroup), one with an Edit Target file layer,
a cook read back from the cook cache. Each hands "everything changed" down.

The framework's own work during a cook -- muting a gate's branches, a chunk prim's
bookkeeping -- is on the scene, not the cook stage: ChunkCommon.get_scene_stage.

Always on. It was behind USD_CHUNKS_SKIP_COOKS=1 while chunks that cannot cook on their
inputs alone were found and made to.
"""

from __future__ import annotations

import logging
import re
import weakref
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pxr import Ar, Sdf, Usd

from usd_chunks import chunk_scheduler, layer_arcs
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_naming import CHUNKS_ROOT
from usd_chunks.cook_errors import CookBlocked, CookCancelled

logger = logging.getLogger(__name__)

#: A change handed down that covers every path: nothing below it may skip.
EVERYTHING = frozenset({Sdf.Path.absoluteRootPath})

#: How many times a cook that wrote outside its mask is retried with the mask widened.
_MAX_WIDENINGS = 3

_GLOB_CHARS = set("*?[")

_VARIANT_SELECTION = re.compile(r"\{[^{}]*\}")

_CHUNKS_ROOT = Sdf.Path(CHUNKS_ROOT)


@dataclass(eq=False)
class CookRecord:
    """What skip-cooking knows about one chunk: from its last cook, and handed down since.

    One per chunk, kept here (record) rather than as attributes on the chunk: a chunk
    carries none of the cook's bookkeeping, and all of it reads in one place.
    """

    #: What its last cook could read: its inputs, its outputs and the reads beyond them.
    #: None when that is not known -- never cooked, rewired, read back from the cook cache,
    #: cooked into a shared layer -- or when it cannot cook on its inputs alone. A first
    #: cook, on the whole stage, records its inputs and outputs.
    mask: frozenset[Sdf.Path] | None = None
    #: The prims its last cook wrote; None before its first.
    outputs: frozenset[Sdf.Path] | None = None
    #: What its last cook looked up beyond its inputs and outputs.
    reads: frozenset[Sdf.Path] = frozenset()
    #: The paths the chunks above it changed since it last cooked (hand_down).
    pending: set[Sdf.Path] = field(default_factory=set)
    #: scene_state as its last cook left it.
    scene: tuple | None = None
    #: The identifier of the layer its last cook authored into.
    layer: str | None = None
    #: Its layer as the chunks below it last saw it, to diff its next cook against.
    seen: Sdf.Layer | None = None
    #: Whether what its last cook changed is everything: it changed the scene outside its
    #: layer, or nothing is known of what it read.
    everything: bool = True
    #: It cannot cook on its inputs alone, and cooks on the whole stage from now on.
    unmaskable: bool = False
    #: The names of the siblings it read without being wired to them, already warned about.
    #: Emptied by a rewire.
    warned_siblings: frozenset[str] = frozenset()
    #: Whether it was disabled when it was last solved (note_state).
    was_disabled: bool = False
    #: (chunk_scheduler.shape_version(), ids of it and the chunks above it, of those below): _related.
    related: tuple | None = None
    #: (chunk_scheduler.shape_version(), ids of the chunks feeding what it feeds): _beside.
    beside: tuple | None = None
    #: (chunk_scheduler.shape_version(), weakrefs to it and the chunks wired to it): _chunk_prims.
    wired: tuple | None = None


# Weak, so a record goes with its chunk; chunks hash by an id no copy shares.
_records: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

# The chunks whose last cook found no prim at one of their inputs anywhere in the scene,
# with those inputs. A chunk can cook before the chunk that makes its prim -- one below it,
# or on another branch -- and find nothing; once the prim is made it cooks again
# (waiting_on), finds it hidden, and is blocked (run).
_waiting: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def record(chunk) -> CookRecord:
    """chunk's CookRecord, made empty the first time it is asked for."""
    rec = _records.get(chunk)
    if rec is None:
        rec = _records[chunk] = CookRecord()
    return rec


# ------------------------------------------------------------------------------------------
# The cook stage
# ------------------------------------------------------------------------------------------

# The cook stages of the cooks now running, innermost last; None for a cook on the scene's
# own stage. Only _kept outlives its cook, under the conditions given there.
_active: list[Usd.Stage | None] = []

# A cook stage being opened, before it has a watch: a cook stage to is_cook_stage, but what
# is looked up on it -- by listeners hearing it muted -- is no cook's read.
_opening: list[Usd.Stage] = []

# The one cook stage kept between the cooks of one flush: (weakref to the scene stage it is
# of, the cook stage). Opening a stage of the scene's layers and muting what is hidden costs
# a layer per chunk in the scene -- 36 ms a cook at 900 chunks -- so a flush opens it once
# and re-masks it per cook. Only for a cook that is not inside another's (that one opens its
# own), and only inside sharing(): a stage alive between edits hears every change the scene
# makes, including one a notice handler authors while USD is still delivering the change
# that set it off -- which reaches the kept stage first and is refused as a "usd threading
# violation". Kept past its scene, it once crashed the process by going on hearing a closed
# scene's layers into listeners whose widgets were gone.
_kept: tuple[weakref.ReferenceType, Usd.Stage] | None = None
_sharing = 0


@contextmanager
def sharing() -> Iterator[None]:
    """Let the masked cooks inside this block share one cook stage, dropped as it ends."""
    global _sharing
    _sharing += 1
    try:
        yield
    finally:
        _sharing -= 1
        if not _sharing:
            release_cook_stage()


def release_cook_stage() -> None:
    """Drop the shared cook stage, and with it its hold on the scene's layers."""
    global _kept
    _kept = None


def _kept_for(main: Usd.Stage) -> Usd.Stage | None:
    """The kept cook stage if it is of main, else None -- dropping one of another stage."""
    global _kept
    if _kept is None:
        return None
    ref, stage = _kept
    owner = ref()
    if owner is not None and owner == main:
        return stage
    _kept = None
    return None


def _remask(stage: Usd.Stage, main: Usd.Stage, mask: set[Sdf.Path], hidden: Iterable[str]) -> None:
    """Make the kept cook stage what _open_cook_stage would open for this cook.

    Muted first, while nothing is populated, and by difference: neighbouring chunks hide
    nearly the same layers. The difference is taken from the stage itself, never from a
    record of what the last cook asked for. Load rules back to everything, as a fresh stage
    has them, in case a cook loaded or unloaded something on it.
    """
    want = {*main.GetMutedLayers(), *hidden}
    have = set(stage.GetMutedLayers())
    if want != have:
        stage.MuteAndUnmuteLayers(sorted(want - have), sorted(have - want))
    if stage.GetLoadRules() != Usd.StageLoadRules.LoadAll():
        stage.SetLoadRules(Usd.StageLoadRules.LoadAll())
    if stage.GetInterpolationType() != Usd.InterpolationTypeLinear:
        stage.SetInterpolationType(Usd.InterpolationTypeLinear)
    stage.SetPopulationMask(_population_mask(mask))


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
    if any(stage == opening for opening in _opening):
        return True
    if _kept is not None and stage == _kept[1]:
        return True
    return any(stage == watched for watch in _watches if not watch.scene for watched in watch.stages)


def _open_cook_stage(main: Usd.Stage, mask: set[Sdf.Path], hidden: Iterable[str] = (),
                     opened: list | None = None, stand_in: _StandIn | None = None) -> Usd.Stage:
    """A stage of main's layers populated at mask, muted as main is and at hidden too.

    Appended to opened, when given, before it is muted: muting sends a change notice from
    it, and a listener that cannot yet tell it for a cook stage (is_cook_stage) takes it
    for the scene's -- the edit layer memo was dropped on every masked cook.

    Given stand_in, opened on its session layer instead, and muted only at what it could
    not leave out.
    """
    stage = Usd.Stage.OpenMasked(
        main.GetRootLayer(),
        stand_in.layer if stand_in is not None else main.GetSessionLayer(),
        main.GetPathResolverContext(),
        _population_mask(mask),
        Usd.Stage.LoadAll,
    )
    if opened is not None:
        opened.append(stage)
    muted = {*main.GetMutedLayers(), *hidden}
    if stand_in is not None:
        muted -= stand_in.dropped
    if muted:
        stage.MuteAndUnmuteLayers(sorted(muted), [])
    return stage


#: Layer metadata a stand-in has of its own: its sublayers are the point of it.
_SUBLAYER_INFO = frozenset({"subLayers", "subLayerOffsets"})


def _own_info(layer: Sdf.Layer) -> dict:
    return {key: value for key, value in _info(layer.pseudoRoot).items() if key not in _SUBLAYER_INFO}


def _has_content(layer: Sdf.Layer) -> bool:
    """Whether layer holds anything but its sublayers."""
    return bool(layer.rootPrims) or bool(_own_info(layer))


class _StandIn:
    """The session layer a cook stage opens with: the scene's, less the layers it hides.

    Taking a layer out of a stage costs as much as the layers it takes out: muting the 908
    a cook hid in a 913-chunk scene was most of opening its stage, and muting the one graph
    layer holding them all cost the same. A stage opened on only the layers it needs costs
    next to nothing, but the session layer holds the graph layer, and the graph layer every
    chunk's. So the stage opens on a stand-in: the session layer's own content, copied,
    sublayering what it does, less the hidden layers -- and any layer on the way to them
    that holds nothing of its own (the graph layer, a group's) stood in for the same way.
    One that holds something keeps its hidden layers, muted.

    A cook may author into the session layer (variant selections with no Edit Target above
    it: chunk_utils.get_edit_layer), so what it changed in the stand-in is copied back once
    it is done.
    """

    def __init__(self, session: Sdf.Layer, layer: Sdf.Layer, before: Sdf.Layer | None,
                 held: list[Sdf.Layer], dropped: set[str]):
        self.session = session
        self.layer = layer
        #: The stand-in as the cook found it, when the session layer had content to copy.
        self.before = before
        #: The stand-ins below it: nothing else holds an anonymous layer named only as a
        #: sublayer, so they live as long as this does.
        self.held = held
        #: The hidden layers it leaves out, so not muted.
        self.dropped = dropped

    def copy_back(self) -> None:
        """Copy what the cook authored into the stand-in to the scene's session layer.

        Each prim it changed is copied whole, its children with it, and each it removed is
        removed; its own metadata, but for the sublayers, is set where it changed.
        """
        after = self.layer
        if self.before is None and not _has_content(after):
            return
        before = self.before if self.before is not None else Sdf.Layer.CreateAnonymous("cook_session_before")
        session = self.session
        with Sdf.ChangeBlock():
            old_info, new_info = _own_info(before), _own_info(after)
            for key in old_info.keys() - new_info.keys():
                session.pseudoRoot.ClearInfo(key)
            for key, value in new_info.items():
                if old_info.get(key) != value:
                    session.pseudoRoot.SetInfo(key, value)
            changed = {path if path.IsPrimOrPrimVariantSelectionPath() else path.GetPrimOrPrimVariantSelectionPath()
                       for path in _spec_paths(before) | _spec_paths(after)
                       if not _same_spec(before, after, path)}
            copied: list[Sdf.Path] = []
            for path in sorted(changed):
                if any(path.HasPrefix(done) for done in copied):
                    continue
                if after.GetObjectAtPath(path) is not None:
                    Sdf.CreatePrimInLayer(session, path)
                    Sdf.CopySpec(after, path, session, path)
                    copied.append(path)
                elif path.IsPrimPath() and (spec := session.GetPrimAtPath(path)) is not None:
                    parent = session.GetPrimAtPath(path.GetParentPath())
                    if parent is not None:
                        parent.RemoveNameChild(spec)


def _stand_in(main: Usd.Stage, hidden: Iterable[str]) -> _StandIn | None:
    """A stand-in session layer for a cook stage that hides hidden, or None to mute instead.

    None when it would leave out no more layers than it keeps: past that, muting costs no
    more than reading the offsets of the layers kept (_stood_in).
    """
    removed = {*main.GetMutedLayers(), *hidden}
    session = main.GetSessionLayer()
    if not removed or session is None:
        return None
    held: list[Sdf.Layer] = []
    dropped: set[str] = set()
    kept = [0]
    subs = _stood_in(session, removed, dropped, held, kept)
    if subs is None or len(dropped) <= kept[0]:
        return None
    layer = Sdf.Layer.CreateAnonymous("cook_session")
    before = None
    if _has_content(session):
        layer.TransferContent(session)
    _set_sublayers(layer, subs)
    if _has_content(session):
        before = snapshot(layer)
    return _StandIn(session, layer, before, held, dropped)


def _stood_in(layer: Sdf.Layer, removed: set[str], dropped: set[str], held: list[Sdf.Layer],
              kept: list[int]) -> list[tuple[str, Sdf.LayerOffset]] | None:
    """layer's sublayers less removed, with a stand-in for each that holds removed ones.

    None when nothing below layer is removed, or a layer holding removed ones has content of
    its own. Adds what it leaves out to dropped, and what it keeps to kept[0], only then.
    """
    paths = list(layer.subLayerPaths)
    if not paths:
        return None
    # By index: the whole list of a 900-sublayer layer's offsets took 50 ms to read.
    offsets = layer.subLayerOffsets
    subs: list[tuple[str, Sdf.LayerOffset]] = []
    left_out: set[str] = set()
    for i, path in enumerate(paths):
        if path in removed:
            left_out.add(path)
            continue
        sub = Sdf.Layer.FindRelativeToLayer(layer, path)
        if sub is None:
            # Not open, so nothing hidden is under it: named so the stand-in finds it too.
            subs.append((Sdf.ComputeAssetPathRelativeToLayer(layer, path), offsets[i]))
            continue
        if sub.identifier in removed:
            left_out.add(sub.identifier)
            continue
        identifier = sub.identifier
        if sub.subLayerPaths and not _has_content(sub):
            inner = _stood_in(sub, removed, left_out, held, kept)
            if inner is not None:
                stand = Sdf.Layer.CreateAnonymous("cook_stand_in")
                _set_sublayers(stand, inner)
                held.append(stand)
                identifier = stand.identifier
        subs.append((identifier, offsets[i]))
    if not left_out:
        return None
    dropped |= left_out
    kept[0] += len(subs)
    return subs


def _set_sublayers(layer: Sdf.Layer, subs: list[tuple[str, Sdf.LayerOffset]]) -> None:
    layer.subLayerPaths = [identifier for identifier, _offset in subs]
    for i, (_identifier, offset) in enumerate(subs):
        if offset != Sdf.LayerOffset():
            layer.subLayerOffsets[i] = offset


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

    def __init__(self, stage: Usd.Stage, mask: set[Sdf.Path], hidden: Iterable[str] = (),
                 scene: bool = False):
        self.stages = [stage]
        self.mask = mask
        self.hidden = tuple(hidden)
        #: Watching a cook on the scene's own stage (a first cook), not on a cook stage.
        self.scene = scene
        self.outside: set[Sdf.Path] = set()
        self.points: set[Sdf.Path] = set()
        self.children: set[Sdf.Path] = set()
        #: The chunk's inputs as its cook left them, and those its cook stage has no prim at.
        self.inputs: set[Sdf.Path] | None = None
        self.absent: set[Sdf.Path] = set()
        #: The parents of the chunk prims in the mask (_chunk_prims), up to /chunks: there
        #: with them, so looking one up by path -- its group's prim -- reads nothing outside.
        #: Listing one's children still does.
        self.parents: set[Sdf.Path] = set()


# The watches of the masked cooks now running, innermost last.
_watches: list[_Watch] = []
_watching_installed = False


def _watch_for(stage) -> _Watch | None:
    if not _watches:
        return None
    watch = _watches[-1]
    return watch if any(stage == watched for watched in watch.stages) else None


def open_for_reading(root: Sdf.Layer, context: Ar.ResolverContext | None = None) -> Usd.Stage:
    """A stage of root for a cook to read from -- masked and watched when the cook is.

    For a module that composes a stage of its own to read (chunk_stage.get_upstream_stage),
    so that what it reads there is held to its inputs like what it reads on the cook stage.
    context is the scene stage's resolver context: root is anonymous, and opened without it
    a relative path was looked for from the working directory, not the network's folder.
    """
    context = context if context is not None else Ar.ResolverContext()
    if not _watches:
        return Usd.Stage.Open(root, context)
    watch = _watches[-1]
    if watch.scene:
        # A first cook sees the whole stage; what it reads here is still watched.
        stage = Usd.Stage.Open(root, context)
        watch.stages.append(stage)
        return stage
    stage = Usd.Stage.OpenMasked(root, context, _population_mask(watch.mask))
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
                if not _covered(prim, watch.mask) and prim not in watch.parents:
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

    Opened on a stand-in session layer that leaves the hidden layers out, when it leaves out
    more than it keeps (_StandIn): its own stage for the one cook, which costs less than
    re-masking the shared one. What the cook authored into the session layer is copied
    back as it ends.
    """
    global _kept
    _install_watching()
    stand_in = _stand_in(main, hidden)
    # A cook inside another's cannot share its stage: they want different masks at once.
    shared = _sharing > 0 and all(watch.scene for watch in _watches) and stand_in is None
    stage = _kept_for(main) if shared else None
    if stage is not None:
        _remask(stage, main, mask, hidden)
    else:
        try:
            stage = _open_cook_stage(main, mask, hidden, _opening, stand_in)
        finally:
            _opening.clear()
        if shared:
            _kept = (weakref.ref(main), stage)
    watch = _Watch(stage, mask, hidden)
    _active.append(stage)
    _watches.append(watch)
    try:
        # The cook stage's own arc, for a layer an asset brings in by one (layer_arcs).
        with Usd.EditContext(stage, layer_arcs.edit_target(stage, layer)):
            yield watch
    finally:
        _watches.pop()
        _active.pop()
        if stand_in is not None:
            stand_in.copy_back()
        if shared and _kept is not None and _kept[1] is stage:
            # Nothing populated while no cook runs on it, so it composes -- and reports --
            # next to nothing as the scene changes in the meantime.
            stage.SetPopulationMask(Usd.StagePopulationMask())


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
        # A selection names a variant, not a prim: /asset{color=red}/geo is /asset/geo. A
        # chunk writes it with a slash after the brace (split_variant_path), which Sdf.Path
        # does not parse, and an unparsed path covered everything.
        text = _VARIANT_SELECTION.sub("/", text)
        parts = []
        for part in filter(None, text.split("/")[1:]):
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
    chunk authored depends on all of it. So does what is wired into the chunkgroups a chunk
    sits in: a chunk inside a group reads what flows into the group, though no wire links
    it to the group's input.
    """
    found = {id(chunk): chunk}
    groups_walked: set[int] = set()

    def group_inputs(c) -> list:
        nodes = []
        for group in _groups_of(c):
            if id(group) in groups_walked:
                break
            groups_walked.add(id(group))
            nodes.extend(node for _key, node in group.get_dag_node().get_inputs())
        return nodes

    # One walk: a chunk found walks on from its own node, and what it shares with the
    # chunks found before it is not walked again. A walk per chunk found went over the same
    # params again and again, and was most of a first cook after opening a scene.
    def chunk_of(node):
        owner = _chunk_of(node)
        if owner is None or id(owner) in found:
            return ()
        found[id(owner)] = owner
        return (owner.get_dag_node(), *group_inputs(owner))

    chunk_scheduler.upstream_closure(chunk.get_dag_node(), also=chunk_of)
    # The seed's own groups: the walk found it already, before chunk_of could add them.
    for node in group_inputs(chunk):
        chunk_scheduler.upstream_closure(node, also=chunk_of)
    return list(found.values())


def _groups_of(chunk) -> Iterator:
    """The context chunks (chunkgroups, then the tab) that hold chunk, innermost first."""
    # Containers sit between a chunk and its group, and the scene's own at the top.
    owner = chunk.get_owner()
    while owner is not None:
        if _is_chunk(owner) and owner.has_flag(ChunkFlags.GroupFlags.CONTEXT):
            yield owner
        owner = owner.get_owner()


def _related(chunk) -> tuple[set[int], set[int]]:
    """The ids of chunk and the chunks above it, and of the chunks below it.

    Cached until the graph changes: only the graph decides it, and a drag changes values,
    not wiring.
    """
    rec = record(chunk)
    version = chunk_scheduler.shape_version()
    cached = rec.related
    if cached is not None and cached[0] == version:
        return cached[1], cached[2]
    # The groups holding it are above it too: a Reference hosting a network brings in what
    # the chunks inside it work on.
    above = {id(c) for c in upstream_chunks(chunk)} | {id(group) for group in _groups_of(chunk)}
    below: set[int] = set()
    groups: set[int] = set()
    own = {id(group) for group in _groups_of(chunk)}
    for node in chunk_scheduler.downstream_closure(chunk.get_dag_node()):
        if (c := _chunk_of(node)) is None:
            continue
        below.add(id(c))
        if id(c) not in own and c.get_context() is c:
            groups.add(id(c))
    # A group below it has what is inside it below it too, as upstream_chunks brings a
    # group's input along. Not the groups holding chunk: what else is in them is beside it.
    if groups:
        below.update(id(c) for c in chunk.get_scene().get_chunks()
                     if any(id(group) in groups for group in _groups_of(c)))
    below -= above
    rec.related = (version, above, below)
    return above, below


def _chunk_prims(chunk) -> set[Sdf.Path]:
    """The chunk prims a cook of chunk may read: its own, and those of the chunks wired to it.

    Framework bookkeeping, read by the helpers a module calls (exec_dependencies, variant
    contexts) for the chunks above it, and moved by an Edit Target's cook for the chunks
    below it. All of /chunks when a chunk among them has no prim yet. Every chunk prim was
    in every mask, and populating 900 of them was a third of opening a cook stage, for a
    cook that reads a handful.

    The chunks are walked once per shape of the graph; their paths are read every cook, as
    a rename moves a prim without changing the graph.
    """
    rec = record(chunk)
    version = chunk_scheduler.shape_version()
    cached = rec.wired
    if cached is None or cached[0] != version:
        wired = upstream_chunks(chunk)
        wired += [c for node in chunk_scheduler.downstream_closure(chunk.get_dag_node())
                  if (c := _chunk_of(node)) is not None]
        cached = rec.wired = (version, tuple(weakref.ref(c) for c in wired))
    paths = set()
    for ref in cached[1]:
        path = getattr(ref(), "_exec_prim_path", None)
        if not path:
            return {_CHUNKS_ROOT}
        paths.add(Sdf.Path(path))
    # A group's prim that holds others of them is populated as their parent: masked, it
    # would bring every chunk in the group along.
    parents = {parent for path in paths for parent in path.GetParentPath().GetAncestorsRange()}
    return paths - parents


def hidden_layers(chunk, layer: Sdf.Layer, layer_of: Callable,
                  place: Callable | None = None) -> dict[str, tuple]:
    """The layers a cook of chunk does not see: all but those of the chunks above it.

    A cook sees what is wired above it, and the branches of a Merge it feeds that come
    before its own: a Merge reads like a chain, its top input first, so a Transform in the
    bottom input sees the Mesh in the top one. Not what is below it -- a Transform wired
    above the Cube it names does not move the Cube -- and not a branch that comes after its
    own. Seeing either made the result depend on what had happened to cook already: live it
    saw them, opened from the file it did not. Only a chunk's own anonymous layer is hidden
    -- a file layer an Edit Target hands to several chunks is the user's, and may hold what
    an upstream chunk wrote too.

    Args:
        chunk: The chunk about to cook.
        layer: Its layer, never hidden.
        layer_of: The layer of a chunk, or None (ChunkLayerManager.get_layer).
        place: Where a chunk cooks among chunks no wire orders, lower first
            (chunk_stage.branch_place). Without it, no other branch is seen.

    Returns:
        {layer identifier: (chunk, layer)} for every hidden layer.
    """
    above, below = _related(chunk)
    beside = _beside(chunk) if place is not None else set()
    mine = place(chunk) if beside else 0
    chunks = chunk.get_scene().get_chunks()

    def seen(other) -> bool:
        if id(other) in above:
            return True
        return id(other) in beside and id(other) not in below and place(other) < mine

    kept = {layer.identifier}
    kept.update(own.identifier for c in chunks if seen(c) and (own := layer_of(c)) is not None)
    hidden: dict[str, tuple] = {}
    for other in chunks:
        if seen(other):
            continue
        own = layer_of(other)
        if own is None or not own.anonymous or own.identifier in kept:
            continue
        # A Sublayer chunk's layer is hidden like any other, with the files it brings in:
        # read_hidden and _made_out_of_sight look through them (_sublayer_stack).
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
        # Not what it writes under /chunks: an opinion on a chunk prim is a param (an
        # Override Chunk Prim's edit), and it reaches its chunk as a param value, not by the
        # cook reading the stage.
        written = (p for p in output_paths(own) if not p.HasPrefix(_CHUNKS_ROOT))
        if listed or any(path in above or _covered(path, subtrees) for path in written):
            readers.append(other)
        elif own.subLayerPaths and _brings_in(own, {*watch.points, *subtrees}):
            readers.append(other)
    return readers


def _sublayer_stack(layer: Sdf.Layer) -> list[Sdf.Layer]:
    """The layers layer brings in as sublayers, nested ones too, that are open."""
    stack: list[Sdf.Layer] = []
    todo = [layer]
    while todo:
        current = todo.pop()
        for path in current.subLayerPaths:
            sub = Sdf.Layer.FindRelativeToLayer(current, path)
            if sub is not None and sub not in stack:
                stack.append(sub)
                todo.append(sub)
    return stack


def _brings_in(layer: Sdf.Layer, paths: Iterable[Sdf.Path]) -> bool:
    """Whether a file layer sublayers has a prim at one of paths: a Sublayer chunk's prims."""
    paths = [path for path in paths if path != Sdf.Path.absoluteRootPath]
    return any(sub.GetPrimAtPath(path) is not None for sub in _sublayer_stack(layer) for path in paths)


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
        _layer_stack_ids(stage),
        tuple(
            (tuple(layer.subLayerPaths),
             tuple(sorted((key, repr(value)) for key, value in _info(layer.pseudoRoot).items())))
            for layer in layers if layer is not None
        ),
    )


#: (the stage, weakly; its Pcp used-layers revision; its layer stack's identifiers) as last
#: read by _layer_stack_ids.
_stack_memo: tuple[weakref.ref, int, tuple[str, ...]] | None = None


def _layer_stack_ids(stage: Usd.Stage) -> tuple[str, ...]:
    """The identifiers of stage's layer stack, session layers included, in order.

    Read again only when the stage's Pcp cache says its layers may have changed: a stack
    holds a layer per chunk, scene_state is read before and after every cook, and reading
    the whole stack each time made opening a network cost the square of its size. The
    revision moves on any sublayer insert, removal or reorder, nested ones included, and on
    a mute; not on authoring. Held weakly, so the memo never keeps a stage alive -- and a
    stage can only match while it is alive, so a freed one is never mistaken for another.
    """
    global _stack_memo
    pcp = getattr(stage, "_GetPcpCache", None)
    revision = pcp().GetUsedLayersRevision() if pcp is not None else None
    memo = _stack_memo
    if (revision is not None and memo is not None and memo[1] == revision
            and (seen := memo[0]()) is not None and seen == stage):
        return memo[2]
    ids = tuple(layer.identifier for layer in stage.GetLayerStack(includeSessionLayers=True))
    if revision is not None:
        _stack_memo = (weakref.ref(stage), revision, ids)
    return ids


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
        record(prop).pending.update(changed)


def passed_through(chunk, reasons: list) -> None:
    """Hand down what a chunk with no cook of its own (a chunkgroup) changed.

    Its own params are what the chunks inside it read, and a change of one changes what is
    below it without a cook to say what: everything. Reached only because a chunk above it
    changed, it changes nothing itself -- that chunk handed its change past it already. Nor
    does a param of a chunk nested in it, which that chunk's own cook answers for, or a
    change only the UI sees: a lofted menu's item list, mirrored as a scene finishes
    loading, left every chunk below the group to cook on the first edit after opening.

    Args:
        chunk: The chunk chunk_cook.flush reached, with no execute() to run.
        reasons: The (alias, propagator) pairs that dirtied it, as for should_skip.
    """
    if not reasons or any(_own_input(chunk, source) for _alias, source in reasons):
        hand_down(chunk, EVERYTHING)


def _own_input(chunk, source) -> bool:
    """Whether source, a propagator that dirtied chunk, is chunk itself or one of its params."""
    if source is chunk:
        return True
    if _is_chunk(source):
        return False
    if getattr(source, "is_presentation_change", lambda: False)():
        return False
    owner = source
    while owner is not None and not _is_chunk(owner):
        owner = owner.get_owner()
    return owner is chunk


def inputs_rewired(node) -> None:
    """A chunk's inputs changed: what it and everything below it saw is no longer known."""
    prop = node.propagator
    if prop is None or not _is_chunk(prop):
        return
    rec = record(prop)
    rec.mask = None
    # Wired now, perhaps, to the sibling it read without being wired to; or rewired wrong.
    rec.warned_siblings = frozenset()
    hand_down(prop, EVERYTHING)


def note_state(chunk) -> None:
    """Hand "everything" down when chunk was disabled or enabled since it was last solved.

    A disabled chunk's layer is muted: what it wrote leaves the stage without a cook, and
    comes back the same way.
    """
    rec = record(chunk)
    disabled = chunk.has_state(ChunkFlags.State.DISABLED)
    if disabled != rec.was_disabled:
        rec.was_disabled = disabled
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


def _scene_state_of(stage: Usd.Stage, states: list | None) -> tuple:
    """scene_state(stage), read from states when it holds stage's, and added to it when not."""
    if states is None:
        return scene_state(stage)
    for seen, state in states:
        if seen == stage:
            return state
    state = scene_state(stage)
    states.append((stage, state))
    return state


def should_skip(chunk, reasons: list, states: list | None = None) -> bool:
    """Whether chunk's cook can be skipped: only chunks above it changed, and not what it reads.

    Args:
        chunk: The chunk chunk_cook.flush is about to cook.
        reasons: The (alias, propagator) pairs that dirtied it since it last cooked, from
            DagNode.iter_changes, gathered by chunk_cook.dispatch.
        states: (stage, scene_state) pairs the caller keeps between decisions, emptied
            whenever something may have changed the scene -- a cook. One drag step decided
            on twenty chunks of the same scene, and read its state twenty times.
    """
    rec = record(chunk)
    if not reasons:
        return False
    mask = rec.mask
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
    if stage is None or rec.scene != _scene_state_of(stage, states):
        return False
    # Where it writes, as well as what it reads: an Edit Target above it repointed hands it
    # another layer to author into, and its result has to move there.
    layer = chunk.get_edit_context()
    if layer is None or layer.identifier != rec.layer:
        return False
    pending = rec.pending or set()
    if _touches(pending, mask):
        return False
    rec.pending = set()
    logger.debug("skipped cooking %s: nothing it reads changed", chunk.name)
    return True


# ------------------------------------------------------------------------------------------
# Running a cook
# ------------------------------------------------------------------------------------------


def run(chunk, main: Usd.Stage, layer: Sdf.Layer, execute, layer_of: Callable | None = None,
        place: Callable | None = None) -> None:
    """Run execute on chunk's cook stage, record what it read, hand down what it changed.

    The mask is the chunk's inputs and what its last cook wrote. A cook that writes
    somewhere new (a renamed prim path) fails to define it on the masked stage -- the spec
    lands in the layer all the same -- and is run again with that path in the mask. A
    masked cook that looked a prim up outside its mask -- a material by a path its params
    do not flag as one -- is run again with that prim in the mask too, and the prims it
    looked up beyond its inputs and outputs are kept in the mask for the next cook. One
    that walked the whole stage, kept widening, or failed for any other reason is run
    again on the whole stage, and the chunk is cooked that way from then on.

    Given layer_of, a masked cook also sees only the layers of the chunks upstream of it
    (hidden_layers). One that reads what another branch authors does not see it, and a
    warning says so (_warn_unwired).

    A first cook, with nothing written yet to say where it writes, runs on the whole stage,
    and records its inputs and outputs as its mask. One that read what a chunk not upstream
    of it authors is run again masked by them, with those layers hidden: on the whole stage
    it would see that chunk only if it happened to have cooked already.
    """
    rec = record(chunk)
    outputs = rec.outputs
    unmaskable = rec.unmaskable
    inputs: set[Sdf.Path] | None = None
    unwired: list = []
    first = outputs is None and not unmaskable and layer_of is not None
    mask: set[Sdf.Path] | None = None
    if outputs is not None and not unmaskable:
        mask = input_paths(chunk) | set(outputs) | set(rec.reads)
    hidden: dict[str, tuple] = {}
    if not _whole(mask) and layer_of is not None:
        hidden = hidden_layers(chunk, layer, layer_of, place)

    rec.pending = set()
    state = scene_state(main)
    widenings = 0
    while True:
        failed = False
        watches: list[_Watch] = []
        try:
            _execute(chunk, main, layer, mask, execute, watches, hidden)
        except CookCancelled:
            raise  # not a cook the mask broke: cancelled, it is not tried again
        except Exception:
            if _whole(mask):
                raise
            failed = True
        if _whole(mask):
            if first and watches:
                first = False
                inputs = input_paths(chunk)
                masked = inputs | output_paths(layer)
                hidden = {} if _whole(masked) else hidden_layers(chunk, layer, layer_of, place)
                if hidden and read_hidden(watches[0], inputs, hidden):
                    mask = masked
                    layer.Clear()
                    continue
            break
        # As the cook left them, once: resolving them is most of what a value change of a
        # param that is an expression costs, and both checks below read them.
        inputs = watches[0].inputs if watches and watches[0].inputs is not None else input_paths(chunk)
        if hidden and watches and (read := read_hidden(watches[0], inputs, hidden)):
            # Reading what a chunk below edits is how networks are built (a Mesh and the
            # Transform after it); reading another branch is a missing wire.
            below = _related(chunk)[1]
            unwired = [c for c in read if id(c) not in below]
        looked_up = watches[0].outside if watches else set()
        # Written outside the mask -- raised, or caught by a module that handles its own
        # errors. Run again where it can see what it wrote.
        wider = _outside(layer, mask)
        read_outside = {path for path in looked_up if not _covered(path, mask | wider)}
        if not failed and not wider and not read_outside:
            break
        layer.Clear()
        if (wider or read_outside) and Sdf.Path.absoluteRootPath not in read_outside                 and widenings < _MAX_WIDENINGS:
            mask |= wider | read_outside
            widenings += 1
            continue
        logger.info(
            "%s cannot cook on a stage of its inputs alone%s; cooking it on the whole stage "
            "from now on", chunk.name,
            f" (it looked up {', '.join(sorted(map(str, read_outside)))})" if read_outside else "",
        )
        rec.unmaskable = True
        mask = None

    rec.outputs = frozenset(output_paths(layer))
    if inputs is None:
        inputs = input_paths(chunk)
    _note_missing(chunk, main, inputs)
    if mask is not None:
        rec.mask = frozenset(mask)
    elif not rec.unmaskable:
        # A first cook, on the whole stage, with nothing written yet to mask it by: its
        # inputs and what it wrote. Its next cook is masked by the same and checks it; left
        # unknown, the first edit after opening cooked every chunk below the one edited.
        rec.mask = frozenset(inputs | rec.outputs)
    else:
        rec.mask = None
    # What it looked up beyond its inputs and outputs, from this cook alone: a read it no
    # longer makes -- its material param pointed elsewhere -- leaves the mask with it.
    rec.reads = frozenset()
    if not _whole(mask) and watches:
        # A chunk prim beyond its own and its upstream's is a read like any other, and kept.
        base = inputs | rec.outputs | _chunk_prims(chunk)
        watch = watches[0]
        rec.reads = frozenset(
            path for path in (watch.points - watch.parents) | watch.children if not _covered(path, base))
    rec.scene = scene_state(main)
    rec.layer = layer.identifier
    # What changed is handed down by finish(), once the cook is wholly done.
    rec.everything = rec.scene != state
    if rec.everything:
        _scene_changed_by(chunk, state, rec.scene)
    if hidden and not _whole(mask) and watches and (made := _made_out_of_sight(watches[0].absent, hidden)):
        raise CookBlocked(_blocked_message(chunk, made))
    if unwired:
        _warn_unwired(chunk, unwired)


def _warn_unwired(chunk, siblings: list) -> None:
    """Warn, once per set of siblings until rewired, that chunk read them without a wire.

    Args:
        chunk: The chunk that cooked.
        siblings: The chunks whose hidden layers author something its cook read.
    """
    rec = record(chunk)
    names = frozenset(s.name for s in siblings)
    if names <= rec.warned_siblings:
        return
    rec.warned_siblings |= names
    listed = ", ".join(sorted(names))
    one = len(names) == 1
    logger.warning(
        "%s reads prims that %s edit%s, but %s wired above it, so it doesn't see those edits.",
        chunk.name, listed, "s" if one else "", "isn't" if one else "aren't",
    )


def _scene_changed_by(chunk, before: tuple, after: tuple) -> None:
    """chunk's cook changed the root or session layer's metadata: no input of the chunks
    not below it.

    A chunk cooks on what solved above it, and the chunks below this one are handed
    "everything" (finish). The rest -- above it, or beside it -- recorded the scene as it
    was before, and would take this cook's change for one of their own: a Render Settings
    chunk at the end of a chain setting the root layer's renderSettingsPrimPath left every
    chunk above it to cook on the first edit after opening.

    Metadata alone. A cook that mutes layers -- a gate -- changes what the chunks beside it
    show without cooking them, and they must see that as a change.
    """
    if before[:2] != after[:2]:
        return
    # Held while their ids are compared, so no id is a dead propagator's, reused.
    below = [node.propagator for node in chunk_scheduler.downstream_closure(chunk.get_dag_node())]
    below_ids = {id(prop) for prop in below}
    for other, rec in list(_records.items()):
        if other is not chunk and id(other) not in below_ids and rec.scene == before:
            rec.scene = after


def _defined(stage: Usd.Stage, path: Sdf.Path) -> bool:
    prim = stage.GetPrimAtPath(path)
    return bool(prim) and prim.IsDefined()


def _made_out_of_sight(absent: Iterable[Sdf.Path], hidden: dict[str, tuple]) -> dict[Sdf.Path, list]:
    """The inputs a cook found no prim at that a hidden layer makes, each with who makes it.

    Made means the layer defines the path or a prim above it: a Cube's def, or a Reference
    chunk's prim with the asset under it.
    """
    made: dict[Sdf.Path, list] = {}
    for path in absent:
        # By who defines the deepest part of the path: a Cliff defines its rock, and a
        # scatter beside it may define /far_shore above it too.
        depth_of: dict[int, tuple] = {}
        for other, own in hidden.values():
            depth = _defined_depth([own, *_sublayer_stack(own)], path)
            if depth:
                depth_of[id(other)] = (depth, other)
        if depth_of:
            deepest = max(depth for depth, _ in depth_of.values())
            made[path] = [other for depth, other in depth_of.values() if depth == deepest]
    return made


def _defined_depth(layers: list[Sdf.Layer], path: Sdf.Path) -> int:
    """How deep the deepest prim at or above path that one of layers defines is; 0 for none."""
    while path != Sdf.Path.absoluteRootPath and not path.isEmpty:
        if any((spec := layer.GetPrimAtPath(path)) is not None and spec.specifier == Sdf.SpecifierDef
               for layer in layers):
            return path.pathElementCount
        path = path.GetParentPath()
    return 0


def _blocked_message(chunk, made: dict[Sdf.Path, list]) -> str:
    """What a blocked chunk's node says: the prim, who makes it, and what to wire."""
    path, makers = min(made.items(), key=lambda item: str(item[0]))
    names = ", ".join(sorted(c.name for c in makers))
    verb = "isn't" if len(makers) == 1 else "aren't"
    return f"{path} is made by {names}, which {verb} wired above {chunk.name}. Wire {names} above {chunk.name}."


def _note_missing(chunk, main: Usd.Stage, inputs: Iterable[Sdf.Path]) -> None:
    """Remember the inputs chunk's cook found no prim at anywhere in the scene, for waiting_on."""
    missing = frozenset(path for path in inputs
                        if path != Sdf.Path.absoluteRootPath and not _defined(main, path))
    if missing:
        _waiting[chunk] = missing
    else:
        _waiting.pop(chunk, None)


def waiting_on(chunk) -> list:
    """The chunks whose last cook found no prim at an input that is there now chunk has cooked.

    A chunk can cook before the chunk that makes its prim -- one below it, or on another
    branch -- and find nothing anywhere: opened from a file, a Transform wired above its
    Cube cooks first. Cooked again once the prim is there, it finds it hidden and is
    blocked, as it is live. Asked of the stage, not of what chunk wrote: a Sublayer or
    Reference chunk brings prims in without writing them into its own layer. A chunk is
    forgotten as it is returned, so it waits again only if its next cook misses too.

    Args:
        chunk: The chunk that has just cooked.

    Returns:
        The chunks to cook again.
    """
    if not _waiting:
        return []
    stage = chunk.get_scene_stage()
    if stage is None:
        return []
    woken = []
    for other, missing in list(_waiting.items()):
        if other is not chunk and any(_defined(stage, path) for path in missing):
            del _waiting[other]
            woken.append(other)
    return woken


def _beside(chunk) -> set[int]:
    """The ids of the chunks feeding what chunk feeds: the other branches of its Merges.

    Cached with _related, until the graph changes.
    """
    rec = record(chunk)
    version = chunk_scheduler.shape_version()
    if rec.beside is not None and rec.beside[0] == version:
        return rec.beside[1]
    above, below = _related(chunk)
    # Not through the groups holding it: a group is below every chunk in it, and what else
    # is in it is not a branch of the same Merge.
    own = {id(group) for group in _groups_of(chunk)}
    beside: set[int] = set()
    for node in chunk_scheduler.downstream_closure(chunk.get_dag_node()):
        if (c := _chunk_of(node)) is not None and c is not chunk and id(c) not in own:
            beside |= _related(c)[0]
    beside -= above | below
    rec.beside = (version, beside)
    return beside


def chunks_above(chunk) -> set[int]:
    """The ids of chunk and every chunk upstream of it, cached until the graph changes."""
    return _related(chunk)[0]


def baseline(chunk, layer: Sdf.Layer) -> Sdf.Layer:
    """What the chunks below chunk last saw of its layer, to tell them what a cook changed.

    Not the layer as the cook starts: a param bound to a prim attribute writes into the
    layer the moment it changes, before any cook (a Light's intensity), and against the
    layer at the start of the cook that change was never a change at all.
    """
    seen = record(chunk).seen
    return seen if seen is not None else snapshot(layer)


def finish(chunk, layer: Sdf.Layer, before: Sdf.Layer | None) -> None:
    """Hand down what chunk's layer changed since the chunks below last saw it.

    Called once the cook is wholly done -- its driven attributes re-authored too -- and
    before chunk_cook.flush decides on the chunks below it.
    """
    rec = record(chunk)
    everything = before is None or rec.everything
    rec.everything = True
    hand_down(chunk, EVERYTHING if everything else changed_paths(before, layer))
    rec.seen = snapshot(layer)


def _whole(mask: set[Sdf.Path] | None) -> bool:
    return mask is None or Sdf.Path.absoluteRootPath in mask


def _execute(chunk, main: Usd.Stage, layer: Sdf.Layer, mask: set[Sdf.Path] | None, execute,
             watches: list[_Watch], hidden: dict[str, tuple]) -> None:
    """Run execute on a stage of mask, or on main itself when mask covers everything.

    The whole stage is main: composing all of it a second time on the cook stage would cost
    the scene's composition, every cook, for no mask. The chunk prims under /chunks are
    framework bookkeeping, read by the helpers a module calls (exec_dependencies, variant
    contexts); every mask carries them, and no cook's output is ever under them. Only the
    chunk's own and its upstream's (_chunk_prims): a helper reading another is caught by the
    watch like any read outside the mask.

    The cook's watch, which collects what it looked up, is appended to watches before it
    runs, so it is there also when the cook raises.
    """
    if _whole(mask):
        # On main: the cook's unmasked() scope is the innermost. Watched all the same, so
        # run() can tell a first cook that read a sibling it is not wired to.
        watch = _Watch(main, {Sdf.Path.absoluteRootPath}, scene=True)
        watches.append(watch)
        _watches.append(watch)
        try:
            execute(chunk)
        finally:
            _watches.pop()
            watch.stages.clear()
        return
    prims = _chunk_prims(chunk)
    with cook_stage(main, layer, mask | prims, hidden) as watch:
        watch.parents = {parent for path in prims for parent in path.GetAncestorsRange()
                         if parent.HasPrefix(_CHUNKS_ROOT) and parent not in prims}
        watches.append(watch)
        try:
            execute(chunk)
            watch.inputs = input_paths(chunk)
            cooked_on = watch.stages[0]
            watch.absent = {path for path in watch.inputs
                            if path != Sdf.Path.absoluteRootPath and not _defined(cooked_on, path)}
        finally:
            # The watch outlives the cook; its stages must not. A cook stage still open
            # goes on hearing the layer's changes -- the Clear of a re-run among them.
            watch.stages.clear()


def restored(chunk, layer: Sdf.Layer) -> None:
    """A cook read back from the cook cache: forget what it read. finish() hands down.

    The cache vouches for the result, not for what the module would have read to make it,
    so a restored chunk is cooked, not skipped, the next time something above it changes.
    """
    rec = record(chunk)
    rec.pending = set()
    rec.outputs = frozenset(output_paths(layer))
    rec.mask = None
    rec.everything = False


def unaccounted(chunk) -> None:
    """A cook this module did not run -- into a shared file layer, or with no layer at all.

    Nothing is known about what it read or changed.
    """
    rec = record(chunk)
    rec.pending = set()
    rec.mask = None
    hand_down(chunk, EVERYTHING)
