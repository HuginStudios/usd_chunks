"""Stage Expression -- declares the USD expression variables a scene composes against.

USD already has this mechanism: a layer carries an `expressionVariables` dict, and
composition substitutes ${NAME} out of it. A studio that has been shipping shot-driven
asset paths has those variables authored in the files it opens, and this chunk is built to
sit on top of what is already there rather than to replace it -- it reads the composed set,
adds to it, and leaves everything it did not author alone.

    Stage Expression
      SHOT            string     sh010
      USE_LOW_POLY    bool       false
      IDENTIFIER      int        3254
      renderPassList  string[]   foreground, background, FX

**Where a variable applies is USD's rule, not ours.** Composition substitutes ${NAME} into
**asset paths and variant selections**, and nowhere else:

    references = @`if(${USE_LOW_POLY}, "assetsLow/hero.usda", "assets/hero.usda")`@

A chunk param is not one of those places, so usd_chunks does not resolve ${NAME} in one --
a Prim Path field holding one keeps the text, literally. Acting on a variable anywhere else
in a chunk graph is the Stage Expression Filter's job: that is a usd_chunks mechanism,
honestly named, rather than an extension of USD's that would appear to work in fields where
OpenUSD says it does not.

Variables are authored into the **session layer**, which is where usd_chunks already keeps
its bookkeeping. Setting one therefore does not dirty the file the user opened, and because
a session opinion is stronger than the root layer's, a variable set here overrides a
studio-authored default of the same name rather than colliding with it.

**A variable is stage-global; recooking is not.** Any chunk anywhere can be gated on
${SHOT}, but only chunks downstream of this one re-execute when it changes. Wire this
upstream of what should react to it.

**Known limit:** changing a variable recomposes the stage live, but introducing one that
did not exist when the stage was opened does not -- Pcp records no dependency on a variable
whose lookup failed, so a reference that was already unresolved stays unresolved until the
stage is reopened.

Requires: usd_chunks.
"""

from __future__ import annotations

import logging

from usd_chunks import chunk, chunk_utils

logger = logging.getLogger(__name__)

#: The list of declared variables, and the columns of one row.
ROWS_PARAM = "variables"
NAME_COLUMN = "var_name"
TYPE_COLUMN = "var_type"
VALUE_COLUMN = "var_value"

#: Where the chunk records which variable names it authored last time, so a name deleted
#: from the list is removed from the stage rather than left behind forever. Hidden:
#: bookkeeping, not a control -- the same shape as the Switch's `muted` record.
_OWNED_PARAM = "owned"

STRING = "string"
INT = "int"
BOOL = "bool"
STRING_LIST = "string[]"

#: What a USD expression variable can hold. Sdf.VariableExpression evaluates strings, whole
#: numbers, booleans and lists of those -- there is deliberately no float type.
TYPES = (STRING, INT, BOOL, STRING_LIST)

_TRUE = {"true", "yes", "on", "1"}


def typed_value(value: str, type_name: str):
    """One row's text as the type it declares, or None if it cannot be read as one.

    A bad number is skipped with a warning rather than authored as its text: a variable
    that silently changed type would make `if(${N}, ...)` behave differently from what the
    row plainly says, which is much harder to spot than a log line.
    """
    text = (value or "").strip()
    if type_name == INT:
        try:
            return int(text)
        except ValueError:
            logger.warning("%r is not a whole number", text)
            return None
    if type_name == BOOL:
        return text.lower() in _TRUE
    if type_name == STRING_LIST:
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def declared_variables(node):
    """(name, typed value) for every usable row, in list order."""
    group = node.get_param(ROWS_PARAM, chunk.is_inst(chunk.ListGroup))
    if group is None:
        return []
    rows = []
    for i in range(group.size):
        name = (getattr(group.get_indexed_param(NAME_COLUMN, i), "value", "") or "").strip()
        if not name:
            continue
        if not name.isidentifier():
            logger.warning("%r is not a usable variable name, skipping", name)
            continue
        type_name = getattr(group.get_indexed_param(TYPE_COLUMN, i), "value", STRING) or STRING
        value = typed_value(getattr(group.get_indexed_param(VALUE_COLUMN, i), "value", ""), type_name)
        if value is None:
            continue
        rows.append((name, value))
    return rows


def _recorded(node) -> set:
    param = node.get_param(_OWNED_PARAM)
    return {n.strip() for n in (param.value or "").split(",") if n.strip()} if param else set()


def _record(node, names) -> None:
    param = node.get_param(_OWNED_PARAM)
    if param is not None:
        param.value = ",".join(sorted(names))


def execute(node):
    stage = node.get_stage()
    mine = dict(declared_variables(node))

    # Start from what is already in the session layer rather than from scratch, so two
    # Stage Expression chunks do not erase each other -- and so a variable some other part
    # of usd_chunks put there survives. Root-layer variables are never touched: they are
    # the studio's, they compose underneath this, and a name defined in both resolves to
    # the session opinion (this one) without either being lost.
    session = stage.GetSessionLayer()
    current = dict(session.expressionVariables or {})

    # Names this chunk authored last time and no longer declares. Dropping them by name --
    # rather than by rewriting the whole dict from `mine` -- is what keeps another chunk's
    # variables intact while still letting a deleted row actually delete something.
    for stale in _recorded(node) - set(mine):
        current.pop(stale, None)

    current.update(mine)
    chunk_utils.set_stage_expression_variables(stage, current)
    _record(node, mine)

    if mine:
        logger.debug("%d variable(s): %s", len(mine), ", ".join(sorted(mine)))
    else:
        logger.debug("no variables declared")


def _rows_changed(owner, notice, _sender):
    """Re-author on any edit to the list -- a value typed, a row added or removed.

    ATTRIBUTE_CHANGED on the whole chunk rather than a per-param callback, for the reason
    the Sublayer chunk documents: removing a row renames the list's internal params, which
    makes a per-param callback fire twice on a state that is only half updated.
    """
    if notice.data.name not in ("size", "value"):
        return
    execute(owner)


def main():
    chunk_api = chunk.Chunk("StageExpression")

    rows = chunk.ListGroup(ROWS_PARAM, "Variables")
    rows.add_param(chunk.LineEdit(
        NAME_COLUMN, "Name",
        tooltip="The variable name. Spell it ${NAME} in an asset path or a variant "
                "selection -- the two places USD substitutes -- or gate a branch on it "
                "with a Stage Expression Filter.",
    ))
    rows.add_param(chunk.Menu(
        TYPE_COLUMN, "Type",
        default_value=STRING, menu_items=list(TYPES),
        tooltip="USD expression variables hold strings, whole numbers, booleans or lists "
                "of those. There is no float type.",
    ))
    rows.add_param(chunk.LineEdit(
        VALUE_COLUMN, "Value",
        tooltip="The value. A list is comma-separated; a boolean is true or false.",
    ))
    chunk_api.add_param(rows)

    owned = chunk.LineEdit(
        _OWNED_PARAM, "Owned",
        tooltip="Bookkeeping: the variable names this chunk authored, so one deleted from "
                "the list is removed from the stage rather than left behind.",
    )
    owned.set_state(chunk.ChunkFlags.State.HIDDEN)
    chunk_api.add_param(owned)

    chunk_api.add_callback(_rows_changed, notice_alias=chunk.ChunkNoticeAlias.ATTRIBUTE_CHANGED)
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Declares USD stage expression variables -- a name, a type and a value per row.\n"
            "USD substitutes ${NAME} into asset paths and variant selections and nowhere "
            "else, so `if(${USE_LOW_POLY}, \"low/hero.usda\", \"hero.usda\")` in a file "
            "field is evaluated by composition itself. A chunk param is not one of those "
            "places and keeps the text literally -- gate on the variable with a Stage "
            "Expression Filter instead.\n"
            "Variables the opened file already declares are added to rather than replaced; "
            "a name set here overrides a studio default of the same name via the session "
            "layer, leaving their file untouched.\n"
            "A variable is visible stage-wide, but only chunks downstream of this one "
            "re-execute when it changes -- wire this upstream of what should react."
        ),
    )
