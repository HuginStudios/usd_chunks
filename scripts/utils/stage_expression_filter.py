"""Stage Expression Filter -- a branch that is only in the scene when the pattern matches.

Two inputs, and they mean different things. Slot 0 is the stream, and it is forwarded
untouched whatever the test says. Slot 1 is the branch being gated: it contributes when the
variable matches one of the patterns, and is suppressed when it does not.

    stream  ──┐
              ├── Filter  SHOT ~ "sh01* sh02*"  ──▶  downstream
    branch  ──┘   match   : stream + branch
                  no match: stream only, branch muted

The variable is picked from the ones declared on the stage, by name -- not typed as a
${SHOT} reference. USD substitutes expression variables into asset paths and variant
selections and nowhere else, so a field that appeared to accept one anywhere would be
usd_chunks quietly inventing a rule OpenUSD does not have. This chunk reads the variable
off the stage itself, which is a mechanism of ours and says so.

A Switch picks one of N interchangeable branches by index. This picks by *fact*: the answer
comes from the stage, so one network can carry work that only happens on some shots, some
asset variants, some render layers -- and cooking it under a different Stage Expression
produces a different scene without anything being rewired. Wire a Wedge into the Stage
Expression it reads and a sweep walks those configurations on its own.

Suppression is the same lever the Switch pulls, for the same reasons: a **block** plus a
layer mute, not the DISABLED state, which belongs to the user. A branch contributes because
its layers are in the stage, not because it is wired -- so cutting the connection instead
would leave its prims composed exactly as before and gate nothing.

Patterns are globs, space-separated: `sh01* sh02*` matches either. Case-sensitive on every
platform (fnmatchcase, not fnmatch, which folds case on Windows and not on Linux -- the same
scene would otherwise gate differently depending on which machine cooked it).

**Known limit:** a layer is muted stage-wide, so a gated branch that also feeds a consumer
*outside* this filter is suppressed for that consumer too. Inherent to layer muting -- the
Switch and the view system both have it.

Requires: usd_chunks.
"""

from __future__ import annotations

import fnmatch
import logging

from usd_chunks import chunk, chunk_registry, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags
from usd_chunks import chunk_stage

logger = logging.getLogger(__name__)

#: Where the filter records what it muted, so a branch that stops being upstream is
#: un-muted rather than left blocked forever. Hidden: bookkeeping, not a control.
_MUTED_PARAM = "muted"

#: The variable this filter tests, chosen from what the stage declares.
VARIABLE_PARAM = "variable"
PATTERN_PARAM = "pattern"

#: Offered when the stage declares nothing yet, so the menu is never empty (an empty Menu
#: has no value at all, and the field would read as broken rather than as unset).
NO_VARIABLES = "<none>"


def stage_variable_names(stage) -> list:
    """Every expression variable in effect on the stage, the studio's included.

    Read off the composed stage rather than from the Stage Expression chunks, so a variable
    authored in the file the user opened is offered exactly like one declared here.
    """
    return sorted(chunk_utils.stage_expression_variables(stage))


def matches(value: str, patterns: str) -> bool:
    """Whether value matches any of the space-separated glob patterns.

    fnmatchcase, not fnmatch: fnmatch folds case on Windows and not on Linux, so the same
    scene would gate differently depending on which machine cooked it. No patterns never
    matches, so a half-built filter keeps its branch out rather than letting it in.
    """
    return any(fnmatch.fnmatchcase(value, p) for p in (patterns or "").split() if p)


def _record(node, chunks) -> None:
    """Record what was muted, by PRIM PATH -- a name is unique only within one context, so
    two chunks in different chunkgroups may legitimately share one."""
    param = node.get_param(_MUTED_PARAM)
    if param is not None:
        param.value = "\n".join(sorted(chunk_stage.get_chunk_prim_path(c) for c in chunks))


def _recorded(node) -> set:
    param = node.get_param(_MUTED_PARAM)
    return {line.strip() for line in (param.value or "").splitlines() if line.strip()} if param else set()


def execute(node):
    if edit_layer := node.get_edit_context():
        # Like a Merge or a Switch, this authors nothing of its own -- but a chunk retyped
        # into one must not keep contributing whatever it authored before.
        edit_layer.Clear()

    stage = node.get_stage()
    inputs = chunk_stage.exec_dependencies(node)

    _sync_variable_choices(node)
    name = node.get_param(VARIABLE_PARAM).value or ""
    variables = chunk_utils.stage_expression_variables(stage)
    # str() so a variable of any type the expression syntax allows can be gated on: an int
    # IDENTIFIER against "32*" reads the way anyone typing that pattern expects, and a
    # string[] renderPassList matches on its elements rather than on Python's repr.
    if isinstance(variables.get(name), (list, tuple)):
        value = " ".join(str(v) for v in variables[name])
        passed = any(matches(str(v), node.get_param(PATTERN_PARAM).value) for v in variables[name])
    else:
        value = str(variables.get(name, ""))
        passed = name in variables and matches(value, node.get_param(PATTERN_PARAM).value)

    # Slot 1 is the gated branch. With nothing wired there this is a passthrough, which is
    # the right behaviour for a filter still being built rather than an error to report.
    gated = inputs[1:]

    # The forwarded stream first, and it wins: a chunk feeding both slots is part of the
    # stream, so it must stay live even when the gate is shut. Without this a shared
    # upstream would be muted along with the branch and take the stream down with it.
    keep = set()
    for dep in inputs[:1]:
        keep |= chunk_stage.exec_upstream(dep)

    muted = set()
    if not passed:
        for dep in gated:
            for c in chunk_stage.exec_upstream(dep):
                if c in keep:
                    continue
                if chunk_stage.set_chunk_muted(stage, c, True):
                    muted.add(c)
    else:
        for dep in gated:
            keep |= chunk_stage.exec_upstream(dep)

    # Only what this filter muted itself. The stream can carry another gate's suppressed
    # branch -- two filters in a row, day then night -- and un-muting everything reachable
    # switched the upstream filter's branch straight back on.
    mine = _recorded(node)
    for c in keep:
        if chunk_stage.get_chunk_prim_path(c) in mine:
            chunk_stage.set_chunk_muted(stage, c, False)

    # Muted by an earlier evaluation and no longer upstream of this filter at all: leaving
    # it blocked would strand a branch the user has already disconnected.
    # Resolved against the whole scene, by prim path. The record is transitive -- muting a
    # branch means the whole branch -- so most of what it holds was never a direct input of
    # this filter, and searching only `inputs` found nothing beyond a one-chunk branch:
    # everything deeper stayed blocked and muted for the rest of the session with no way
    # back, which is the exact case this record exists to prevent. By path rather than
    # name, since a name is unique only within one context.
    stranded = _recorded(node) - {
        chunk_stage.get_chunk_prim_path(c) for c in muted | keep
    }
    if stranded:
        by_path = {
            chunk_stage.get_chunk_prim_path(c): c
            for c in chunk_registry.ChunkRegistry().scene.get_chunks()
        }
        for path in stranded:
            if (c := by_path.get(path)) is not None:
                chunk_stage.set_chunk_muted(stage, c, False)

    _record(node, muted)
    verdict = "passes" if passed else "blocked"
    logger.debug(
        "%s=%r ~ %r -> %s%s", name, value, node.get_param(PATTERN_PARAM).value, verdict,
        f", muted {len(muted)} chunk(s)" if muted else "",
    )


def _sync_variable_choices(node) -> None:
    """Offer exactly the variables the stage declares, keeping the current choice.

    The menu is rebuilt on every cook rather than once at creation: a Stage Expression
    upstream is usually edited after the filter exists, and a dropdown that only ever
    listed what was declared the moment this node was made would be permanently empty.
    A name that has since gone is kept in the list while it is the current selection, so
    the field shows what it is still gating on instead of silently moving to another
    variable.
    """
    param = node.get_param(VARIABLE_PARAM)
    if param is None:
        return
    names = stage_variable_names(node.get_stage())
    current = param.value
    if current and current not in names and current != NO_VARIABLES:
        names.append(current)
    items = names or [NO_VARIABLES]
    if list(param.menu_items) != items:
        param.menu_items = items


def _chunk_loaded(owner, _notice, _sender):
    _sync_variable_choices(owner)


def main():
    chunk_api = chunk.Chunk("StageExpressionFilter")
    chunk_api.add_params([
        chunk.Menu(
            VARIABLE_PARAM, "Variable",
            menu_items=[NO_VARIABLES],
            # The list is rebuilt from the stage, so a saved choice must survive being
            # restored before that has happened.
            dynamic_items=True,
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Which stage expression variable to test, from those declared on the "
                    "stage -- by a Stage Expression chunk upstream or by the file that was "
                    "opened.",
        ),
        chunk.LineEdit(
            PATTERN_PARAM, "Pattern",
            flags=ChunkFlags.ParamFlags.LABELED,
            tooltip="Glob patterns the variable must match for the gated branch to "
                    "contribute, space-separated: `sh01* sh02*` matches either. Empty "
                    "never matches, so the branch stays out until this is filled in.",
        ),
    ])
    muted = chunk.TextEdit(
        _MUTED_PARAM, "Muted",
        tooltip="Bookkeeping: the chunks this filter has muted, so one that stops being "
                "upstream is un-muted rather than left blocked.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)
    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        # Two positional exec inputs: the top one is forwarded always, the bottom one is
        # gated. Positional, so they get a port each rather than stacking on one.
        exec_inputs=2,
        description="Gates a branch on what the stage says. The top input is forwarded "
        "whatever happens; the bottom one contributes only when the chosen variable matches "
        "one of the patterns, and is muted -- the whole branch, not just the chunk in the "
        "slot -- when it does not.\n"
        "Variable lists what the stage declares, so one network can carry work that only "
        "happens on some shots or some asset variants, and cooking it against a different "
        "Stage Expression produces a different scene with nothing rewired.\n"
        "Patterns are globs, space-separated: `sh01* sh02*` matches either.\n"
        "A chunk feeding both inputs stays live: the forwarded stream wins.\n"
        "Muting is stage-wide, so a gated branch that also feeds something outside this "
        "filter is suppressed for that consumer too.",
    )
