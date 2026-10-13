"""Where a chunk sits: its prim path, and the layer its exec-chain position authors into.

Split out of chunk_stage so chunk_utils can resolve a chunk's edit layer without importing
chunk_stage, which imports chunk, which imports chunk_core, which imports chunk_utils. This
module sits below all of them: it needs pxr, the chunk vocabulary and one flag from
chunk_notice, and nothing that defines a chunk. chunk_stage re-exports every name here.
"""
from __future__ import annotations

import contextlib
import weakref
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from pxr import Sdf, Usd

from usd_chunks import chunk_naming, chunk_notice
from usd_chunks.chunk_flags import ChunkFlags

if TYPE_CHECKING:  # pragma: no cover -- typing only, no import at runtime
    from usd_chunks import chunk_core as core

_EXEC_INPUT = chunk_naming.EXEC_INPUT
_CHUNK_PRIM_TYPENAME = chunk_naming.CHUNK_PRIM_TYPENAME
_CHUNKS_ROOT = chunk_naming.CHUNKS_ROOT
_EDIT_TARGET_MARKER = chunk_naming.EDIT_TARGET_MARKER



def _resolve_chunk_root(chunk: core.ChunkBase) -> str:
    """Return the parent prim path for chunk based on its context's prim_path."""
    context = chunk.get_context()
    if context and context.prim_path:
        return context.prim_path
    return _CHUNKS_ROOT


def get_chunk_prim_path(chunk: core.ChunkBase) -> str:
    """Return the USD prim path string for a chunk."""
    if chunk.has_flag(ChunkFlags.GroupFlags.CONTEXT) and chunk.get_config() and chunk.get_config().module:
        return chunk.prim_path or f"{_resolve_chunk_root(chunk)}/{chunk.name}"
    return f"{_resolve_chunk_root(chunk)}/{chunk.name}"


def _prim_edit_target_layer(stage: Usd.Stage, prim: Usd.Prim) -> Sdf.Layer | None:
    """The layer named by prim's Edit Target marker, if it is in the stage's layer stack.

    Only a layer actually in the current stack is returned, so the result is always safe to
    author into. Otherwise None.
    """
    attr = prim.GetAttribute(_EDIT_TARGET_MARKER)
    if not (attr and attr.IsValid()):
        return None
    ident = attr.Get()
    if not ident:
        return None
    for layer in stage.GetLayerStack(includeSessionLayers=True):
        if layer.identifier == ident:
            return layer
    return None


def _iter_exec_upstream(stage: Usd.Stage, start: Sdf.Path):
    """Breadth-first walk of the exec chain (inputs:chunk connections) upstream from start.

    Yields each reachable (path, prim, unbranched) exactly once, start itself excluded. prim
    may be invalid (dangling connection) -- such nodes are yielded but not walked through.
    Shared by every "look upstream" consumer so the traversal semantics (dedup, ordering,
    dangling handling) live in one place.

    unbranched says everything visited up to and including this node forms a single chain
    start -> ... -> node, with no level holding more than one node. That is the condition
    under which "what does the walk from here find" is the same question as "what does the
    walk from start find", which is what lets nearest_upstream_edit_layer share one answer
    along the run instead of re-deriving it per node. It goes False for good at the first
    fork and never comes back.

    Deliberately exec-only (inputs:chunk), NOT the exec+data walk view_cook_list
    does: Edit Target resolution follows the execution chain that actually defines
    authoring position, and must ignore incidental data wiring -- a chunk reading another's
    points shouldn't drag its Edit Target upstream. The two are separate on purpose.
    """
    seen = {start}
    frontier = [start]
    unbranched = True
    while frontier:
        next_frontier: list = []
        for path in frontier:
            p = stage.GetPrimAtPath(path)
            if path != start:
                yield path, p, unbranched
            if not (p and p.IsValid()):
                continue
            exec_in = p.GetAttribute(_EXEC_INPUT)
            if exec_in and exec_in.IsValid():
                for conn in exec_in.GetConnections():
                    conn_prim = conn.GetPrimPath()
                    if conn_prim not in seen:
                        seen.add(conn_prim)
                        next_frontier.append(conn_prim)
        unbranched = unbranched and len(next_frontier) == 1
        frontier = next_frontier


_edit_layer_memo: dict[str, Sdf.Layer | object] = {}
# What the memo holds for "no Edit Target chunk upstream" (memo.get's None means "not asked").
_NO_UPSTREAM = object()
_edit_layer_memo_stage: weakref.ref | None = None
_edit_layer_memo_frozen = False


def invalidate_edit_layer_memo() -> None:
    """Drop the memoised nearest_upstream_edit_layer answers.

    Must be called by anything that can change what that function returns: the exec chain
    it walks (an inputs:chunk connection appearing, moving or being healed away), the Edit
    Target markers it looks for, or the stage's layer stack (a marker only counts while the
    layer it names is actually composed).

    Two kinds of write are exempt, both because they provably cannot move an answer: a
    frozen block (see freeze_edit_layer_memo) and ChunkLayerManager rearranging its own
    internal layers (see chunk_notice._internal_layer_write_active).
    """
    if _edit_layer_memo_frozen or chunk_notice._internal_layer_write_active:
        return
    _edit_layer_memo.clear()


_edit_layer_memo_muted: frozenset[str] = frozenset()


def edit_layer_memo_affected(
    notice: Usd.Notice.ObjectsChanged,
    chunk_prim_paths: Iterable[str] = (),
    has_chunk_under: Callable[[str], bool] | None = None,
) -> bool:
    """Whether this change round can move any nearest_upstream_edit_layer answer.

    That function is a walk of inputs:chunk connections between chunk prims, reading the
    Edit Target marker off each and checking the named layer is in the layer stack. So an
    answer moves only when one of those moves: an exec connection or a marker (a property
    by one of two names), a chunk prim appearing or going, or the layer stack itself
    (a sublayer edit, which USD reports as an info change on "/", or a mute, which it
    reports with no paths, or with only the driven prims the layer held -- so the muted
    layers are compared to what they were when the memo was built).

    Everything else is what a scene load mostly consists of: a chunk writing the prims it
    drives. On a four-node scene, 755 change rounds during a load touch no chunk prim at
    all, and each one would otherwise throw the memo away and cost every chunk a fresh walk
    of the chain above it.

    A chunk prim the sync has not met yet is caught by its own type once anything touches
    it, and by the "/" rule when it arrives by sublayer; an arrival by reference onto a
    non-Chunk prim outside every known root is the one route this does not see until the
    loader registers it.

    Args:
        notice: The stage's ObjectsChanged notice.
        chunk_prim_paths: What the sync knows to be chunk prims, so a resync of an ancestor
            of one counts (the prim under it may be gone).
        has_chunk_under: The same question asked as a lookup -- whether a known chunk prim
            lies below a path -- in place of scanning chunk_prim_paths for it, which cost
            the size of the scene on every notice.

    Returns:
        True if the memo must be dropped.
    """
    stage = notice.GetStage()
    if stage is None:
        return True
    if _edit_layer_memo_stage is None or _edit_layer_memo_stage() is not stage:
        return True
    resynced = notice.GetResyncedPaths()
    info = notice.GetChangedInfoOnlyPaths()
    if not resynced and not info:
        return True
    # A sublayer edit reports "/", caught below; what reaches here unannounced is a mute, so
    # the muted set is compared rather than the whole stack -- which costs a layer per chunk
    # on every notice, and which a chunk's own layer coming and going (internal, exempt from
    # dropping the memo) used to change, dropping it on the next notice anyway.
    if frozenset(stage.GetMutedLayers()) != _edit_layer_memo_muted:
        return True
    if has_chunk_under is None:
        known = list(chunk_prim_paths)

        def has_chunk_under(path_str: str) -> bool:
            prefix = path_str + "/"
            return any(known_path.startswith(prefix) for known_path in known)
    for path in (*resynced, *info):
        if path.IsPropertyPath():
            if path.name in (_EXEC_INPUT, _EDIT_TARGET_MARKER):
                return True
            continue
        if path == Sdf.Path.absoluteRootPath:
            return True
        prim = stage.GetPrimAtPath(path)
        if not (prim and prim.IsValid()):
            return True
        path_str = str(path)
        if prim.GetTypeName() == _CHUNK_PRIM_TYPENAME or path_str == _CHUNKS_ROOT:
            return True
        if has_chunk_under(path_str):
            return True
    return False


@contextlib.contextmanager
def freeze_edit_layer_memo():
    """Hold the memo across a block whose writes provably cannot move any answer.

    Only for authoring that rearranges *where* an already-composed opinion is stored --
    _migrate_chunk_bookkeeping's per-property strong -> weak consolidation, which by
    construction leaves both inputs:chunk and chunk:editTargetLayer composing exactly what
    they composed before. Those writes still resync prims, so without this every migration
    in a pass throws away the answers the pass just computed, and each of the next chunk's
    answers is re-walked from scratch up the whole chain above it.

    Never wrap a block that can author a new chunk prim, an exec connection or a layer
    stack change: those are the three things the memo is a cache of.

    Yields:
        None, with the memo frozen.
    """
    global _edit_layer_memo_frozen
    prev = _edit_layer_memo_frozen
    _edit_layer_memo_frozen = True
    try:
        yield
    finally:
        _edit_layer_memo_frozen = prev


def nearest_upstream_edit_layer(stage: Usd.Stage, prim: Usd.Prim) -> Sdf.Layer | None:
    """The layer a chunk at prim's position should author its driven content into.

    That is upstream_edit_target_layer, or the session layer when no Edit Target chunk is
    upstream. Driven content is cook output, so it never follows the stage's own edit
    target -- unlike a chunk's prim, which does (chunk_utils.get_bookkeeping_layer).

    Args:
        stage: The stage.
        prim: The chunk prim.

    Returns:
        The layer, or None without a stage.
    """
    if not stage:
        return None
    return upstream_edit_target_layer(stage, prim) or stage.GetSessionLayer()


def upstream_edit_target_layer(stage: Usd.Stage, prim: Usd.Prim) -> Sdf.Layer | None:
    """The layer published by the nearest Edit Target chunk upstream of prim, if any.

    Found by walking the exec chain (inputs:chunk) upstream from prim. None means no Edit
    Target chunk routes this position and the caller picks its own fallback. A pure
    function of graph topology, never of which chunk ran last. prim's own marker is
    deliberately skipped (an Edit Target chunk defines the target for everything
    *downstream* of it, not for its own bookkeeping -- that follows whatever is upstream
    of it).

    Memoised per prim path, because being a pure function of topology also makes it a walk
    of the entire exec chain above prim, and it is asked once per get_edit_context --
    several times per chunk. On a long chain that is quadratic, and unmemoised it is the
    single largest cost of opening a saved graph. The memo is dropped wholesale by
    invalidate_edit_layer_memo() rather than aged per entry: one chunk's answer depends on
    every chunk above it, so there is no cheaper unit to invalidate than "all of it".

    Args:
        stage: The stage.
        prim: The chunk prim.

    Returns:
        The layer, or None when nothing upstream publishes one (or without a stage).
    """
    if not (stage and prim and prim.IsValid()):
        return None

    # Answers are keyed by prim path, and prim paths repeat across stages -- /chunks/Dot in a
    # file just opened is the same string as /chunks/Dot in the stage it replaced, while the
    # layer it resolves to belongs to the old stage and is in no layer stack at all now.
    # Authoring against that layer loses the write; migrating a chunk into it strips the
    # chunk's prim from the stage it is actually on. The memo serves one stage at a time, so
    # a different stage starts a new one.
    global _edit_layer_memo_stage, _edit_layer_memo_muted
    memo = _edit_layer_memo
    current = _edit_layer_memo_stage() if _edit_layer_memo_stage is not None else None
    if current is not stage:
        if current is not None and current.GetRootLayer() == stage.GetRootLayer():
            # Another stage of the same layers while that one lives -- a cook stage
            # (skip_cook) asked during a cook. Answered without the memo, and without
            # dropping it: a drag step cooking one chunk threw the scene's answers away
            # twice, once for the cook stage and once on the way back.
            memo = {}
        else:
            _edit_layer_memo.clear()
            _edit_layer_memo_stage = weakref.ref(stage)
    # The muted layers every answer about to be memoised is computed against. Taken as the
    # memo starts filling (not on each notice), so a mute that happened while the memo was
    # empty is still seen by edit_layer_memo_affected as a difference from this, whichever
    # notice comes next. A sublayer edit needs no snapshot: USD reports it as a change on "/".
    if memo is _edit_layer_memo and not memo:
        _edit_layer_memo_muted = frozenset(stage.GetMutedLayers())

    key = str(prim.GetPath())
    if (memoised := memo.get(key)) is not None:
        return None if memoised is _NO_UPSTREAM else memoised

    result = None
    # Every node on an unbranched run above prim has this same answer, so the walk both
    # stops at the first one already memoised and leaves the answer on the rest. Without
    # that the memo only ever holds the node that was asked about, and a chain restored or
    # reconciled node by node re-walks the whole thing above each one in turn.
    shared: list[str] = []
    for path, p, unbranched in _iter_exec_upstream(stage, prim.GetPath()):
        if not (p and p.IsValid()):
            continue
        layer = _prim_edit_target_layer(stage, p)
        if layer is not None:
            result = layer
            break
        if not unbranched:
            continue
        upstream_key = str(path)
        if (upstream := memo.get(upstream_key)) is not None:
            if upstream is not _NO_UPSTREAM:
                result = upstream
            break
        shared.append(upstream_key)

    if result is None:
        # Nothing upstream inside this graph. A chunk inside a chunkgroup is still behind
        # whatever the group is behind: the first chunk in a group has no exec input of its
        # own, so the walk stopped at the group's edge and its content landed in the session
        # layer, not the Edit Target's layer the group sits after.
        parent = prim.GetParent()
        if parent and parent.GetTypeName() == _CHUNK_PRIM_TYPENAME:
            result = upstream_edit_target_layer(stage, parent)

    # None is memoised too, as a sentinel: "nothing upstream" is as costly to re-derive.
    stored = _NO_UPSTREAM if result is None else result
    memo[key] = stored
    for shared_key in shared:
        memo[shared_key] = stored
    return result
