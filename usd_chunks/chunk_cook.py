"""Cooking: when a chunk runs its module's execute(node).

The model, in two steps:

1. The graph runs through and finds the dependent nodes to cook. A change calls
   chunk.execute() (ChunkNoticePropagator.execute), which dirties the chunk and solves
   everything downstream of it: notices and callbacks run there and then, and each chunk
   the solve reaches with a data change is handed to dispatch() here, which marks it stale
   and pending. Nothing cooks during the solve. batched_solve() holds the solves back and
   runs them once.
2. When the outermost solve (or batch) ends, flush() cooks every pending chunk, upstream
   first. A cook calls its chunk's execute. That is ChunkBase._cook: it checks the chunk
   may run now, then runs its module into the chunk's own layer.

This module decides *when* (dispatch, flush, batching); ChunkBase._cook is *how*. The how
stays on the chunk because it needs the chunk's layers and registry, which sit above
chunk_notice -- and chunk_notice imports this module, so it imports nothing above it.

Stale tracking (is_cook_stale) records a chunk whose cook was refused -- blocked outside
the active view, muted, no layer yet -- so chunk_stage.recook_for_view cooks it later.

The graph itself (DagNode, notices) is chunk_notice's; the graph algorithms are
chunk_scheduler's. Chunk authors never call anything here: they write execute(node) and
call chunk.execute() to run a chunk again.
"""

from __future__ import annotations

import collections
from contextlib import contextmanager

from usd_chunks import chunk_notice, chunk_scheduler, skip_cook

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
    """Run fn once when the enclosing batch has drained, instead of now.

    For bookkeeping that only has to be right once the dust settles and costs something each
    time -- refreshing a chunk's gizmo anchors reads every param it has, and a Wedge change
    asked for it once per param written, hundreds of times. Deferred under the same key it
    runs once, with the batch's final values.

    Args:
        key: Identifies the work, so asking twice runs it once.
        fn: What to run.

    Returns:
        False outside a batch -- the caller runs fn itself, straight away.
    """
    if _batch_deferred is None:
        return False
    _batch_deferred[key] = fn
    return True


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
    A chunk outside the active view is refused by ChunkBase._cook (blocked) and stays
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
                if skip_cook.enabled():
                    # Decided here, not when the solve reached it: the chunks above it
                    # have cooked by now (upstream first) and handed down what changed.
                    if skip_cook.should_skip(chunk, changes):
                        # Its layer already holds the result -- as stale as it was.
                        chunk._cook_stale = was_stale
                        continue
                    # A chunk with no cook of its own (a chunkgroup) changes what is
                    # below it without a cook to say what: everything.
                    if not chunk.can_cook():
                        skip_cook.hand_down(chunk, skip_cook.EVERYTHING)
                # Not stale any more: it cooked since it was reached -- its own params,
                # set while it ran, reach it here, and its result already takes them in.
                if is_cook_stale(chunk):
                    # The cook itself -- and every guard on whether it may run now -- is
                    # ChunkBase._cook. A chunk whose module has no execute() has nothing
                    # to run and stays stale.
                    chunk._cook()
    finally:
        _flushing = was_flushing


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


@contextmanager
def executing(prop):
    """The execution scope around one chunk's module.execute(): what a cook sets while it runs.

    Marks prop as executing (its own guard against being cooked re-entrantly), raises
    chunk_notice._usd_write_active so the stage listeners skip the writes the module makes,
    and names the chunk in chunk_notice._executing_chunk so an Edit Target resolves from its
    place in the graph. ChunkBase._cook is the one caller; those two live in chunk_notice because
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


def inputs_rewired(node) -> None:
    """A node gained or lost an input: what it and everything below it cooked on is gone.

    Called by DagNode when one of its input edges changes. Only skip-cooking keeps a record
    of what a chunk read, so this is a no-op with it off.
    """
    if skip_cook.enabled():
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
    if skip_cook.enabled():
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
