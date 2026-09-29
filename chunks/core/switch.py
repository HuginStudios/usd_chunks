"""Switch -- one of several upstream branches reaches the output.

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

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk_stage

logger = logging.getLogger(__name__)



def execute(node):
    inputs = chunk_stage.exec_dependencies(node)
    if not inputs:
        return

    index = int(node.get_param("index").value or 0)
    index = max(0, min(index, len(inputs) - 1))

    # The muting itself is chunk_stage.gate_to_branches, shared with the Stage Expression
    # Switch: the two differ only in how the live branch is chosen, and keeping two copies
    # of the suppression meant fixing the same bug twice.
    _, muted = chunk_stage.gate_to_branches(node, {index})

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
        chunk_stage.GATE_RECORD_PARAM,
        "Muted",
        tooltip="Bookkeeping: the chunks this Switch has muted, so one that stops being "
        "upstream is un-muted rather than left blocked.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)
    return chunk_api


def config():
    return chunk.DefaultConfig(
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
        dynamic_exec_inputs=True,
    )
