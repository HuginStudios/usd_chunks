"""Stage Expression -- names a value once, and every field on the stage can spell it.

USD already has this mechanism: a layer carries an `expressionVariables` dict, and
composition substitutes ${NAME} out of it. A studio that has been shipping shot-driven
asset paths has those variables authored in the files it opens, and this chunk is built to
sit on top of what is already there rather than to replace it -- it reads the composed set,
adds to it, and leaves everything it did not author alone.

    Stage Expression
      SHOT   = sh010          o--> (wire a Wedge in to sweep it)
      ASSET  = hero
      ------------------------------------------------------------
      Reference.asset      `"./${ASSET}.usda"`     <- USD substitutes
      Transform.primpath   /World/${SHOT}/geo      <- usd_chunks substitutes

The two lines above resolve by different machinery, and knowing which is which is most of
understanding this chunk. USD substitutes ${NAME} into **asset paths and variant selections
only**. A prim path is not one of them and never will be -- so ${NAME} in a Prim Path field
is expanded by usd_chunks at param-read time instead (chunk_utils.resolve_stage_expression,
wired into every param's value read). Both spellings are accepted everywhere: the
backtick-delimited form USD defines, evaluated by USD's own evaluator so studio syntax
behaves exactly as it does in their files, and a bare ${NAME} anywhere inside a string,
which is the form a path needs.

Variables are authored into the **session layer**, which is where usd_chunks already keeps
its bookkeeping. Setting one therefore does not dirty the file the user opened, and because
a session opinion is stronger than the root layer's, a variable set here overrides a
studio-authored default of the same name rather than colliding with it.

**A variable is stage-global; recooking is not.** Any chunk anywhere can read ${SHOT}, but
only chunks downstream of this one on the exec chain re-execute when it changes. Wire this
upstream of what should react to it.

**Known limit:** changing a variable recomposes the stage live, but introducing one that
did not exist when the stage was opened does not reach USD-native arcs -- Pcp records no
dependency on a variable whose lookup failed, so a reference that was already unresolved
stays unresolved until the stage is reopened. Params resolved by usd_chunks are unaffected;
they read the variables on every access.

Requires: usd_chunks.
"""

from __future__ import annotations

import logging

from usd_chunks import chunk, chunk_notice, chunk_utils

logger = logging.getLogger(__name__)

#: Params the chunk always has. Everything else on it is a lofted variable.
_FIXED = frozenset({"variables", "owned", "composed"})

#: Where the chunk records which variable names it authored last time, so a name deleted
#: from the spec is removed from the stage rather than left behind forever. Hidden:
#: bookkeeping, not a control -- the same shape as the Switch's `muted` record.
_OWNED_PARAM = "owned"


def _parse_spec(text):
    """The variables the chunk declares, as (name, default) rows.

    One per line, `NAME = default` or `NAME default`; the default is optional. Text rather
    than params because the declaration has to survive a save and a runtime-lofted param
    does not -- the same trade the Wedge makes for its ranges, and for the same reason: the
    file stays readable and the lofted params are a view of it.
    """
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, default = line.partition("=")
        if not sep:
            name, _, default = line.partition(" ")
        name = name.strip()
        if not name.isidentifier():
            logger.warning("%r is not a usable variable name, skipping", name)
            continue
        if name in _FIXED:
            logger.warning("%r is reserved by the chunk, skipping", name)
            continue
        rows.append((name, default.strip()))
    return rows


def _variables(node):
    """The lofted variable params, in author order.

    Selected by excluding the fixed names rather than by a flag: unlike a Wedge range these
    are ordinary input params -- connectable, editable, drivable by a wire -- so there is no
    flag that distinguishes them. Groups are filtered by type because GroupFlags and
    ParamFlags share bits, so a Group answers has_flag for things it does not have.
    """
    return [
        p for p in node.get_my_params()
        if not isinstance(p, chunk.Group) and p.name not in _FIXED
    ]


def _sync_variables(node):
    """Rebuild the lofted params so they match the spec.

    Called on load as well as on edit. A chunk does not own params it lofted at runtime --
    they are gone by the time the file is reopened, so without this the chunk comes back
    holding only its spec text and authors nothing at all.
    """
    spec = node.get_param("variables")
    wanted = dict(_parse_spec(spec.value if spec else ""))
    have = {p.name: p for p in _variables(node)}

    for name, param in have.items():
        if name not in wanted:
            node.remove_param_or_group(param)

    for name, default in wanted.items():
        if name in have:
            continue
        node.add_param(chunk.LineEdit(
            name, name, default_value=default,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip=f"Value of the {name} variable. Spell it as a ${{{name}}} reference in "
                    "any field, or wire something into this port to drive it.",
        ))


def _recorded(node) -> set:
    param = node.get_param(_OWNED_PARAM)
    return {n.strip() for n in (param.value or "").split(",") if n.strip()} if param else set()


def _record(node, names) -> None:
    param = node.get_param(_OWNED_PARAM)
    if param is not None:
        param.value = ",".join(sorted(names))


def execute(node):
    stage = node.get_stage()
    if stage is None:
        return
    if edit_layer := node.get_edit_context():
        # A Stage Expression authors layer metadata, never prims. Clearing is still right:
        # a chunk retyped into one must not keep contributing whatever it authored before.
        edit_layer.Clear()

    mine = {p.name: p.value for p in _variables(node) if p.value not in (None, "")}

    # Start from what is already in the session layer rather than from scratch, so two
    # Stage Expression chunks do not erase each other -- and so a variable some other part
    # of usd_chunks put there survives. Root-layer variables are never touched: they are
    # the studio's, they compose underneath this, and a name defined in both resolves to
    # the session opinion (this one) without either being lost.
    session = stage.GetSessionLayer()
    current = dict(session.expressionVariables or {})

    # Names this chunk authored last time and no longer declares. Dropping them by name --
    # rather than by rewriting the whole dict from `mine` -- is what keeps another chunk's
    # variables intact while still letting a deleted spec line actually delete something.
    for stale in _recorded(node) - set(mine):
        current.pop(stale, None)

    current.update(mine)
    chunk_utils.set_stage_expression_variables(stage, current)
    _record(node, mine)

    if composed := node.get_param("composed"):
        # The full composed set, this chunk's contribution included -- and the studio's
        # root-layer variables alongside it, which is the whole point of showing it: the
        # names available to type into a field are not only the ones declared here.
        variables = chunk_utils.stage_expression_variables(stage)
        composed.value = "\n".join(f"{k} = {v}" for k, v in sorted(variables.items()))

    if mine:
        logger.debug("%d variable(s): %s", len(mine), ", ".join(sorted(mine)))
    else:
        logger.debug("no variables declared")


def _spec_changed(owner, _notice, _sender):
    node = owner.get_chunk()
    _sync_variables(node)
    execute(node)


def _chunk_loaded(owner, _notice, _sender):
    """Loft the variables back after a load.

    The spec restores as a plain string value; the params it declares do not restore at
    all. Without this the chunk reopens holding only its text and every reference to its
    variables goes unresolved -- silently, from a file that looks intact.
    """
    _sync_variables(owner)


def main():
    chunk_api = chunk.Chunk("StageExpression")
    chunk_api.add_param(chunk.TextEdit(
        "variables", "Variables",
        default_value="",
        rows=4,
        tooltip="One variable per line, NAME = value. Each becomes a port you can wire "
                "into, and a name you can spell as a ${...} reference in any other "
                "chunk's field.",
    ))
    owned = chunk.LineEdit(
        _OWNED_PARAM, "Owned",
        tooltip="Bookkeeping: the variable names this chunk authored, so one deleted from "
                "the spec is removed from the stage rather than left behind.",
    )
    owned.set_state(chunk.ChunkFlags.State.HIDDEN)
    chunk_api.add_param(owned)
    chunk_api.add_param(chunk.TextEdit(
        "composed", "In Effect",
        flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
        rows=3,
        tooltip="Every variable in effect on the stage, including ones authored by the "
                "file that was opened rather than by this chunk.",
    ))

    chunk_api.add_callback(_spec_changed, sender=chunk_api.get_param("variables"))
    # Runtime-lofted params do not survive a save; the spec does.
    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)
    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Declares USD stage expression variables. Each line becomes a name usable as a "
            "${...} reference in any chunk's field -- prim paths and asset paths alike -- "
            "and a port a Wedge or any other chunk can drive.\n"
            "Variables the opened file already declares are read and shown alongside these "
            "rather than replaced; a name set here overrides a studio default of the same "
            "name via the session layer, leaving their file untouched.\n"
            "A variable is visible stage-wide, but only chunks downstream of this one "
            "re-execute when it changes -- wire it upstream of what should react."
        ),
    )
