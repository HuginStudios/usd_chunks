"""Cooking: when a chunk runs its module's execute(node).

The model, in two steps:

1. The graph runs through and finds the dependent nodes to cook. A change calls
   chunk.execute() (ChunkNoticePropagator.execute), which dirties the chunk and solves
   everything downstream of it; for each chunk the solve reaches with a data change it
   calls dispatch() here. batched_solve() holds the solves back and runs them once.
2. A cook calls its chunk's execute. That is ChunkBase._cook: it checks the chunk may run
   now, then runs its module into the chunk's own layer.

This module decides *when* (step 1's decisions); ChunkBase._cook is *how* (step 2). The
how stays on the chunk because it needs the chunk's layers and registry, which sit above
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

from usd_chunks import chunk_notice, skip_cook

# The chunks the running cook holds back until it has finished, in the order they were reached;
# None while no cook runs. See executing().
_held_cooks: dict | None = None

# The propagators whose solve an enclosing batched_solve() is holding back, in the order they
# asked, each with the batch clock at the time it asked; None outside a batch.
_batched: list | None = None

# Inside a batch, a clock that ticks once per queued solve and once per cook, and when each
# chunk last cooked by it. None outside a batch. See _cooked_since.
_batch_clock = 0
_batch_cooked: dict | None = None

# The batch clock when the solve now draining was queued; None when no queued solve is running.
_solving_since: int | None = None

# Follow-up work held until an enclosing batch has drained, by key; None outside a batch.
_batch_deferred: dict | None = None


def queue(prop) -> bool:
    """Inside batched_solve(), hold prop's solve back until the block ends.

    Returns:
        True if it was queued; False outside a batch, where the caller solves now.
    """
    if _batched is None:
        return False
    _batched.append((prop, _tick()))
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


def _tick() -> int:
    global _batch_clock
    _batch_clock += 1
    return _batch_clock


def _cooked_since(prop) -> bool:
    """Whether prop's chunk already cooked after the change the draining solve is for.

    A batch solves the changes it held back one after another, and the first solve cooks
    everything below them with every new value in place. Expression params re-announce their
    value only once the param they read has changed -- which happens during that first solve,
    so they are queued behind it, and solving them re-cooked every chunk below a second time
    with the same values. A chunk that cooked after the queued change was made has read it
    already: its module is skipped, while everything else the solve does -- widgets, the
    param's own USD write -- still happens. A later change is queued with a later clock, and
    cooks it again.
    """
    if _batch_cooked is None or _solving_since is None:
        return False
    return _batch_cooked.get(id(prop), -1) > _solving_since


@contextmanager
def batched_solve():
    """Hold back every solve until the block ends, then solve once.

    Setting a value solves everything downstream of it straight away, which is right for
    one edit and wasteful for many: setting N values that feed the same chunks cooks those
    chunks N times, each against a half-updated set of inputs, and only the last cook
    counts. A Wedge cooking one variant sets every range it has -- dozens -- and that alone
    cooked the chunks below it hundreds of times per variant.

    Inside the block a value change only marks what it affects dirty. When the block ends,
    each propagator that asked to solve is solved -- and the first solve already takes in
    every dirty node it is connected to, in dependency order, so the rest are clean by the
    time their turn comes and are skipped. Everything downstream cooks once, with all the
    new values in place.

    Solves asked for while the batch is being solved -- a param binding its storage
    attribute sets its value, which asks for a solve -- are queued behind it rather than
    run inside it. Run inside, each one nested a whole solve in the one before, and a batch
    of dozens of changes nested deep enough to exhaust the stack.

    Nested blocks join the outermost one.
    """
    global _batched, _batch_cooked, _solving_since, _batch_deferred
    if _batched is not None:
        yield
        return
    _batched = collections.deque()
    _batch_cooked = {}
    _batch_deferred = {}
    try:
        yield
        while _batched or _batch_deferred:
            while _batched:
                prop, queued_at = _batched.popleft()
                # Not re-dirtied: an earlier solve in this loop may have cleaned it along
                # with everything else it reached, and dirtying it again would cook that twice.
                if prop.get_dag_node().dirty:
                    _solving_since = queued_at
                    try:
                        prop._solve_downstream()
                    finally:
                        _solving_since = None
            # Deferred work runs once the solves have settled; anything it changes queues
            # another round rather than being lost.
            pending = list(_batch_deferred.values())
            _batch_deferred.clear()
            for fn in pending:
                fn()
    finally:
        _batched = None
        _batch_cooked = None
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
    a Wedge writing a variant's ranges -- solves them there and then, and that solve reached
    everything downstream of the chunk while the chunk was still half way through its own
    cook: those chunks cooked against a layer it had not finished, and cooked again once it
    had, when the change that started it all got to them. The param callbacks still run
    during the cook; the chunks below are held (see dispatch) and cooked once,
    when it ends. The edit that started the cook still returns with all of them cooked.

    Args:
        prop: The chunk about to run its module.
    """
    global _held_cooks
    prev_executing_chunk = chunk_notice._executing_chunk
    prev_usd_write_active = chunk_notice._usd_write_active
    prev_held_cooks = _held_cooks
    held: dict = {}
    prop._executing = True
    chunk_notice._usd_write_active = True
    chunk_notice._executing_chunk = getattr(prop, "get_chunk", lambda: None)()
    _held_cooks = held
    try:
        yield
    finally:
        # Restored, not cleared: a cook can be reached from inside a guarded
        # write (a rename triggers one), and clearing outright unsuppressed
        # that outer write for whatever it still had left to do.
        chunk_notice._usd_write_active = prev_usd_write_active
        chunk_notice._executing_chunk = prev_executing_chunk
        _held_cooks = prev_held_cooks
        prop._executing = False
    # Not on a raise: a module that fails ends the solve it was part of, as it always has.
    #
    # A chunk that has just cooked is clean. What it set on itself while it ran -- and the
    # params it created, whose nodes start out dirty -- dirtied it again after its solve had
    # cleaned it, and its result already takes all of that in. Left dirty, the next solve
    # anywhere below it walked up to it (chunk_scheduler.dirty_component) and cooked it
    # again: a Light, on every edit of the Transform after it. Cleaned before the held
    # chunks solve, so their solves do not walk up and cook it a second time either.
    prop.get_dag_node()._dirty = False
    for chunk in held:
        if chunk.get_dag_node().dirty:
            chunk._solve_downstream()


def inputs_rewired(node) -> None:
    """A node gained or lost an input: what it and everything below it cooked on is gone.

    Called by DagNode when one of its input edges changes. Only skip-cooking keeps a record
    of what a chunk read, so this is a no-op with it off.
    """
    if skip_cook.enabled():
        skip_cook.inputs_rewired(node)


def dispatch(chunk, node, changes, seed) -> None:
    """Cook a chunk the solve has reached with a data change -- or decide it must wait.

    The cook half of ChunkNoticePropagator._solve_downstream: the solve has already run the
    chunk's notices and callbacks. This holds it if the chunk above it is still cooking,
    skips it if nothing it reads changed (skip_cook), marks it stale, and cooks it unless
    this batch already did.

    Args:
        chunk: The chunk the solve reached.
        node: Its DagNode.
        changes: The (alias, propagator) changes that reached it.
        seed: The propagator whose execute() started the solve.
    """
    # A chunk setting its own params mid-cook reaches everything below it from
    # here. Those wait for its cook to end -- left dirty, so whichever solve gets
    # to them next, this one's end or an enclosing one, cooks them once. See
    # executing().
    if (
        _held_cooks is not None
        and chunk is not chunk_notice._executing_chunk
        and chunk_notice._chunk_of(seed) is chunk_notice._executing_chunk
    ):
        node._dirty = True
        _held_cooks[chunk] = None
        # Its reasons go with it: the solve that holds it clears them, and
        # the skip decision when it is solved later needs them.
        if skip_cook.enabled():
            skip_cook.hold_reasons(chunk, changes)
        return
    if skip_cook.enabled():
        skip_cook.note_state(chunk)
        # Dirtied only by chunks above it cooking, and none of what they
        # changed is anything it reads: its layer already holds the result.
        # Not stale either -- there is nothing for recook_for_view to redo.
        if skip_cook.should_skip(chunk, changes):
            return
        # A chunk with no cook of its own (a chunkgroup) changes what is
        # below it without a cook to say what: everything.
        if not chunk.can_cook():
            skip_cook.hand_down(chunk, skip_cook.EVERYTHING)
    # Stale until the module actually runs below. Every reason this dispatch has
    # for not running it -- blocked, muted, no layer yet, no stage -- leaves the
    # layer holding a result the change never reached, and recook_for_view cooks
    # a chunk again on exactly that. A chunk with no execute() (a chunkgroup)
    # stays stale, which tells recook_for_view its inside needs cooking.
    mark_cook_stale(chunk)
    if _cooked_since(chunk):
        chunk._cook_stale = False
    # The cook itself -- and every guard on whether it may run now -- is
    # ChunkBase._cook. A chunk whose module has no execute() has nothing to run
    # and stays stale.
    elif chunk._cook():
        # True once cook() got past its guards, including when the layer left
        # the stack mid-cook. A module that raises ends the whole batch, and
        # this record with it, so there is nothing to note on that path.
        if _batch_cooked is not None:
            _batch_cooked[id(chunk)] = _tick()
