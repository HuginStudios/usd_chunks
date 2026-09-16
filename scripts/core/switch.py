"""Switch — one of several upstream branches reaches the output.

A Merge is fan-in: every branch wired into it contributes, and slot order decides who wins.
A Switch is the choice instead of the sum: the branch in the selected slot contributes and
the others are suppressed.

    Branch A ──┐
    Branch B ──┼── Switch (index 1) ──▶   only B reaches downstream
    Branch C ──┘

Like Merge, this authors nothing of its own.

Suppression is a *mute*, not a disconnect. A branch contributes because its layers are in
the stage, not because it is wired -- wiring decides ordering and graph membership, which
is why Merge authors nothing either. Cutting the connection would leave the branch's prims
composed exactly as before and switch nothing.

The mute is a **block**, the same mechanism the view system uses to hide non-viewed
branches, rather than the DISABLED state. DISABLED belongs to the user: they set it by hand
and expect it to stay. A Switch reaching in to set and clear it on chunks it does not own
means every index flip risks undoing them, and needing bookkeeping to avoid that is a sign
the wrong lever was pulled. Blocking is the engine's own lever, and ChunkLayerManager
already guards the two against each other so a blocked layer is never un-muted by the
disable path.

**Known limit:** a layer is muted stage-wide, so a branch that also feeds a consumer
*outside* this Switch is suppressed for that consumer too. Inherent to layer muting -- the
view system has the same property -- and worth knowing before wiring a shared branch
through one.

Requires: usd_chunks.
"""

from __future__ import annotations

import logging

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd

logger = logging.getLogger(__name__)

#: Where the switch records what it muted, so a branch that stops being upstream is still
#: un-muted rather than left blocked forever. Hidden: bookkeeping, not a control.
_MUTED_PARAM = "muted"


def _upstream(node, seen=None) -> set:
    """`node` and everything upstream of it on the exec chain.

    Transitive, because switching a branch has to suppress the whole branch and not merely
    the chunk plugged into the slot -- the rest of it would carry on authoring into the
    composed stage.

    Guarded against revisiting: a diamond (two branches sharing a source) would otherwise
    walk the shared part once per path, and a cycle would not terminate at all.
    """
    seen = set() if seen is None else seen
    if id(node) in {id(n) for n in seen}:
        return seen
    seen.add(node)
    for dep in chunk_usd.exec_dependencies(node):
        _upstream(dep, seen)
    return seen


def _set_muted(node, target, muted: bool) -> bool:
    """Block or unblock one chunk and mute or unmute its layer.

    Never a context (a tab or group): is_blocked() climbs the owner chain, so blocking a
    container would propagate down and block its whole subtree, including the branch that
    was selected. Containers author nothing of their own -- their leaves are handled
    individually by the caller's loop, exactly as recook_for_view does it.
    """
    if target.has_flag(ChunkFlags.GroupFlags.CONTEXT):
        return False

    target.set_blocked(muted)
    stage = node.get_stage()
    layer = chunk_registry.ChunkLayerManager().get_layer(target)
    if stage is None or layer is None:
        return True
    # stage.Mute/UnmuteLayer, not layer.SetMuted: SetMuted recomposes lazily and expires
    # prims, which is the "chunk layers disappear" bug the rest of the codebase avoids the
    # same way.
    if muted:
        stage.MuteLayer(layer.identifier)
    else:
        stage.UnmuteLayer(layer.identifier)
    return True


def _record(node, chunks) -> None:
    """Record what was muted, by PRIM PATH.

    Not by name: a name is only unique within one context, so two chunks in different
    chunkgroups may legitimately share one, and releasing "the chunk called b" would pick
    whichever of them came first.
    """
    param = node.get_param(_MUTED_PARAM)
    if param is not None:
        param.value = "\n".join(sorted(chunk_usd.get_chunk_prim_path(c) for c in chunks))


def _recorded(node) -> set:
    param = node.get_param(_MUTED_PARAM)
    return {line.strip() for line in (param.value or "").splitlines() if line.strip()} if param else set()


def execute(node):
    if edit_layer := node.get_edit_context():
        # Same as Merge: a Switch has no result of its own, but a chunk retyped into one
        # must not keep contributing whatever it authored before.
        edit_layer.Clear()

    inputs = chunk_usd.exec_dependencies(node)
    if not inputs:
        return

    index = int(node.get_param("index").value or 0)
    index = max(0, min(index, len(inputs) - 1))

    # The selected branch first: a chunk feeding two branches must stay live if either of
    # the ones it feeds is the chosen one, so `keep` wins over every suppression below.
    keep = _upstream(inputs[index])

    muted = set()
    for slot, dep in enumerate(inputs):
        if slot == index:
            continue
        for c in _upstream(dep):
            if c in keep:
                continue
            if _set_muted(node, c, True):
                muted.add(c)

    # Un-mute the chosen branch. Blocking is this Switch's own lever rather than the user's
    # DISABLED, so there is nothing of theirs to clobber -- but only chunks still reachable
    # are un-muted, and the record below catches a branch that has since been unwired.
    for c in keep:
        _set_muted(node, c, False)

    # Muted by an earlier index and no longer upstream of this Switch at all: leaving it
    # blocked would strand a branch the user has already disconnected.
    #
    # Resolved against the whole scene, not against `inputs`. The record is transitive --
    # that is the point of it -- so most of what it holds was never a direct input of this
    # Switch, and searching only the slots found nothing beyond a one-chunk branch.
    # Everything deeper stayed blocked and muted for the rest of the session with no way
    # back, which is the exact case this record exists to prevent.
    stranded = _recorded(node) - {chunk_usd.get_chunk_prim_path(c) for c in muted | keep}
    if stranded:
        by_path = {
            chunk_usd.get_chunk_prim_path(c): c
            for c in chunk_registry.ChunkRegistry().scene.get_chunks()
        }
        for path in stranded:
            if (c := by_path.get(path)) is not None:
                _set_muted(node, c, False)

    _record(node, muted)
    logger.debug(
        "input %d of %d live%s", index, len(inputs),
        f", muted {len(muted)} chunk(s)" if muted else "",
    )


def main():
    chunk_api = chunk.Chunk("Switch")
    chunk_api.add_param(
        chunk.Slider(
            "index",
            "Input",
            default_value=0,
            is_int=True,
            min_value=0,
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Which input slot reaches the output. Out-of-range clamps to the last "
            "connected slot rather than muting everything.\n"
            "Drivable by expression -- @Wedge.branch@ picks the branch per variant, so a "
            "sweep can vary structure rather than only values. The value truncates, and a "
            "wedge range is half-open, so use [0, number-of-slots): 0 2 for two inputs, "
            "since 0 1 would floor every draw to slot 0.",
        )
    )
    muted = chunk.TextEdit(
        _MUTED_PARAM,
        "Muted",
        tooltip="Bookkeeping: the chunks this Switch has muted, so one that stops being "
        "upstream is un-muted rather than left blocked.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.CORE,
        description="Picks one upstream branch. Where a Merge sums every branch wired into "
        "it, a Switch passes the one in the selected slot and mutes the rest -- the whole "
        "branch, not just the chunk in the slot. A chunk feeding both the chosen branch and "
        "a muted one stays live.\n"
        "Muting is stage-wide, so a branch that also feeds something outside this Switch is "
        "suppressed for that consumer too.\n"
        "Wire the Input to a Wedge range to sweep configurations that differ in structure "
        "rather than in value. Use [0, number-of-slots) -- the index truncates and a wedge "
        "range is half-open.",
    )
