"""Switch — one of several upstream branches reaches the output.

A Merge is fan-in: every branch wired into it contributes, and slot order decides who wins.
A Switch is the choice instead of the sum: the branch in the selected slot contributes and
the others are suppressed.

    Branch A ──┐
    Branch B ──┼── Switch (index 1) ──▶   only B reaches downstream
    Branch C ──┘

Like Merge, this authors nothing of its own. What it does is mute the branches it did not
pick, by putting their chunks into the DISABLED state -- which is already what mutes a
chunk's layer, so the suppressed branches simply stop contributing opinions to the composed
stage. They also grey out in the node graph, which is the feedback you want: you can see
which branch is live.

Requires: usd_chunks.
"""

from __future__ import annotations

from usd_chunks import chunk
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd

#: Where the switch records what it muted, so it never re-enables a chunk the user
#: disabled by hand. Hidden because it is bookkeeping, not a control.
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


def _record(node, chunks) -> None:
    param = node.get_param(_MUTED_PARAM)
    if param is not None:
        param.value = "\n".join(sorted(c.name for c in chunks))


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

    previously = _recorded(node)
    muted = set()
    for slot, dep in enumerate(inputs):
        if slot == index:
            continue
        for c in _upstream(dep):
            if c in keep:
                continue
            c.set_state(ChunkFlags.State.DISABLED)
            muted.add(c)

    # Only un-mute what this switch muted. Without the record, flipping the index would
    # clear a DISABLED a user had set by hand on a chunk that happens to sit in the branch
    # being selected -- silently undoing their intent.
    for c in keep:
        if c.name in previously and c.name not in {m.name for m in muted}:
            c.unset_state(ChunkFlags.State.DISABLED)

    _record(node, muted)
    print(f"[Switch] input {index} of {len(inputs)} live"
          + (f", muted {len(muted)} chunk(s)" if muted else ""))


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
            "connected slot rather than muting everything.",
        )
    )
    muted = chunk.TextEdit(
        _MUTED_PARAM,
        "Muted",
        tooltip="Bookkeeping: the chunks this Switch muted, so flipping the input never "
        "re-enables one you disabled yourself.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description="Picks one upstream branch. Where a Merge sums every branch wired into "
        "it, a Switch passes the one in the selected slot and mutes the rest -- the whole "
        "branch, not just the chunk in the slot. A chunk feeding both the chosen branch and "
        "a muted one stays live.\n"
        "Wire it to a Wedge's output to sweep configurations that differ in structure "
        "rather than in value.",
    )
