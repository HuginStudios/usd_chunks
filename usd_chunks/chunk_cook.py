"""Cooking: when a chunk runs its module's execute(node), and how.

The model, in two steps:

1. The graph runs through and finds the dependent nodes to cook. A change calls
   chunk.execute() (ChunkNoticePropagator.execute), which dirties the chunk and solves
   everything downstream of it; for each chunk the solve reaches with a data change it
   calls dispatch() here. batched_solve() holds the solves back and runs them once.
2. A cook calls its chunk's execute. cook() checks the chunk may run now, then runs its
   module into the chunk's own layer.

Stale tracking (is_cook_stale) records a chunk whose cook was refused -- blocked outside
the active view, muted, no layer yet -- so chunk_stage.recook_for_view cooks it later.

The graph itself (DagNode, notices) is chunk_notice's; the graph algorithms are
chunk_scheduler's. Chunk authors never call anything here: they write execute(node) and
call chunk.execute() to run a chunk again.
"""

from __future__ import annotations

import collections
from contextlib import contextmanager

from pxr import Tf, Usd

from usd_chunks import chunk_core, chunk_notice, chunk_registry, chunk_utils, cook_cache, skip_cook

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
    place in the graph. cook() is the one caller; those two live in chunk_notice because
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
    # cook(). A chunk whose module has no execute() has nothing to run
    # and stays stale.
    elif cook(chunk):
        # True once cook() got past its guards, including when the layer left
        # the stack mid-cook. A module that raises ends the whole batch, and
        # this record with it, so there is nothing to note on that path.
        if _batch_cooked is not None:
            _batch_cooked[id(chunk)] = _tick()


def cook(chunk) -> bool:
    """Run chunk's module into its own edit layer, now, and nothing downstream of it.

    Owned by the framework, not meant to be overridden: a chunk's behaviour is its
    module's execute(). Deciding WHEN to cook -- a data change rather than a pure state
    change, not twice in one batch -- is the solve's (dispatch, above);
    this only decides whether the chunk CAN run right now, and runs it. It does not
    mark the chunk stale first: the solve does, so a cook that is refused leaves it
    stale for recook_for_view to pick up.

    Returns:
        True if the module was run -- also when its layer left the stage's layer stack
        part way through, which is skipped rather than raised. False if a guard refused
        it (no execute(), no stage, muted, not in the layer stack yet, blocked, being
        deleted, or already executing).
    """
    execute = chunk._module_hook("execute")
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
                    skipping = skip_cook.enabled() and ctx.anonymous
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
                            skip_cook.run(chunk, stage, ctx, execute,
                                          chunk_registry.ChunkLayerManager().get_layer)
                        else:
                            execute(chunk)
                    elif skipping:
                        skip_cook.restored(chunk, ctx)
                    if skip_cook.enabled() and not ctx.anonymous:
                        skip_cook.unaccounted(chunk)
                    # What a param drives is its own state, not the cook's
                    # output -- see ChunkBase.reauthor_driven_attrs.
                    if ctx.anonymous:
                        chunk.reauthor_driven_attrs()
                    if skipping:
                        skip_cook.finish(chunk, ctx, before)
                    if cached is not None:
                        cached.after_cook(chunk, ctx)
                chunk._cook_stale = False
                chunk_utils.stamp_module_hash(stage, chunk)
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
            if skip_cook.enabled():
                skip_cook.unaccounted(chunk)
            chunk._cook_stale = False
            chunk_utils.stamp_module_hash(stage, chunk)
    return True


def execute_roots(chunks) -> None:
    """Call execute() on every root node (no dependencies), recursing into groups."""
    for c in chunks:
        sub = [p for p in c.get_my_params() if isinstance(p, chunk_core.ChunkBase)]
        if sub:
            execute_roots(sub)
        elif not c.get_dependencies():
            c.execute()
