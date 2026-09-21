"""Stage Expression Switch -- the normal path, or the matched one.

Two inputs and two paths, the way Solaris does it: if the variable matches the pattern the
scene goes through the **matched** path, and if it does not it goes through the **normal**
one. Exactly one of them is ever live -- this is a Switch, not a merge, and the choice is a
fact read off the stage rather than an index typed into a field.

    Sun -- SunAim -- Sky_day ------┐ normal
                                   ├── ShotSwitch -- Camera -- View
    Ceiling -- Lamp -- Sky_night --┘ matched: SHOT ~ "*_night"

            SHOT = sh030_night  ->  matched path live, normal path muted
            SHOT = sh010_day    ->  normal path live, matched path muted

So the graph stays one thread with a fork that rejoins, rather than a branch hanging off
the side of the chain -- and the work above the fork is shared: a chunk feeding both paths
stays live whichever one is taken.

The live path is found from the **view** upward rather than from the top down: the engine
cooks what the active view pulls on (chunk_stage.recook_for_view), so which of the two is
live is decided where they meet, and a path nothing downstream asks for costs nothing.

**Why a variable and not a ${SHOT} typed into a field.** USD substitutes expression
variables into asset paths and variant selections and nowhere else, so a field accepting
one anywhere would be usd_chunks inventing a rule OpenUSD does not have. This chunk reads
the variable off the stage by name, which is a mechanism of ours and says so.

Patterns are globs, space-separated: `sh01* sh02*` matches either. An empty pattern never
matches, so a half-built switch runs the normal path rather than an arbitrary one. More
than two cases is more than one switch, chained -- each one still reading as two paths.

Suppression is the Switch's lever, for the same reasons: a **block** plus a layer mute, not
the DISABLED state, which belongs to the user.

**Known limit:** a layer is muted stage-wide, so a suppressed branch that also feeds a
consumer *outside* this switch is suppressed for that consumer too. Inherent to layer
muting -- the Switch and the view system both have it.

Requires: usd_chunks.
"""

from __future__ import annotations

import fnmatch
import logging

from usd_chunks import chunk, chunk_stage, chunk_utils
from usd_chunks.chunk_flags import ChunkFlags

logger = logging.getLogger(__name__)

#: The variable this switch reads, chosen from what the stage declares.
VARIABLE_PARAM = "variable"

#: The globs the variable is tested against to take the matched path.
PATTERN_PARAM = "pattern"

#: Input 0 is the normal path, input 1 the matched one. Positional, so each gets its own
#: port: unlike a Switch's interchangeable inputs, these two mean different things.
NORMAL_INPUT = 0
MATCHED_INPUT = 1

#: Offered when the stage declares nothing yet, so the menu is never empty (an empty Menu
#: has no value at all, and the field reads as broken rather than as unset).
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
    scene would gate differently depending on which machine cooked it.
    """
    return any(fnmatch.fnmatchcase(value, p) for p in (patterns or "").split() if p)


def _values(node) -> list:
    """The variable's value as the list of strings to match against.

    A string[] variable matches on its elements, so `renderPassList` against "FX" reads the
    way anyone typing that pattern expects; everything else is a single value, str() so an
    int IDENTIFIER can be matched with "32*" like any other text.
    """
    name = node.get_param(VARIABLE_PARAM).value or ""
    variables = chunk_utils.stage_expression_variables(node.get_stage())
    if name not in variables:
        return []
    value = variables[name]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def chosen_input(node) -> int:
    """MATCHED_INPUT when the variable matches the pattern, NORMAL_INPUT when it does not.

    A variable that is not declared at all does not match, so the scene keeps running its
    normal path rather than dropping into a branch meant for a shot nobody selected.
    """
    pattern = node.get_param(PATTERN_PARAM).value
    if any(matches(v, pattern) for v in _values(node)):
        return MATCHED_INPUT
    return NORMAL_INPUT


def _sync_variable_choices(node) -> None:
    """Offer exactly the variables the stage declares, keeping the current choice.

    Rebuilt on every cook rather than once at creation: a Stage Expression upstream is
    usually edited after this node exists, so a dropdown listing only what was declared the
    moment it was made would be permanently empty. A name that has since gone stays in the
    list while it is selected, so the field shows what it is still switching on instead of
    silently moving to another variable.
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


def execute(node):
    if edit_layer := node.get_edit_context():
        # Like a Merge or a Switch, this authors nothing of its own -- but a chunk retyped
        # into one must not keep contributing whatever it authored before.
        edit_layer.Clear()

    _sync_variable_choices(node)
    index = chosen_input(node)
    _, muted = chunk_stage.gate_to_branches(node, {index})

    logger.debug(
        "%s -> %s path%s",
        node.get_param(VARIABLE_PARAM).value,
        "matched" if index == MATCHED_INPUT else "normal",
        f", muted {len(muted)} chunk(s)" if muted else "",
    )


def _chunk_loaded(owner, _notice, _sender):
    _sync_variable_choices(owner)


def main():
    chunk_api = chunk.Chunk("StageExpressionSwitch")
    chunk_api.add_param(chunk.Menu(
        VARIABLE_PARAM, "Variable",
        menu_items=[NO_VARIABLES],
        # The list is rebuilt from the stage, so a saved choice must survive being restored
        # before that has happened.
        dynamic_items=True,
        flags=ChunkFlags.ParamFlags.LABELED,
        tooltip="Which stage expression variable decides. Declared by a Stage Expression "
                "chunk upstream, or by the file that was opened.",
    ))

    chunk_api.add_param(chunk.LineEdit(
        PATTERN_PARAM, "Pattern",
        flags=ChunkFlags.ParamFlags.LABELED,
        tooltip="Glob patterns that send the scene down the matched path (the lower "
                "input), space-separated: `sh01* sh02*` matches either. Anything else "
                "takes the normal path.",
    ))

    muted = chunk.TextEdit(
        chunk_stage.GATE_RECORD_PARAM, "Muted",
        tooltip="Bookkeeping: the chunks this switch has muted, so one that stops being "
                "upstream is un-muted rather than left blocked.",
    )
    muted.set_state(ChunkFlags.State.HIDDEN)
    chunk_api.add_param(muted)

    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk.ChunkNoticeAlias.CHUNK_LOADED)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        # Two positional exec inputs: the top one is the normal path, the bottom one the
        # matched path. Positional, so they get a port each rather than stacking on one --
        # unlike a Switch's interchangeable inputs, these two mean different things.
        exec_inputs=2,
        description=(
            "Two paths, and the stage picks between them: if the chosen expression variable "
            "matches the pattern the scene goes through the matched path (the lower input), "
            "otherwise through the normal one (the upper). Exactly one is ever live -- the "
            "other is muted, the whole branch of it rather than just the chunk in the "
            "slot.\n"
            "So one network can carry work that only happens on some shots or some asset "
            "variants, and cooking it against a different Stage Expression produces a "
            "different scene with nothing rewired.\n"
            "A chunk feeding both paths stays live whichever is taken, so the thread above "
            "the fork is shared.\n"
            "Patterns are globs, space-separated: `sh01* sh02*` matches either. An empty "
            "pattern never matches, so the normal path runs.\n"
            "Muting is stage-wide, so a suppressed path that also feeds something outside "
            "this switch is suppressed for that consumer too."
        ),
    )
