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

The graph itself (DagNode, notices) is chunk_notice's; the graph algorithms are
chunk_scheduler's. Chunk authors never call anything here: they write execute(node) and
call chunk.execute() to run a chunk again.
"""

from __future__ import annotations

import collections
from collections.abc import Callable
from contextlib import contextmanager

from pxr import Tf, Usd

from usd_chunks import chunk_notice, chunk_scheduler, cook_cache, skip_cook

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

# Follow-up work held until the outermost flush has cooked, by key, outside a batch.
_solve_deferred: dict = {}

# What runs after every cook, once its write guard is down. See after_cook().
_after_cook_hooks: list[Callable] = []


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


def queue(prop) -> bool:
    """Inside batched_solve(), hold prop's solve back until the block ends.

    Returns:
        True if it was queued; False outside a batch, where the caller solves now.
    """
    if _batched is None:
        return False
    _batched.append(prop)
    return True


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
    global _flushing
    if _flushing and not force:
        return
    was_flushing, _flushing = _flushing, True
    # The scene's state as skip_cook last read it here; only a cook changes it in between.
    states: list = []
    # The outermost flush's masked cooks share one cook stage, dropped as it ends.
    share = None if was_flushing else skip_cook.sharing()
    if share is not None:
        share.__enter__()
    try:
        rounds = 0
        while True:
            chunks = [c for c in _pending if not getattr(c, "_executing", False)]
            if not chunks:
                break
            rounds += 1
            if rounds > _MAX_FLUSH_ROUNDS:
                _pending.clear()
                raise RuntimeError("chunk_cook.flush: cooks kept dirtying each other: "
                                   + ", ".join(getattr(c, "name", "?") for c in chunks))
            work = [(c, *_pending.pop(c)) for c in chunks]
            work.sort(key=lambda w: chunk_scheduler.get_depth(w[0].get_dag_node()))
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
                    chunk._cook()
    finally:
        _flushing = was_flushing
        if share is not None:
            share.__exit__(None, None, None)
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

    Nested blocks join the outermost one.
    """
    global _batched, _batch_deferred
    if _batched is not None:
        yield
        return
    _batched = collections.deque()
    _batch_deferred = {}
    try:
        yield
        while _batched or _batch_deferred or any(not getattr(c, "_executing", False) for c in _pending):
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
        _batched = None
        _batch_deferred = None


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
    not_yet_in_stack = bool(stage and ctx and not stage.HasLocalLayer(ctx))
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
    if not (execute is not None and stage is not None
            and not getattr(chunk, '_executing', False)
            and not getattr(chunk, '_deleting', False)
            and not muted and not not_yet_in_stack and not chunk.is_blocked()):
        return False

    with executing(chunk), skip_cook.unmasked():
        if ctx:
            try:
                with Usd.EditContext(stage, ctx):
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
                    if ctx.anonymous:
                        ctx.Clear()
                    # A module that declared cache_cook may have this exact
                    # cook on disk already -- read it back instead. Anonymous
                    # only, for the same reason as the Clear above.
                    cached = cook_cache.lookup(chunk) if ctx.anonymous else None
                    if cached is None or not cached.restore(chunk, ctx):
                        if cached is not None:
                            cached.before_cook(chunk)
                        if skipping:
                            skip_cook.run(chunk, stage, ctx, execute, layer_of)
                        else:
                            execute(chunk)
                    elif skipping:
                        skip_cook.restored(chunk, ctx)
                    if not ctx.anonymous:
                        skip_cook.unaccounted(chunk)
                    # What a param drives is its own state, not the cook's
                    # output -- see ChunkBase.reauthor_driven_attrs.
                    if ctx.anonymous:
                        chunk.reauthor_driven_attrs()
                    if skipping:
                        skip_cook.finish(chunk, ctx, before)
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
                if stage.IsLayerMuted(ctx.identifier) or not stage.HasLocalLayer(ctx):
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
    for fn in _after_cook_hooks:
        fn(chunk)
    return True


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
    try:
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
    prop.get_dag_node()._dirty = False


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
    of what a chunk read is what goes.
    """
    skip_cook.inputs_rewired(node)


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
