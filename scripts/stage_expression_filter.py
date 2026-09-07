"""Stage Expression Filter -- a branch that is only in the scene when the pattern matches.

Two inputs, and they mean different things. Slot 0 is the stream, and it is forwarded
untouched whatever the test says. Slot 1 is the branch being gated: it contributes when the
pattern matches the value, and is suppressed when it does not.

    stream  ──┐
              ├── Filter  ${SHOT} ~ "sh01*"  ──▶  downstream
    branch  ──┘   match   : stream + branch
                  no match: stream only, branch muted

A Switch picks one of N interchangeable branches by index. This picks by *fact*: the answer
comes from the stage, so one network can carry work that only happens on some shots, some
asset variants, some render layers -- and cooking it under a different Stage Expression
produces a different scene without anything being rewired. Wire a Wedge into the Stage
Expression it reads and a sweep walks those configurations on its own.

Suppression is the same lever the Switch pulls, for the same reasons: a **block** plus a
layer mute, not the DISABLED state, which belongs to the user. A branch contributes because
its layers are in the stage, not because it is wired -- so cutting the connection instead
would leave its prims composed exactly as before and gate nothing.

**Known limit:** a layer is muted stage-wide, so a gated branch that also feeds a consumer
*outside* this filter is suppressed for that consumer too. Inherent to layer muting -- the
Switch and the view system both have it.

Requires: usd_chunks.
"""

from __future__ import annotations

import fnmatch
import re

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks.plugins import chunk_usd

#: Where the filter records what it muted, so a branch that stops being upstream is
#: un-muted rather than left blocked forever. Hidden: bookkeeping, not a control.
_MUTED_PARAM = "muted"

GLOB = "Glob"
REGEX = "Regex"
EQUALS = "Equals"

_MODES = (GLOB, REGEX, EQUALS)


def _matches(value: str, pattern: str, mode: str) -> bool:
    """Whether value passes pattern under mode.

    Glob is the default because it is what a shot or asset name is usually tested with --
    `sh01*`, `*_hero`, `bg_??` -- and because it is the same vocabulary the prim-path
    fields already use, so one syntax covers both. Regex is there for the cases glob cannot
    reach (alternation, anchoring), and a bad pattern reports itself rather than silently
    never matching, which is indistinguishable from a variable being wrong.

    fnmatchcase, not fnmatch: fnmatch folds case on Windows and not on Linux, so the same
    file would gate differently depending on which machine cooked it.
    """
    if mode == EQUALS:
        return value == pattern
    if mode == REGEX:
        try:
            return re.search(pattern, value) is not None
        except re.error as exc:
            print(f"[Stage Expression Filter] {pattern!r} is not a valid regex: {exc}")
            return False
    return fnmatch.fnmatchcase(value, pattern)


def _record(node, chunks) -> None:
    """Record what was muted, by PRIM PATH -- a name is unique only within one context, so
    two chunks in different chunkgroups may legitimately share one."""
    param = node.get_param(_MUTED_PARAM)
    if param is not None:
        param.value = "\n".join(sorted(chunk_usd.get_chunk_prim_path(c) for c in chunks))


def _recorded(node) -> set:
    param = node.get_param(_MUTED_PARAM)
    return {line.strip() for line in (param.value or "").splitlines() if line.strip()} if param else set()


def execute(node):
    if edit_layer := node.get_edit_context():
        # Like a Merge or a Switch, this authors nothing of its own -- but a chunk retyped
        # into one must not keep contributing whatever it authored before.
        edit_layer.Clear()

    stage = node.get_stage()
    inputs = chunk_usd.exec_dependencies(node)

    # The value read is a param, so it has already been through the whole resolution chain:
    # a ${SHOT} reference, a `${SHOT}` expression, a @Node.param@ wire, or a literal typed
    # in. Reading it here rather than looking the variable up directly is what lets the
    # filter test something that is not a stage variable at all.
    value = str(node.get_param("value").value or "")
    pattern = str(node.get_param("pattern").value or "")
    mode = node.get_param("mode").value or GLOB
    passed = bool(pattern) and _matches(value, pattern, mode)

    # Slot 1 is the gated branch. With nothing wired there this is a passthrough, which is
    # the right behaviour for a filter still being built rather than an error to report.
    gated = inputs[1:]

    # The forwarded stream first, and it wins: a chunk feeding both slots is part of the
    # stream, so it must stay live even when the gate is shut. Without this a shared
    # upstream would be muted along with the branch and take the stream down with it.
    keep = set()
    for dep in inputs[:1]:
        keep |= chunk_usd.upstream_chunks(dep)

    muted = set()
    if not passed:
        for dep in gated:
            for c in chunk_usd.upstream_chunks(dep):
                if c in keep:
                    continue
                if chunk_usd.set_chunk_muted(stage, c, True):
                    muted.add(c)
    else:
        for dep in gated:
            keep |= chunk_usd.upstream_chunks(dep)

    for c in keep:
        chunk_usd.set_chunk_muted(stage, c, False)

    # Muted by an earlier evaluation and no longer upstream of this filter at all: leaving
    # it blocked would strand a branch the user has already disconnected.
    # Resolved against the whole scene, by prim path. The record is transitive -- muting a
    # branch means the whole branch -- so most of what it holds was never a direct input of
    # this filter, and searching only `inputs` found nothing beyond a one-chunk branch:
    # everything deeper stayed blocked and muted for the rest of the session with no way
    # back, which is the exact case this record exists to prevent. By path rather than
    # name, since a name is unique only within one context.
    stranded = _recorded(node) - {
        chunk_usd.get_chunk_prim_path(c) for c in muted | keep
    }
    if stranded:
        by_path = {
            chunk_usd.get_chunk_prim_path(c): c
            for c in chunk_registry.ChunkRegistry().scene.get_chunks()
        }
        for path in stranded:
            if (c := by_path.get(path)) is not None:
                chunk_usd.set_chunk_muted(stage, c, False)

    _record(node, muted)
    verdict = "passes" if passed else "blocked"
    print(f"[Stage Expression Filter] {value!r} {mode.lower()} {pattern!r} -> {verdict}"
          + (f", muted {len(muted)} chunk(s)" if muted else ""))


def main():
    chunk_api = chunk.Chunk("StageExpressionFilter")
    chunk_api.add_params([
        chunk.LineEdit(
            "value", "Value",
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="What to test. Usually a reference to a stage variable -- type it the "
                    "way you would in any other field -- but any resolved value works, "
                    "including one arriving through a wire.",
        ),
        chunk.LineEdit(
            "pattern", "Pattern",
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="The pattern Value must match for the gated branch to contribute. An "
                    "empty pattern never matches, so the branch stays out until this is "
                    "filled in.",
        ),
        chunk.Menu(
            "mode", "Match",
            default_value=GLOB, menu_items=list(_MODES),
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Glob (* ? [seq], case-sensitive on every platform), a regular "
                    "expression searched anywhere in the value, or an exact comparison.",
        ),
    ])
    muted = chunk.TextEdit(
        _MUTED_PARAM, "Muted",
        tooltip="Bookkeeping: the chunks this filter has muted, so one that stops being "
                "upstream is un-muted rather than left blocked.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        # Two positional exec inputs: the top one is forwarded always, the bottom one is
        # gated. Positional, so they get a port each rather than stacking on one.
        exec_inputs=2,
        description="Gates a branch on what the stage says. The top input is forwarded "
        "whatever happens; the bottom one contributes only when Value matches Pattern, and "
        "is muted -- the whole branch, not just the chunk in the slot -- when it does not.\n"
        "Value is usually a stage variable reference, so one network can carry work that "
        "only happens on some shots or some asset variants, and cooking it against a "
        "different Stage Expression produces a different scene with nothing rewired.\n"
        "A chunk feeding both inputs stays live: the forwarded stream wins.\n"
        "Muting is stage-wide, so a gated branch that also feeds something outside this "
        "filter is suppressed for that consumer too.",
    )
