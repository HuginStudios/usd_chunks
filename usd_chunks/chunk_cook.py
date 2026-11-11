"""Cooking: when a chunk runs its module's execute(node).

The model, in two steps:

1. The graph runs through and finds the dependent nodes to cook. A change calls
   chunk.execute() (ChunkNoticePropagator.execute), which dirties the chunk and solves
   everything downstream of it: notices and callbacks run there and then, and each chunk
   the solve reaches with a data change is handed to dispatch() here, which marks it stale
   and pending. Nothing cooks during the solve. batched_solve() holds the solves back and
   runs them once.
2. When the outermost solve (or batch) ends, flush() cooks every pending chunk, upstream
   first. A cook calls its chunk's execute. That is cook(): it checks the chunk may run
   now, then runs its module into the chunk's own layer.

This module decides *when* (dispatch, flush, batching) and *how* (cook). chunk_notice
imports this module, so it imports nothing above chunk_notice: the two things a cook needs
from above it -- where a chunk's layer is, and how to stamp the module hash -- are handed
in by ChunkBase._cook, which is only that.

Stale tracking (is_cook_stale) records a chunk whose cook was refused -- blocked outside
the active view, muted, no layer yet -- so chunk_stage.recook_for_view cooks it later.

A cook that raises (or is cancelled) marks its chunk FAILED, and nothing below it cooks
for the rest of that pass (cook_pass): the chunks below keep their last result, stale.
The other chunks of the pass still cook. A cook that runs clears FAILED.

A chunk whose cook waits on a long job holds every other cook until it ends (hold): the
host's event loop runs meanwhile, and a recook it starts waits instead (hold_back).

A script that only writes a network defers every cook (deferred): solves still mark what
they reach pending and stale, a read pulls just the chunks it needs (pull), and the rest
cooks once, when the block ends.

The graph itself (DagNode, notices) is chunk_notice's; the graph algorithms are
chunk_scheduler's. Chunk authors never call anything here: they write execute(node) and
call chunk.execute() to run a chunk again.
"""

from __future__ import annotations

import collections
import logging
import os
import weakref
from collections.abc import Callable
from contextlib import ExitStack, contextmanager

from pxr import Ar, Sdf, Tf, Usd

from usd_chunks import (
    chunk_notice,
    chunk_scheduler,
    cook_cache,
    cook_timings,
    layer_arcs,
    layer_reconcile,
    skip_cook,
)
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.chunk_naming import CHUNKS_ROOT
from usd_chunks.cook_errors import CookBlocked, CookCancelled

logger = logging.getLogger(__name__)

# The chunks the solves since the last flush() reached, waiting for it -- each with the
# (alias, propagator) changes that reached it and whether it was stale before they did.
_pending: dict = {}

# How many solves are running inside each other; the outermost one flushes. See solving().
_solve_depth = 0

# Whether flush() is running: a solve inside one of its cooks leaves its chunks to it.
_flushing = False

# Cooks that keep dirtying each other never settle; flush() stops after this many rounds.
_MAX_FLUSH_ROUNDS = 1000

# The propagators whose solve an enclosing batched_solve() is holding back, in the order they
# asked; None outside a batch.
_batched: collections.deque | None = None

# Follow-up work held until an enclosing batch has drained, by key; None outside a batch.
_batch_deferred: dict | None = None

# Work held until the outermost batch has ended and cooked, by key; None outside a batch.
# See after_batch().
_after_batch: dict | None = None

# Follow-up work held until the outermost flush has cooked, by key, outside a batch.
_solve_deferred: dict = {}

# What runs after every cook, once its write guard is down. See after_cook().
_after_cook_hooks: list[Callable] = []

# The chunks whose last cook was blocked (CookBlocked): nothing below them cooks until they
# cook again without it. Weak, so a deleted chunk blocks nothing.
_blocked: weakref.WeakSet = weakref.WeakSet()

# Orders the chunks of one flush round that sit at the same depth. See break_ties_with().
_tie_break: Callable | None = None

# How many holds are open (hold()): while any is, nothing else cooks.
_held = 0

# What asked to run while a hold was open, by key, to run once it has ended (hold_back).
_held_back: dict = {}

# How many deferrals are open (deferred()): while any is, solves only mark.
_deferring = 0

# The DagNodes a pull() is cooking for, inside a deferral; None when none is running.
_pull_only: dict | None = None

# How many cook passes are running inside each other (cook_pass), and the DagNodes below a
# chunk whose cook failed in the outermost one: those do not cook until it has ended.
_pass_depth = 0
_below_failed: dict = {}



def after_cook(fn: Callable) -> None:
    """Run fn(chunk) after every cook that ran a module, once the cook's write guard is down.

    For what sits above this module and has to hear what a cook changed, which the stage
    listeners skip while it runs: ChunkStageSync re-reading the chunk prims another chunk's
    cook wrote onto. Registering fn again runs it once.

    Args:
        fn: Called with the chunk that cooked.
    """
    if fn not in _after_cook_hooks:
        _after_cook_hooks.append(fn)


def break_ties_with(fn: Callable) -> None:
    """Cook the chunks of a flush round that sit at the same depth in the order of fn(chunk).

    For what sits above this module and knows what the DAG does not: a Merge's slots are on
    the stage (chunk_stage.branch_place). Without it they cook in the order they were reached.

    Args:
        fn: Called with a chunk; lower cooks first.
    """
    global _tie_break
    _tie_break = fn


def _cook_order(chunk) -> tuple:
    """flush()'s sort key: upstream first, then break_ties_with's order."""
    return (chunk_scheduler.get_depth(chunk.get_dag_node()), _tie_break(chunk) if _tie_break is not None else 0)


def queue(prop) -> bool:
    """Inside batched_solve(), hold prop's solve back until the block ends.

    Returns:
        True if it was queued; False outside a batch, where the caller solves now.
    """
    if _batched is None:
        return False
    _batched.append(prop)
    return True


def solve_batched() -> None:
    """Run the solves the enclosing batch has held back so far, cooking nothing.

    For a cook inside the block that must take them in: a chunk a script makes cooks once
    as it is made, and the solves its making queued -- each param it added, each value set
    -- marked it again when the block ended, after that cook, so every chunk cooked twice.
    """
    while _batched:
        prop = _batched.popleft()
        if prop.get_dag_node().dirty:
            prop._solve_downstream()


def defer_to_batch_end(key, fn) -> bool:
    """Run fn once when the enclosing batch has drained -- or the solve has cooked -- instead of now.

    For bookkeeping that only has to be right once the dust settles and costs something each
    time -- refreshing a chunk's gizmo anchors reads every param it has, and a Wedge change
    asked for it once per param written, hundreds of times. Deferred under the same key it
    runs once, with the batch's final values.

    Outside a batch, a solve settles too: one drag step of a Transform's gizmo changed nine
    values -- the param, its group, what its cook set -- and refreshed the anchors for each,
    a quarter of the step. Inside a solve or a flush, fn runs once the outermost flush has
    cooked everything pending.

    Args:
        key: Identifies the work, so asking twice runs it once.
        fn: What to run.

    Returns:
        False outside a batch, a solve and a flush -- the caller runs fn itself, straight away.
    """
    if _batch_deferred is not None:
        _batch_deferred[key] = fn
        return True
    if _solve_depth or _flushing:
        _solve_deferred[key] = fn
        return True
    return False


def is_batching() -> bool:
    """Whether a batched_solve (change_block) is open, so what it sets settles at its end."""
    return _batched is not None


def is_held() -> bool:
    """Whether a chunk is waiting on a long job, so nothing else may cook (hold)."""
    return bool(_held)


def after_batch(key, fn) -> bool:
    """Inside a batch, keep fn to run once the outermost one has ended and cooked.

    Later than defer_to_batch_end, which runs before the batch's final flush: for work that
    must see that flush's result -- ranking the chunk layers after an Edit Target that cooked
    in it re-homed the chunks below -- or that need only be done once for the whole block,
    such as looking for chunk prims that arrived by USD. Kept under the same key, it runs
    once, the last one asked for.

    Args:
        key: Identifies the work.
        fn: What to run.

    Returns:
        False outside a batch: the caller runs it now.
    """
    if _after_batch is None:
        return False
    _after_batch[key] = fn
    return True


def hold_back(key, fn) -> bool:
    """While a hold is open, keep fn to run once it has ended, instead of now.

    For a cook that would start while a chunk waits on its job -- a recook a timer fires, a
    view switching. Kept under the same key, it runs once, the last one asked for.

    Args:
        key: Identifies the work.
        fn: What to run.

    Returns:
        False when no hold is open: the caller runs it now.
    """
    if not (_held or _deferring):
        return False
    _held_back[key] = fn
    return True


@contextmanager
def hold(later: Callable[[Callable], None]):
    """Cook nothing else until the block ends: for a cook waiting on a long job.

    The cook waiting is one chunk's, inside a flush; the chunks below it must cook against
    its result, so they wait. The host's event loop runs while it waits, to paint and show
    progress, and what that loop would start is held: flush() cooks nothing, and a recook
    asks hold_back() to keep it. They run when the block has ended -- not straight away,
    as the cook it is in has not finished yet, but through later(fn), which runs fn once
    the event loop is back at the top.

    Args:
        later: Runs a function from the host's event loop once the current call stack has
            returned (a zero-delay timer).
    """
    global _held
    _held += 1
    try:
        yield
    finally:
        _held -= 1
        if not _held:
            later(_release)


def _release() -> None:
    """Run what hold_back() kept, and cook what the held solves left pending."""
    if _held:
        return  # held again before this ran; that hold's own release runs it
    flush()
    while _held_back:
        pending = list(_held_back.values())
        _held_back.clear()
        for fn in pending:
            fn()


def begin_deferring() -> None:
    """Cook nothing until the matching end_deferring(); see deferred()."""
    global _deferring
    _deferring += 1


def end_deferring(cook: bool = True) -> None:
    """End a begin_deferring(): the outermost one cooks what is pending, or drops it.

    Args:
        cook: False to drop what is pending instead -- for a scene about to be thrown away,
            whose results nobody will read. Its chunks stay stale.
    """
    global _deferring
    _deferring -= 1
    if _deferring:
        return
    if cook:
        _release()
    else:
        _pending.clear()
        _held_back.clear()


@contextmanager
def deferred():
    """Cook nothing until the block ends, then cook what it left pending, once.

    For a script writing a network: making a chunk, wiring it and setting its values each
    solve as usual -- notices and callbacks run, and what they reach is marked pending and
    stale -- but flush() cooks nothing, and a recook waits (hold_back). A read that needs a
    result pulls the chunks it depends on (pull); everything else cooks when the outermost
    block ends, as one flush. Not on a raise: what is pending is dropped, and stays stale.
    """
    begin_deferring()
    try:
        yield
    except BaseException:
        end_deferring(cook=False)
        raise
    end_deferring()


def is_deferring() -> bool:
    """Whether a deferred() block is open, so a change only marks what it reaches."""
    return bool(_deferring)


def pull(chunk) -> None:
    """Inside deferred(), cook what is pending upstream of chunk, chunk included -- and only that.

    For a read of a chunk's result in the middle of a block: the chunks it depends on cook
    now, upstream first, and the rest stay pending for the block's end. Outside a deferral
    nothing is left pending, so this cooks nothing.

    Args:
        chunk: The chunk whose result is about to be read.
    """
    global _pull_only
    if not _deferring:
        return
    solve_batched()
    prev, _pull_only = _pull_only, chunk_scheduler.upstream_closure(chunk.get_dag_node())
    try:
        flush(force=True)
    finally:
        _pull_only = prev


def cook_alone(chunk) -> None:
    """Inside deferred(), cook chunk now without what is above it, and leave it pending.

    For a chunk that builds params in its cook (Light's schema inputs), when only those
    params are wanted -- a save storing the values a script gave them -- and not its
    result: cooking what is above it would be the whole network. It cooks again, in
    place, with the rest.

    Args:
        chunk: The chunk.
    """
    changes, _ = _pending.pop(chunk, ([], True))
    try:
        chunk._cook()
    finally:
        mark_cook_stale(chunk)
        _pending[chunk] = (changes, True)


def pull_all() -> None:
    """Inside deferred(), cook everything pending now, recooks included, and keep deferring.

    For a read of the whole result -- an export, a commit -- in the middle of a block.
    """
    global _deferring
    if not _deferring:
        return
    solve_batched()
    was, _deferring = _deferring, 0
    try:
        _release()
    finally:
        _deferring = was


def _cooks_now(chunk) -> bool:
    """Whether flush() would cook a pending chunk now: not mid-cook, and not deferred."""
    if getattr(chunk, "_executing", False):
        return False
    return not _deferring or (_pull_only is not None and chunk.get_dag_node() in _pull_only)


@contextmanager
def cook_pass():
    """Around one pass of cooks (a flush, a recook): a failure in it holds back what is below.

    Passes inside each other are one pass, and what failed stays failed until the
    outermost one ends: a recook runs a flush per chunk, and a chunk below one that failed
    must not cook in a later one of them.
    """
    global _pass_depth
    _pass_depth += 1
    try:
        yield
    finally:
        _pass_depth -= 1
        if not _pass_depth:
            _below_failed.clear()


def below_failed(chunk) -> bool:
    """Whether a chunk is below one whose cook failed in this pass (cook_pass)."""
    return bool(_below_failed) and chunk.get_dag_node() in _below_failed


def _set_failed(chunk, failed: bool) -> None:
    flag = ChunkFlags.State.FAILED
    if chunk.has_state(flag) != failed:
        if failed:
            chunk.set_state(flag)
        else:
            chunk.unset_state(flag)


@contextmanager
def solving():
    """Around one solve: the outermost one, outside a batch, flushes what it left pending.

    A solve inside another -- a param callback setting a value -- adds to the same pending
    set, so everything one edit reaches cooks once, after all of its callbacks have run.
    """
    global _solve_depth
    _solve_depth += 1
    try:
        yield
    finally:
        _solve_depth -= 1
    # Not on a raise -- the with-block's exception skips this.
    if _solve_depth == 0 and _batched is None:
        flush()


def flush(force: bool = False) -> None:
    """Cook every pending chunk, upstream first, until none are left.

    A cook can set params and so dirty more chunks; those are picked up by the next round.
    A chunk outside the active view is refused by cook() (blocked) and stays
    stale for recook_for_view. A chunk mid-cook stays pending for the flush around it.

    Args:
        force: Flush even inside another flush -- for a batched_solve() ending inside a
            cook (a Wedge setting its ranges), which reads the result straight after.
            A plain solve inside a cook waits for the outer flush instead.
    """
    if _held or (_deferring and _pull_only is None) or (_flushing and not force):
        return
    with cook_pass():
        _flush()


def _flush() -> None:
    global _flushing
    was_flushing, _flushing = _flushing, True
    # The first failure among this flush's cooks, raised on once the rest have cooked.
    error = None
    # The scene's state as skip_cook last read it here; only a cook changes it in between.
    states: list = []
    # The outermost flush's masked cooks share one cook stage, dropped as it ends.
    share = None if was_flushing else skip_cook.sharing()
    if share is not None:
        share.__enter__()
    try:
        rounds = 0
        while True:
            chunks = [c for c in _pending if _cooks_now(c)]
            if not chunks:
                break
            rounds += 1
            if rounds > _MAX_FLUSH_ROUNDS:
                _pending.clear()
                raise RuntimeError("chunk_cook.flush: cooks kept dirtying each other: "
                                   + ", ".join(getattr(c, "name", "?") for c in chunks))
            work = [(c, *_pending.pop(c)) for c in chunks]
            work.sort(key=lambda w: _cook_order(w[0]))
            for chunk, changes, was_stale in work:
                # Reached again since this round began: a cook before it set what it reads
                # (a Math Ops whose a is @Mult1.result@). Decided on with only the reasons it
                # had, it was skipped -- and the skip put back its old staleness over the
                # new dispatch's, so it never cooked at all.
                if chunk in _pending:
                    changes = changes + _pending.pop(chunk)[0]
                # Decided here, not when the solve reached it: the chunks above it
                # have cooked by now (upstream first) and handed down what changed.
                if skip_cook.should_skip(chunk, changes, states):
                    # Its layer already holds the result -- as stale as it was.
                    chunk._cook_stale = was_stale
                    continue
                if not chunk.can_cook():
                    skip_cook.passed_through(chunk, changes)
                # Not stale any more: it cooked since it was reached -- its own params,
                # set while it ran, reach it here, and its result already takes them in.
                if is_cook_stale(chunk):
                    # The cook itself -- and every guard on whether it may run now -- is
                    # cook(), reached through ChunkBase._cook. A chunk whose module has no
                    # execute() has nothing to run and stays stale.
                    states.clear()
                    try:
                        chunk._cook()
                    except Exception as e:  # noqa: BLE001 - raised on below, once the rest cooked
                        # cook() held back what is below it; the other chunks still cook.
                        error = error or e
    finally:
        _flushing = was_flushing
        if share is not None:
            share.__exit__(None, None, None)
    if error is not None and not isinstance(error, CookCancelled):
        raise error
    # The outermost flush, outside a batch (which runs its own): what was deferred until
    # the cooks were done. Each may solve again, which flushes and runs its own.
    if not was_flushing and _batch_deferred is None:
        while _solve_deferred:
            pending = list(_solve_deferred.values())
            _solve_deferred.clear()
            for fn in pending:
                fn()


@contextmanager
def batched_solve():
    """Hold back every solve until the block ends, then solve once.

    Setting a value solves everything downstream of it straight away, which is right for
    one edit and wasteful for many: setting N values that feed the same chunks runs their
    notices and callbacks N times, each against a half-updated set of inputs. A Wedge
    cooking one variant sets every range it has -- dozens -- and that alone cooked the
    chunks below it hundreds of times per variant.

    Inside the block a value change only marks what it affects dirty. When the block ends,
    each propagator that asked to solve is solved -- and the first solve already takes in
    every dirty node it is connected to, in dependency order, so the rest are clean by the
    time their turn comes and are skipped. Then flush() cooks everything downstream once,
    with all the new values in place.

    Solves asked for while the batch is being solved -- a param binding its storage
    attribute sets its value, which asks for a solve -- are queued behind it rather than
    run inside it. Run inside, each one nested a whole solve in the one before, and a batch
    of dozens of changes nested deep enough to exhaust the stack.

    Nested blocks join the outermost one. What after_batch() kept runs once it has ended
    and cooked, outside it.
    """
    global _batched, _batch_deferred, _after_batch
    if _batched is not None:
        yield
        return
    _batched = collections.deque()
    _batch_deferred = {}
    _after_batch = {}
    try:
        yield
        while _batched or _batch_deferred or any(_cooks_now(c) for c in _pending):
            while _batched:
                prop = _batched.popleft()
                # Not re-dirtied: an earlier solve in this loop may have cleaned it along
                # with everything else it reached, and dirtying it again would cook that twice.
                if prop.get_dag_node().dirty:
                    prop._solve_downstream()
            # Deferred work runs once the solves have settled; anything it changes queues
            # another round rather than being lost.
            pending = list(_batch_deferred.values())
            _batch_deferred.clear()
            for fn in pending:
                fn()
            # Forced: the batch may end inside a cook (a Wedge), whose caller reads the
            # result straight after. Cooks that set params queue another round above.
            flush(force=True)
    finally:
        after, _after_batch = _after_batch, None
        _batched = None
        _batch_deferred = None
    # Not on a raise -- the with-block's exception skips this. Outside the batch now, so what
    # each one changes solves and cooks as any change does.
    for fn in after.values():
        fn()


def is_cook_stale(chunk) -> bool:
    """Whether a chunk's layer may not hold the result of its current inputs.

    A chunk goes stale when a change reaches it and its module does not run -- it was
    blocked outside the active view, its layer was muted, or it had no layer to author into
    yet -- and when its layer is created, which is empty. A cook that runs clears it.
    chunk_stage.recook_for_view reads this to re-run only the chunks that need it: a chunk
    that is not stale still has its last result in its layer, muted or not.

    A chunk that has never cooked is stale.

    Args:
        chunk: The chunk.

    Returns:
        True if the chunk needs to cook.
    """
    return getattr(chunk, "_cook_stale", True)


def mark_cook_stale(chunk) -> None:
    """Record that a chunk's layer no longer holds the result of its inputs. See is_cook_stale."""
    chunk._cook_stale = True


def clear_cook_stale(chunk) -> None:
    """Record that a chunk's layer holds the result of its inputs. See is_cook_stale."""
    chunk._cook_stale = False


def cook(chunk, layer_of: Callable, stamp: Callable) -> bool:
    """Run chunk's module into its own edit layer, now, and nothing downstream of it.

    Deciding WHEN to cook -- a data change rather than a pure state change, once per edit,
    upstream first -- is dispatch and flush; this only decides whether the chunk CAN run
    right now, and runs it. It does not mark the chunk stale first: the solve does, so a
    cook that is refused leaves it stale for recook_for_view to pick up.

    Reached through ChunkBase._cook, which hands in the two things this needs from the
    modules above it: where a chunk's layer is, and how to stamp the module hash.

    Args:
        chunk: The chunk.
        layer_of: The layer of a chunk, or None (ChunkLayerManager.get_layer).
        stamp: Records on the chunk's prim the module source it cooked with
            (chunk_utils.stamp_module_hash), called as stamp(stage, chunk).

    Returns:
        True if the module was run -- also when its layer left the stage's layer stack
        part way through, which is skipped rather than raised. False if a guard refused
        it (no execute(), no stage, muted, not in the layer stack yet, blocked, being
        deleted, or already executing).
    """
    execute = chunk._module_hook("execute")
    # Nothing to run (a chunkgroup), so no layer to ask for: asking makes it, and a layer
    # made puts it on the stage. Made at the end of a load, after every chunk inside had
    # cooked, it changed the layer stack each of them recorded (skip_cook.scene_state) --
    # and the first edit after opening cooked all of them again.
    if execute is None:
        return False
    # The scene's stage, not get_stage(): a cook started inside another chunk's masked
    # cook would get that chunk's inputs as its stage.
    stage = chunk.get_scene_stage()
    ctx = chunk.get_edit_context()
    # A disabled chunk's edit layer is muted, i.e. removed from the stage's local
    # layer stack. Don't re-run its module: authoring into a muted layer raises
    # ("Layer ... is not in the local LayerStack"), and a disabled chunk must not
    # execute anyway.
    muted = bool(stage and ctx and stage.IsLayerMuted(ctx.identifier))
    # Separately: a chunk's own edit layer may not be inserted into the stage's
    # local layer stack YET at all -- not muted (present-but-disabled), just not
    # there yet. Hit live during load_chunks's bulk restore: a ListGroup proxy
    # row's value setter (chunk_core.py's ProxyParam.__setattr__, "value" case)
    # calls straight through to this same dispatch, bypassing the
    # chunk_notice._loading_stage guard that suppresses every OTHER reactive path
    # during that restore -- reaching here before get_or_create_layer has run for
    # this specific chunk, same "not in the local LayerStack" raise as the muted
    # case. load_chunks's own later, correctly-ordered explicit .execute() call
    # (after every chunk's layer is properly registered) still runs normally.
    not_yet_in_stack = bool(stage and ctx and not layer_arcs.is_editable(stage, ctx))
    # A blocked chunk (view-driven: not in the active view's tree -- e.g. connected to
    # an inactive view) must never run its module, whatever triggered the solve. recook
    # unblocks the active view's tree before executing it, so cooking is unaffected.
    # A chunk being deleted must not run its module -- teardown notices can land
    # back here after its params are already detached (see request_delete).
    # With no stage there is nothing for a module to author into: every chunk module
    # works through get_stage(), so running one stage-less is never useful and
    # crashes on the first stage call. The host can genuinely have no stage for a
    # window -- usdview clears its stage before opening the next file while still
    # pumping the Qt event loop, so a deferred rebuild, or a click landing in that
    # window, can reach here.
    # Below a chunk whose cook failed in this pass: what it would read is not there. It
    # keeps its last result, stale, and cooks in a later pass.
    if not (execute is not None and stage is not None
            and not getattr(chunk, '_executing', False)
            and not getattr(chunk, '_deleting', False)
            and not muted and not not_yet_in_stack and not chunk.is_blocked()
            and not below_failed(chunk)):
        return False
    if (blocker := blocked_above(chunk)) is not None:
        _mark_not_cooked(chunk, blocker)
        return False

    try:
        _run(chunk, stage, ctx, execute, layer_of, stamp)
    except Exception as e:
        _set_failed(chunk, True)
        below = chunk_scheduler.downstream_closure(chunk.get_dag_node())
        if not isinstance(e, CookBlocked):
            _below_failed.update(below)
            raise
        # The chunks below it wait out the pass; it does not.
        _below_failed.update(dict.fromkeys(node for node in below if node is not chunk.get_dag_node()))
        # Said on the node, not raised: a script, or the scene being opened, goes on. It is
        # not held back for the rest of the pass itself: wired a moment later -- a script
        # wires a chunk after its first cook -- it cooks again in the same pass.
        # A script's chunk not wired yet (chunk_scene.place) keeps quiet, and is not marked
        # as having said so: if it is still blocked once wired, that cook says it.
        pending = getattr(chunk, "_wiring_pending", False)
        if not pending and (chunk not in _blocked or getattr(chunk, "_cook_problem", None) != str(e)):
            logger.warning("%s is blocked: %s", chunk.name, e)
        _blocked.add(chunk)
        chunk._cook_problem = None if pending else str(e)
        # It shows nothing: what the cook made without the prim it could not see -- a
        # Scatter's fallback ground -- is not its result.
        if ctx is not None and ctx.anonymous:
            ctx.Clear()
        _block_below(chunk)
        return True
    _set_failed(chunk, False)
    chunk._cook_problem = None
    chunk._blocked_by = None
    if chunk in _blocked:
        _blocked.discard(chunk)
        _unblock_below(chunk)
    for fn in _after_cook_hooks:
        fn(chunk)
    # A chunk that looked for a prim nobody had made yet: made now, it cooks again, and finds
    # out whether it can see it.
    for waiting in skip_cook.waiting_on(chunk):
        waiting.execute()
    return True


def _block_below(chunk) -> None:
    """Mark every chunk below a blocked one as not cooked: red, and stale for when it is fixed.

    They may have cooked already -- opened from a file, a Cube below a Transform cooks before
    the Transform finds it is blocked -- and they cook again once it is unblocked.
    """
    for below in _chunks_below(chunk):
        _mark_not_cooked(below, chunk)
        mark_cook_stale(below)


def _unblock_below(chunk) -> None:
    """Clear what _block_below marked, once chunk cooks without being blocked.

    A chunk with nothing to cook of its own (a View, an Output) is never cooked to clear it.
    The rest are stale, and cook as the chunk's change reaches them.
    """
    for below in _chunks_below(chunk):
        blocker = getattr(below, "_blocked_by", None)
        if blocker is not None and blocker() is chunk:
            _set_failed(below, False)
            below._cook_problem = None
            below._blocked_by = None


def _chunks_below(chunk) -> list:
    return [node.propagator for node in chunk_scheduler.downstream_closure(chunk.get_dag_node())
            if node.propagator is not chunk and callable(getattr(node.propagator, "can_cook", None))]


def _mark_not_cooked(chunk, blocker) -> None:
    _set_failed(chunk, True)
    chunk._cook_problem = f"Not cooked: {blocker.name} above it is blocked."
    chunk._blocked_by = weakref.ref(blocker)


def blocked_above(chunk):
    """The blocked chunk upstream of chunk, if any: chunk does not cook until it is unblocked.

    Args:
        chunk: The chunk about to cook.

    Returns:
        A blocked chunk above it, or None.
    """
    if not _blocked:
        return None
    above = skip_cook.chunks_above(chunk)
    return next((b for b in list(_blocked) if b is not chunk and id(b) in above), None)


def cook_problem(chunk) -> str | None:
    """Why chunk's last cook failed or was refused, when there is something to say."""
    return getattr(chunk, "_cook_problem", None)


# Prim metadata that, set by a chunk below on a prim a chunk above writes under, makes USD
# refuse the write: an inactive prim has no children to author, an instance only proxies.
_BLOCKING = (("active", False), ("instanceable", True))


@contextmanager
def _lifted_from_below(chunk, layer, layer_of):
    """For chunk's cook, lift what the chunks below it set that blocks writing its own prims.

    Merge Instancers deactivates the instancers it merged, in its own layer. A fresh run
    cooks a Scatter before that layer holds anything; cooking the Scatter again found the
    deactivation there, and it could not define the Prototypes under its own instancer
    ("Failed to define UsdPrim"). Lifted only on the prims this chunk last wrote below, so
    it sees them as a fresh run does, and put back after: a chunk below that skips its cook
    keeps its result whole, and one that cooks clears them itself.
    """
    lifted = []
    if layer.anonymous:
        parents = set()

        def visit(path):
            if path.IsPrimPath():
                parents.update(path.GetParentPath().GetAncestorsRange())

        layer.Traverse(Sdf.Path.absoluteRootPath, visit)
        parents.discard(Sdf.Path.absoluteRootPath)
        if parents:
            for node in chunk_scheduler.downstream_closure(chunk.get_dag_node()):
                below = layer_of(node.propagator) if node.propagator is not chunk else None
                if below is None or below is layer:
                    continue
                for path in parents:
                    spec = below.GetPrimAtPath(path)
                    for key, blocking in _BLOCKING:
                        if spec is not None and spec.HasInfo(key) and spec.GetInfo(key) == blocking:
                            lifted.append((spec, key, blocking))
    if lifted:
        with Sdf.ChangeBlock():
            for spec, key, _ in lifted:
                spec.ClearInfo(key)
    try:
        yield
    finally:
        if lifted:
            with Sdf.ChangeBlock():
                for spec, key, value in lifted:
                    if spec:  # cleared away by a cook of the chunk below, made inside this one
                        spec.SetInfo(key, value)


_CHUNKS_ROOT = Sdf.Path(CHUNKS_ROOT)

#: The scratch layer each chunk now diff-cooking writes into, by id: its get_edit_context.
_scratch: dict[int, Sdf.Layer] = {}


#: The chunk layer each scratch layer stands in for, by the scratch layer's identifier.
_stands_for: dict[str, Sdf.Layer] = {}


def cooking_into(chunk) -> Sdf.Layer | None:
    """The scratch layer chunk's running diff cook writes into, or None: its edit context then."""
    return _scratch.get(id(chunk)) if _scratch else None


def real_layer(layer: Sdf.Layer | None) -> Sdf.Layer | None:
    """The chunk layer a diff cook's scratch layer stands in for; any other layer as it is."""
    if layer is None or not _stands_for:
        return layer
    return _stands_for.get(layer.identifier, layer)


@contextmanager
def _cooking_into(chunk, scratch: Sdf.Layer | None, ctx: Sdf.Layer):
    if scratch is None:
        yield
        return
    previous = _scratch.get(id(chunk))
    _scratch[id(chunk)] = scratch
    _stands_for[scratch.identifier] = ctx
    try:
        yield
    finally:
        _stands_for.pop(scratch.identifier, None)
        if previous is None:
            _scratch.pop(id(chunk), None)
        else:
            _scratch[id(chunk)] = previous


def _scratch_for(chunk, stage, ctx):
    """A fresh scratch layer for chunk's diff cook, or None to clear and rewrite ctx instead.

    Only a chunk's own anonymous layer, in the stage's local layer stack: a file layer is
    the user's and never cleared, and one an asset brings in by an arc is edited through it.
    USD_CHUNKS_DIFF_COOK=0 turns it off, for comparing.
    """
    if os.environ.get("USD_CHUNKS_DIFF_COOK", "1") == "0":
        return None
    if not ctx.anonymous or not stage.HasLocalLayer(ctx) or cooking_into(chunk) is not None:
        return None
    # Only once a plain cook has shown it leaves the layer stack alone (_run's probe).
    if getattr(chunk, "_diff_ok", None) is not True:
        return None
    # The same one every cook, emptied: a module may key what it keeps between cooks on its
    # edit context's identifier (UV Unwrap's last cook), and nothing composes it between them.
    scratch = getattr(chunk, "_diff_scratch", None)
    if scratch is None:
        scratch = chunk._diff_scratch = Sdf.Layer.CreateAnonymous("diff_cook")
    else:
        scratch.Clear()
    return scratch


def _keep_driven(chunk, ctx: Sdf.Layer, scratch: Sdf.Layer) -> None:
    """Carry the attributes chunk's params drive from ctx into scratch, before the reconcile.

    They are the params' state, re-authored on the scene's stage after the cook
    (reauthor_driven_attrs), never by the cook: left out of scratch, the reconcile would
    remove them only for that to add them back.
    """
    for _, param in chunk.iter_hierarchy():
        paths = getattr(param, "_driven_attr_paths", None)
        if not paths or param.get_chunk() is not chunk:
            continue
        for path in paths:
            prop = Sdf.Path(path)
            if (ctx.GetPropertyAtPath(prop) is not None and scratch.GetPropertyAtPath(prop) is None
                    and scratch.GetPrimAtPath(prop.GetPrimPath()) is not None):
                Sdf.CopySpec(ctx, prop, scratch, prop)


def _cook_into(chunk, stage, ctx, scratch, execute, layer_of, cached, skipping) -> None:
    """Run the module, or read its cook back from the cache, into scratch or else ctx."""
    target = scratch if scratch is not None else ctx
    with _cooking_into(chunk, scratch, ctx):
        if cached is None or not cached.restore(chunk, target):
            if cached is not None:
                cached.before_cook(chunk)
            if skipping:
                skip_cook.run(chunk, stage, target, execute, layer_of, _tie_break,
                              swap=ctx if scratch is not None else None)
            else:
                execute(chunk)
        elif skipping:
            skip_cook.restored(chunk, target)


def _writes_chunk_prims(layer: Sdf.Layer) -> bool:
    """Whether layer holds anything under /chunks, where the chunk prims are."""
    return layer.GetPrimAtPath(_CHUNKS_ROOT) is not None


#: Set while a reconcile writes into a chunk's layer: its writes are not _Writes' outside ones.
_reconciling = False


class _Writes:
    """Whether anything but its reconcile wrote to a chunk's layer since its last finish.

    A param bound to a prim attribute writes into the layer the moment it changes, before
    any cook, and reauthor_driven_attrs after one. While nothing did, what the reconcile
    changed is what changed since the chunks below last looked, and skip_cook.finish need
    not compare the whole layer again to find it. Heard through Sdf's notice for this one
    layer. A cook inside an Sdf.ChangeBlock would hear it late, but a cook there reads a
    stage that has not recomposed, and does not work anyway.
    """

    def __init__(self, layer: Sdf.Layer):
        self.layer_id = layer.identifier
        #: Unknown until a first finish has seen the layer.
        self.outside = True
        self._listener = Tf.Notice.Register(Sdf.Notice.LayersDidChangeSentPerLayer, self._heard, layer)

    def _heard(self, notice, sender) -> None:
        if not _reconciling:
            self.outside = True


def _writes_to(chunk, ctx: Sdf.Layer) -> _Writes:
    writes = getattr(chunk, "_diff_writes", None)
    if writes is None or writes.layer_id != ctx.identifier:
        writes = chunk._diff_writes = _Writes(ctx)
    return writes


def _diff_cooked(chunk, stage, ctx, execute, layer_of, cached, kept) -> set[Sdf.Path] | None:
    """Cook chunk into a scratch layer and reconcile it into ctx: the prims it changed, or
    None if it did not diff cook.

    None, with ctx untouched, when it cannot (_scratch_for) or must not: a cook that changed
    the layer stack or what is muted -- an Edit Target placing layers, a Layer chunk, a gate
    -- or raised. Most such chunks show it on their first, plain, cook and are never diff
    cooked (_run); one that only does it later is caught here, and cooked by clearing and
    rewriting its layer from then on: its stack edits land on layers the cook stage
    composes too, while it is open. What it changed is handed down as everything changed,
    as a cook that changed the scene is.
    """
    global _reconciling
    scratch = _scratch_for(chunk, stage, ctx)
    if scratch is None:
        return None
    if kept:
        scratch.subLayerPaths = kept
    state = skip_cook.scene_state(stage)
    reason = None
    try:
        _cook_into(chunk, stage, ctx, scratch, execute, layer_of, cached, True)
    except (CookBlocked, CookCancelled):
        raise
    except Exception as e:  # noqa: BLE001 -- cooked again the old way, which raises it if it is the module's
        reason = f"it raised {type(e).__name__}"
    after = skip_cook.scene_state(stage)
    if reason is None and after != state:
        reason = "it changes the layer stack"
    # Layers it puts in the stack as its own layer's sublayers reach the stack only with the
    # reconcile, past the check above: a Layer chunk, an arc layer.
    if reason is None and (kept or list(scratch.subLayerPaths)):
        reason = "it puts layers in the stack"
    # Overrides on chunk prims (Override Chunk Prim) are read back by the framework as the
    # target chunk's params, from the scene's stage, as the layer changes.
    if reason is None and _writes_chunk_prims(scratch):
        reason = "it writes onto chunk prims"
    if reason is not None:
        chunk._diff_ok = False
        skip_cook.changed_scene(chunk, state, after)
        logger.debug("%s is cooked by clearing its layer from now on: %s", chunk.name, reason)
        return None
    _keep_driven(chunk, ctx, scratch)
    _writes_to(chunk, ctx)
    _reconciling = True
    try:
        return layer_reconcile.reconcile(scratch, ctx)
    except Exception:  # noqa: BLE001 -- a field Sdf will not set where it is: the cook stands
        # The layer may be half changed: replaced whole with the cook's result, as clearing and
        # rewriting it would have, and everything handed down. The same field would be refused
        # every cook, so the chunk is cooked the old way from now on.
        logger.warning("%s: its cook could not be applied in place; replacing its layer, and "
                       "cooking it by clearing its layer from now on", chunk.name, exc_info=True)
        chunk._diff_ok = False
        _reconciling = False
        with Sdf.ChangeBlock():
            ctx.TransferContent(scratch)
        return {Sdf.Path.absoluteRootPath}
    finally:
        _reconciling = False


def _run(chunk, stage, ctx, execute, layer_of, stamp) -> None:
    """cook()'s cook, once it has decided the chunk may run."""
    with executing(chunk), skip_cook.unmasked(), cook_timings.timed(chunk):
        if ctx:
            try:
                with Usd.EditContext(stage, layer_arcs.edit_target(stage, ctx)), \
                        _lifted_from_below(chunk, ctx, layer_of):
                    # What the chunks below saw, to tell them what this cook
                    # changed (skip_cook). Anonymous only, like the Clear.
                    skipping = ctx.anonymous
                    before = skip_cook.baseline(chunk, ctx) if skipping else None
                    # A cook starts from an empty layer, so a module
                    # authors its whole result every time and never reads
                    # its own last one back. Anonymous only: that is a
                    # layer this chunk alone owns and rebuilds on load.
                    # A chunk in a context with a file layer is handed
                    # that file layer, shared and the user's own.
                    # Its sublayers kept, in one change: they are layers the chunk put in
                    # the stack (a Layer chunk's), and between the Clear and the module
                    # putting them back, everything listening saw them gone -- an Edit
                    # Target on one reset itself. The module still sets them every cook.
                    # Only what this chunk itself last cooked there: a chunk handed a
                    # layer by its hash (a new scene, a reload) starts from empty.
                    kept = (list(ctx.subLayerPaths)
                            if ctx.anonymous and getattr(chunk, "_cooked_into", None) == ctx.identifier else [])
                    # A module that declared cache_cook may have this exact
                    # cook on disk already -- read it back instead. Anonymous
                    # only, for the same reason as the Clear.
                    cached = cook_cache.lookup(chunk) if ctx.anonymous else None
                    reconciled = _diff_cooked(chunk, stage, ctx, execute, layer_of, cached, kept)
                    if reconciled is None:
                        # A chunk's first cook is a plain one, and decides whether it may be
                        # diff cooked: not if it changed the layer stack or what is muted (an
                        # Edit Target, a gate) or put layers under its own (a Layer chunk).
                        # Tried and given up instead, a diff cook left such a chunk's stack
                        # edits half made.
                        probing = ctx.anonymous and getattr(chunk, "_diff_ok", None) is None
                        state = skip_cook.scene_state(stage) if probing else None
                        if ctx.anonymous:
                            with Sdf.ChangeBlock():
                                ctx.Clear()
                                if kept:
                                    ctx.subLayerPaths = kept
                        _cook_into(chunk, stage, ctx, None, execute, layer_of, cached, skipping)
                        if probing:
                            chunk._diff_ok = (skip_cook.scene_state(stage) == state and not ctx.subLayerPaths
                                              and not _writes_chunk_prims(ctx))
                    if ctx.anonymous:
                        chunk._cooked_into = ctx.identifier
                    if not ctx.anonymous:
                        skip_cook.unaccounted(chunk)
                    # What a param drives is its own state, not the cook's
                    # output -- see ChunkBase.reauthor_driven_attrs.
                    if ctx.anonymous:
                        chunk.reauthor_driven_attrs()
                    if skipping:
                        # What the reconcile changed, when nothing else wrote to the layer since
                        # the chunks below last saw it (_Writes): no second look at every spec.
                        writes = getattr(chunk, "_diff_writes", None)
                        known = (reconciled if reconciled is not None and writes is not None
                                 and writes.layer_id == ctx.identifier and not writes.outside else None)
                        skip_cook.finish(chunk, ctx, before, known)
                        if writes is not None:
                            writes.outside = False
                    if cached is not None:
                        cached.after_cook(chunk, ctx)
                clear_cook_stale(chunk)
                stamp(stage, chunk)
            except Tf.ErrorException:
                # The layer left the stage's local layer stack between the
                # check above and here. It can: a cook reached from inside
                # another cook mutes layers (a gate suppressing a branch),
                # and this dispatch is reached re-entrantly -- a module
                # setting a param during its own execute lands straight back
                # here for whatever that param drives.
                #
                # Re-checked rather than swallowed: only the "not in the
                # local LayerStack" case is skipped, and any other error a
                # module raises still surfaces. Skipping is right because it
                # is what the guards above already decided -- a chunk whose
                # layer is not in the stack has nowhere to author, and the
                # cook that put it back will run it.
                if stage.IsLayerMuted(ctx.identifier) or not layer_arcs.is_editable(stage, ctx):
                    # Logged as the solve always has, under chunk_notice.
                    chunk_notice.logger.debug(
                        "skipped cooking %s: its layer left the stage's "
                        "layer stack mid-solve",
                        chunk.name,
                    )
                else:
                    raise
        else:
            # No edit layer of its own -- author into the stage's current target.
            execute(chunk)
            skip_cook.unaccounted(chunk)
            clear_cook_stale(chunk)
            stamp(stage, chunk)


@contextmanager
def executing(prop):
    """The execution scope around one chunk's module.execute(): what a cook sets while it runs.

    Marks prop as executing (its own guard against being cooked re-entrantly), raises
    chunk_notice._usd_write_active so the stage listeners skip the writes the module makes,
    and names the chunk in chunk_notice._executing_chunk so an Edit Target resolves from its
    place in the graph. cook() is the one caller; those two live in chunk_notice because
    every listener reads them from there.

    A module that sets its own params while it runs -- a Light showing its prim's attributes,
    a Wedge writing a variant's ranges -- solves them there and then: the param callbacks
    run during the cook. The chunks below only go pending, and flush() cooks them once this
    cook has ended, against the layer it finished.

    Args:
        prop: The chunk about to run its module.
    """
    prev_executing_chunk = chunk_notice._executing_chunk
    prev_usd_write_active = chunk_notice._usd_write_active
    prop._executing = True
    chunk_notice._usd_write_active = True
    chunk_notice._executing_chunk = getattr(prop, "get_chunk", lambda: None)()
    # The stage's resolver context, bound for the module: a file it opens itself
    # (Sdf.Layer.FindOrOpen on a param's path) is found as the stage finds it -- a relative
    # path from the network's folder -- not from the working directory (asset_paths).
    stage = prop.get_stage() if hasattr(prop, "get_stage") else None
    try:
        with ExitStack() as bound:
            if stage is not None:
                bound.enter_context(Ar.ResolverContextBinder(stage.GetPathResolverContext()))
            yield
    finally:
        # Restored, not cleared: a cook can be reached from inside a guarded
        # write (a rename triggers one), and clearing outright unsuppressed
        # that outer write for whatever it still had left to do.
        chunk_notice._usd_write_active = prev_usd_write_active
        chunk_notice._executing_chunk = prev_executing_chunk
        prop._executing = False
    # Not on a raise: a module that fails ends the solve it was part of, as it always has.
    #
    # A chunk that has just cooked is clean. What it set on itself while it ran -- and the
    # params it created, whose nodes start out dirty -- dirtied it again after its solve had
    # cleaned it, and its result already takes all of that in. Left dirty, the next solve
    # anywhere below it walked up to it (chunk_scheduler.dirty_component) and cooked it
    # again: a Light, on every edit of the Transform after it.
    #
    # Its reasons go too, as chunk_scheduler.solve clears a node it solved. A cook outside
    # any solve -- a View's recook, which usdview runs after every open -- left a Light's
    # rebuilt params' GROUP_CHANGED here, and the next edit handed them to skip_cook as a
    # change of the Light's own, so it could never be skipped.
    node = prop.get_dag_node()
    node._dirty = False
    node._changes.clear()
    node._index = 0


def is_cook_stage(stage) -> bool:
    """Whether stage is a running cook's stage of its inputs (skip_cook), not the scene's."""
    return skip_cook.is_cook_stage(stage)


@contextmanager
def outside_cook():
    """The scene's stage for what runs during a cook but is not part of it.

    Another chunk's notice listener, run while a chunk cooks: its get_stage() is the
    scene's, and what it looks up is not the cook's read.
    """
    with skip_cook.unmasked():
        yield


def inputs_rewired(node) -> None:
    """A node gained or lost an input: what it and everything below it cooked on is gone.

    Called by DagNode when one of its input edges changes. The record skip-cooking keeps
    of what a chunk read is what goes, and the chunk goes stale: a cook sees only what is
    wired above it, so its last result no longer says what it would make now. A script
    wires a chunk after its first cook, and a Transform made below a Cube kept the result
    of a cook that could not see the Cube.
    """
    skip_cook.inputs_rewired(node)
    # A chunk alone has can_cook; a param's input edges are no cook's.
    prop = node.propagator
    if callable(getattr(prop, "can_cook", None)):
        mark_cook_stale(prop)
        # Blocked, or not cooked below a blocked one: the next flush finds out whether the
        # new wire fixed it. A script wires a chunk after its first cook, which was blocked.
        if prop in _blocked or getattr(prop, "_blocked_by", None) is not None:
            dispatch(prop, node, [], None)


def dispatch(chunk, node, changes, seed) -> None:
    """Mark a chunk the solve has reached with a data change stale, and pending for flush().

    The cook half of ChunkNoticePropagator._solve_downstream: the solve has already run the
    chunk's notices and callbacks. Whether it cooks -- or is skipped because nothing it
    reads changed (skip_cook) -- is decided in flush(), once the chunks above it have cooked.

    Args:
        chunk: The chunk the solve reached.
        node: Its DagNode.
        changes: The (alias, propagator) changes that reached it.
        seed: The propagator whose execute() started the solve.
    """
    skip_cook.note_state(chunk)
    if chunk in _pending:
        _pending[chunk][0].extend(changes)
    else:
        _pending[chunk] = (list(changes), is_cook_stale(chunk))
    # Stale until the module actually runs. Every reason flush() has for not running
    # it -- blocked, muted, no layer yet, no stage -- leaves the layer holding a result
    # the change never reached, and recook_for_view cooks a chunk again on exactly that.
    # A chunk with no execute() (a chunkgroup) stays stale, which tells recook_for_view
    # its inside needs cooking.
    mark_cook_stale(chunk)


def cook_all(chunks) -> None:
    """Cook every one of chunks, upstream first, whether or not anything reached it.

    Each is made pending as a solve reaching it would make it, with no change to give as a
    reason -- so none is skipped (skip_cook skips only for a change above it) -- and flush()
    cooks them, through the same guards as any cook: a chunk outside the active view is
    refused and stays stale for recook_for_view. No notices are sent; nothing changed.

    Args:
        chunks: The chunks, nested ones included (SceneSender.get_chunks).
    """
    with solving():
        for chunk in chunks:
            dispatch(chunk, chunk.get_dag_node(), [], None)
