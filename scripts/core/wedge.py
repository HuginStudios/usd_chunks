"""Wedge — one network, N variants, reproducible from the file.

A chunk network produces one result. A wedge turns it into a family of them: each row of
the Ranges list lofts an OUTPUT param -- a *range* -- and cooking at `current_index` picks
one value from each and drives whatever is wired to it. Scrub the index and the network changes; sweep the index
from the CLI and you have a dataset or a product catalogue.

    Wedge
      seed          = 42            <- editable
      current_index = 0             <- editable
      count         = 500
      ------------------------------------------------
      tz    [1.6 --*---- 2.9]  o--> SimDrop.tz
      mass  [ 20 -*----- 90 ]  o--> SimDrop.mass

Ranges drive their targets by *connection*, like any other driven param, so the graph shows
what varies as wires, a rename cannot break the link, and the target's authored value
survives underneath as the fallback the moment it is disconnected. Nothing is written into
the params being varied.

The whole point is that a 50KB `.usda` regenerates a dataset a customer would otherwise
receive as terabytes -- on their hardware, auditable, re-runnable at a different resolution
next quarter. That is only true if regeneration is exact, which is what `_unit` below is
for.

Requires: usd_chunks.
"""

from __future__ import annotations

import hashlib
import logging

from pxr import Sdf

from usd_chunks import chunk, chunk_notice

logger = logging.getLogger(__name__)

#: The Kind a range row can be.
KIND_NUMBER, KIND_COLOUR, KIND_VARIANTS = "Number", "Colour", "Variants"
KINDS = [KIND_NUMBER, KIND_COLOUR, KIND_VARIANTS]

#: The row fields of the Ranges list.
_ROW_FIELDS = ("r_name", "r_kind", "r_min", "r_max", "r_int", "r_col_min", "r_col_max",
               "r_prim", "r_set", "r_weights")

#: Params the wedge always has. Everything else on it is a lofted range.
_FIXED = frozenset({"seed", "current_index", "count", "mode", "ranges", "range_rows", *_ROW_FIELDS})

#: A Colour row's bounds when it does not say: the whole cube.
_BLACK, _WHITE = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)

#: Set on the node while _sync_ranges is adding and removing params, so the cooks those
#: edits trigger do not re-enter it.
_SYNCING = "_wedge_syncing"


def _unit(seed: int, index: int, name: str) -> float:
    """A stable float in [0, 1) from the triple that identifies one sample.

    Hashed with sha256 rather than the built-in `hash`, which is *not* usable here: Python
    randomises string hashing per process unless PYTHONHASHSEED is set, so `hash(name)`
    gives a different answer in every run and the whole recipe stops reproducing -- silently,
    and only across processes, which is the hardest way to find a bug.

    Deriving from `(seed, index, name)` rather than drawing from a running RNG is what makes
    a recipe shippable: variant 5,432 can be regenerated on its own, on another machine, in
    any order, in parallel, without producing the 5,431 before it. Sequential sampling would
    work identically for a full run and quietly break every one of those properties.
    """
    digest = hashlib.sha256(f"{seed}:{index}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _parse_spec(text):
    """The ranges the chunk carries, as (name, kind, low, high) rows.

    One range per line:

        height 1 9                          a number, sampled in the interval
        CarPaint color                      a colour, sampled in the wedge's HSV bands
        Paint variants /World/car color     one of a variantSet's variants, read off the stage
        Paint variants /World/car color 3,1,1   ...with the first picked three times as often

    This was what the file stored, back when a range could only be typed: a string
    survives a save and runtime-lofted params do not. The Ranges list does the storing now
    -- its rows are what the file carries, and they can be edited after the fact, which
    the text could not once it had become a slider. Text pasted into the hidden `ranges`
    field is imported into rows (_import_spec) and then cleared, so an older file, and
    anything that still writes the spec as text, keeps working.
    """
    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        name, rest = parts[0], parts[1:]
        kind = rest[0].lower() if rest else ""

        if kind == "color":
            rows.append({"name": name, "kind": "color"})
        elif kind == "variants" and len(rest) >= 3:
            row = {"name": name, "kind": "variants", "prim": rest[1], "set": rest[2]}
            if len(rest) >= 4:
                try:
                    row["weights"] = [float(w) for w in rest[3].split(",")]
                except ValueError:
                    logger.warning("cannot read weights %r, using uniform", rest[3])
            rows.append(row)
        elif len(rest) >= 2:
            try:
                low, high = float(rest[0]), float(rest[1])
            except ValueError:
                logger.warning("cannot read range %r", line)
                continue
            rows.append({"name": name, "kind": "float",
                         "low": min(low, high), "high": max(low, high)})
        else:
            logger.warning("cannot read range %r", line)
    return rows


def _row_list(node):
    return node.get_param("range_rows", chunk.is_inst(chunk.ListGroup)) if node.get_param("range_rows") else None


def _rows(node):
    """The ranges the chunk carries, as rows in the shape _parse_spec produces.

    Read off the Ranges list, which is what the file stores. A row without a name, or one
    whose name is taken by a control or an earlier row, is skipped -- it is a row being
    typed, not a range. A Variants row needs its prim and set before it is one.
    """
    rows_param = _row_list(node)
    if rows_param is None:
        return []
    out, seen = [], set()
    for i in range(min(rows_param.size, len(rows_param.get_my_params()))):
        def field(name, i=i):
            proxy = rows_param.get_indexed_param(rows_param.get_param(name), i)
            return proxy.value if proxy is not None else None

        name = (field("r_name") or "").strip()
        if not name or name in _FIXED or name in seen:
            continue
        kind = field("r_kind") or KIND_NUMBER
        if kind == KIND_COLOUR:
            low = tuple(float(c) for c in (field("r_col_min") or _BLACK))
            high = tuple(float(c) for c in (field("r_col_max") or _WHITE))
            row = {"name": name, "kind": "color", "low": low, "high": high}
        elif kind == KIND_VARIANTS:
            prim, vset = (field("r_prim") or "").strip(), (field("r_set") or "").strip()
            if not prim or not vset:
                continue
            row = {"name": name, "kind": "variants", "prim": prim, "set": vset}
            if weights := (field("r_weights") or "").strip():
                try:
                    row["weights"] = [float(w) for w in weights.split(",")]
                except ValueError:
                    logger.warning("cannot read weights %r, using uniform", weights)
        else:
            low, high = float(field("r_min") or 0.0), float(field("r_max") or 0.0)
            row = {"name": name, "kind": "float", "low": min(low, high), "high": max(low, high),
                   "int": bool(field("r_int"))}
        seen.add(name)
        out.append(row)
    return out


def _import_spec(node):
    """Turn text in the legacy `ranges` field into rows, then clear it.

    The field is hidden; it exists for files saved before the list, and for anything
    that still sets the spec as text. A line naming a row that already exists updates
    that row in place, so re-importing is idempotent.
    """
    spec = node.get_param("ranges")
    text = (spec.value or "") if spec else ""
    rows_param = _row_list(node)
    if not text.strip() or rows_param is None:
        return
    for row in _parse_spec(text):
        existing = next(
            (i for i in range(rows_param.size)
             if (rows_param.get_indexed_param(rows_param.get_param("r_name"), i).value or "").strip() == row["name"]),
            None,
        )
        if existing is None:
            rows_param.size += 1
            existing = rows_param.size - 1

        def put(field, value, i=existing):
            proxy = rows_param.get_indexed_param(rows_param.get_param(field), i)
            if proxy is not None and proxy.value != value:
                proxy.value = value

        put("r_name", row["name"])
        if row["kind"] == "color":
            put("r_kind", KIND_COLOUR)
            put("r_col_min", _BLACK)
            put("r_col_max", _WHITE)
        elif row["kind"] == "variants":
            put("r_kind", KIND_VARIANTS)
            put("r_prim", row["prim"])
            put("r_set", row["set"])
            put("r_weights", ",".join(f"{w:g}" for w in row["weights"]) if row.get("weights") else "")
        else:
            put("r_kind", KIND_NUMBER)
            put("r_min", row["low"])
            put("r_max", row["high"])
            put("r_int", bool(row.get("int")))
    spec.value = ""


def _slider_matches(param, row) -> bool:
    return (
        isinstance(param, chunk.Slider)
        and bool(getattr(param, "is_int", False)) == bool(row.get("int"))
    )


def _sync_ranges(node):
    """Make the lofted OUTPUT params match the Ranges rows.

    Called on load as well as on every cook. A chunk does not own params it lofted at
    runtime -- they are gone by the time the file is reopened, so a wedge restored from
    disk arrives with only _FIXED and drives nothing, and every variant of a sweep comes
    out identical to variant 0. Same failure mtlx_shader's _chunk_loaded exists to prevent,
    and the same cure: loft from the thing that did survive, which is the rows.

    A row edited after its param exists updates the param in place where it can -- a
    Number's bounds -- so the wires on its outport survive the edit. A change of kind, or
    of a Number between integer and float, is a different param type and has to be a new
    param.
    """
    if getattr(node, _SYNCING, False):
        return
    setattr(node, _SYNCING, True)
    try:
        wanted = {row["name"]: row for row in _rows(node)}
        have = {p.name: p for p in _ranges(node)}
        flags = chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED

        for name, param in have.items():
            row = wanted.get(name)
            stale = (
                row is None
                or (row["kind"] == "color" and not isinstance(param, chunk.ColorPicker))
                or (row["kind"] == "variants" and not isinstance(param, chunk.LineEdit))
                or (row["kind"] == "float" and not _slider_matches(param, row))
            )
            if stale:
                node.remove_param_or_group(param)
                continue
            if row["kind"] == "float":
                if param.min_value != row["low"]:
                    param.min_value = row["low"]
                if param.max_value != row["high"]:
                    param.max_value = row["high"]

        have = {p.name: p for p in _ranges(node)}
        for name, row in wanted.items():
            if name in have:
                continue
            if row["kind"] == "color":
                node.add_param(chunk.ColorPicker(name, name, default_value=row["low"], flags=flags,
                                                 tooltip="Each channel varies between the row's Min and Max colour."))
            elif row["kind"] == "variants":
                node.add_param(chunk.LineEdit(
                    name, name, flags=flags,
                    tooltip=f"One variant of {row['set']!r} on {row['prim']}"
                            + (" (weighted)" if row.get("weights") else "")
                            + ". Wire it to a Set Variant chunk's Variant Selection.",
                ))
            else:
                low, high = row["low"], row["high"]
                if row.get("int"):
                    low, high = int(round(low)), int(round(high))
                node.add_param(chunk.Slider(name, name, default_value=low, min_value=low, max_value=high,
                                            is_int=bool(row.get("int")), flags=flags,
                                            tooltip=f"Varies between {low:g} and {high:g}."))
    finally:
        setattr(node, _SYNCING, False)


def _refresh_count(node):
    """In Enumerate mode the count is derived, so write it where everything already looks.

    `usdchunks sweep` reads `inputs:count` straight off the prim without loading the
    network -- deliberately, so planning a sweep does not pay the startup cost twice. That
    only works if the number is in the file, so the wedge computes it here rather than
    making the CLI reimplement the decode.
    """
    mode_param = node.get_param("mode")
    if not mode_param or mode_param.value != ENUMERATE:
        return
    total = _combinations(node)
    if total <= 1:
        logger.warning("Enumerate needs at least one variants range with options")
    if node.get_param("count").value != total:
        node.get_param("count").value = total


def _spec_changed(owner, _notice, _sender):
    """Text landed in the legacy field: import it as rows. The cook that follows lofts."""
    node = owner.get_chunk()
    _import_spec(node)
    _sync_ranges(node)
    _refresh_count(node)
    execute(node)


def _mode_changed(owner, _notice, _sender):
    node = owner.get_chunk()
    _refresh_count(node)
    execute(node)


def _chunk_loaded(owner, _notice, _sender):
    """Loft the ranges back after a load.

    The rows restore as values; the params they describe do not restore at all. Without
    this the wedge reopens holding only its controls, and a sweep produces N copies of one
    variant -- silently, from a file that looks intact.
    """
    _import_spec(owner)
    _sync_ranges(owner)
    _refresh_count(owner)


def _ranges(node):
    """The lofted range params, in author order.

    Selected by the OUTPUT flag rather than by excluding known names: OUTPUT is what makes
    a param a range in the first place.

    Groups are filtered out by type first, and must be -- they are in get_my_params too, they
    answer has_flag, and GroupFlags and ParamFlags are separate IntFlag enums whose bits
    overlap, so a Group cheerfully reports having ParamFlags.OUTPUT. Reaching it as a range
    then fails on `Group has no attribute min_value`.

    get_my_params, not get_params: the latter walks the nested ChainMap and would reach into
    params this chunk does not own.
    """
    return [
        p for p in node.get_my_params()
        if not isinstance(p, chunk.Group)
        and hasattr(p, "has_flag")
        and p.has_flag(chunk.ChunkFlags.ParamFlags.OUTPUT)
    ]


def _weighted_pick(unit: float, weights) -> int:
    """Index chosen by weight rather than uniformly, from the same [0, 1) draw.

    Weights live on the wedge and not on the asset on purpose. What options exist is a fact
    about the product and belongs with it; how often to pick each one is a property of the
    job. A configurator wants the sales mix, a training set built from the same asset often
    wants the opposite -- rare configurations oversampled, precisely so a model does not fail
    on the ones it would otherwise almost never see. Same asset, opposite distributions, both
    correct, so the opinion belongs to whoever is generating rather than baked into shared
    data everyone inherits.
    """
    total = sum(weights)
    if total <= 0:
        return 0
    threshold = unit * total
    accumulated = 0.0
    for i, weight in enumerate(weights):
        accumulated += weight
        if threshold < accumulated:
            return i
    return len(weights) - 1


def _variant_names(node, prim_path, set_name):
    """The variants a variantSet offers, read off the stage.

    The options are not authored on the wedge on purpose. They belong to the asset --
    whoever built it decided what paints and body styles exist, and they are versioned and
    composed with it. Restating them here would mean maintaining a copy that drifts, and
    would lose everything a variant can carry beyond a value: a different material binding,
    a different mesh, a different referenced sub-asset, and the physics that goes with it.
    """
    stage = node.get_stage() if hasattr(node, "get_stage") else None
    if stage is None:
        return []
    prim = stage.GetPrimAtPath(Sdf.Path(prim_path))
    if not prim or not prim.IsValid():
        logger.warning("no prim at %s", prim_path)
        return []
    vset = prim.GetVariantSets().GetVariantSet(set_name)
    if not vset:
        logger.warning("%s has no variantSet %r", prim_path, set_name)
        return []
    return list(vset.GetVariantNames())


ENUMERATE = "Enumerate"


def _discrete(node):
    """The entries that have a finite option list, in spec order, as (name, options).

    Order is the spec's author order and must stay that way: it decides each entry's stride
    in the decode below, so reordering the spec renumbers every variant. Worth knowing
    before editing a spec that someone has already generated a dataset from.

    Only variantSets are discrete. A float or colour range has no option list to enumerate,
    so those keep being sampled even in Enumerate mode -- you get every configuration the
    asset offers, each with its own draw of the continuous parameters.
    """
    out = []
    for row in _rows(node):
        if row["kind"] != "variants":
            continue
        names = _variant_names(node, row["prim"], row["set"])
        if names:
            out.append((row["name"], names))
    return out


def _combinations(node) -> int:
    """How many variants Enumerate mode produces: the product of the option counts."""
    total = 1
    for _, options in _discrete(node):
        total *= len(options)
    return total


def _enumerated_pick(index: int, position: int, discrete) -> int:
    """Which option entry `position` takes at `index` -- a mixed-radix digit.

    Arithmetic on the index, never a walk, so enumeration keeps every property sampling
    has: variant 5,432 decodes on its own, in any order, in parallel, and --only and
    --verify work unchanged. Enumerating by iterating and counting would produce the same
    full sweep and break all three.
    """
    stride = 1
    for _, options in discrete[:position]:
        stride *= len(options)
    return (index // stride) % len(discrete[position][1])


def sample(node, index: int) -> dict:
    """Every range's value at `index`, as {param name: value}.

    Split out from execute() because the sweep runner and the verifier want the numbers
    without cooking anything.
    """
    seed = int(node.get_param("seed").value or 0)
    spec = {row["name"]: row for row in _rows(node)}
    mode_param = node.get_param("mode")
    enumerating = bool(mode_param) and mode_param.value == ENUMERATE
    discrete = _discrete(node) if enumerating else []
    positions = {name: i for i, (name, _) in enumerate(discrete)}

    values = {}
    for param in _ranges(node):
        row = spec.get(param.name, {})
        if row.get("kind") == "variants":
            names = _variant_names(node, row["prim"], row["set"])
            if not names:
                values[param.name] = ""
                continue
            if param.name in positions:
                # Enumerate visits every combination exactly once, so a weight has nothing
                # to act on -- honouring one would mean repeating some, which is a
                # different operation. Ignored here rather than silently in the decode.
                pick = _enumerated_pick(index, positions[param.name], discrete)
            else:
                unit = _unit(seed, index, param.name)
                weights = row.get("weights")
                if weights and len(weights) != len(names):
                    logger.warning(
                        "%s: %d weights for %d variants, using uniform",
                        param.name, len(weights), len(names),
                    )
                    weights = None
                pick = (_weighted_pick(unit, weights) if weights
                        else int(unit * len(names)) % len(names))
            values[param.name] = names[pick]
            continue
        if isinstance(param, chunk.ColorPicker):
            # One draw per channel, each keyed by its own name, so red does not track
            # green across variants. Between the row's Min and Max colour, channel by
            # channel: a range of (0.4, 0, 0)..(1, 0.1, 0.1) is every red, and a channel
            # with Min == Max is held.
            low, high = row.get("low", _BLACK), row.get("high", _WHITE)
            values[param.name] = tuple(
                min(lo, hi) + _unit(seed, index, f"{param.name}:{channel}") * abs(hi - lo)
                for channel, lo, hi in zip("rgb", low, high)
            )
            continue
        low = param.min_value if param.min_value is not None else 0.0
        high = param.max_value if param.max_value is not None else 1.0
        if low > high:
            low, high = high, low
        value = low + _unit(seed, index, param.name) * (high - low)
        values[param.name] = int(round(value)) if getattr(param, "is_int", False) else value
    return values


def execute(node):
    """Cook variant `current_index`: sync the lofted params to the rows, then sample.

    Syncing here rather than only from a callback is what makes the rows editable: every
    edit to a row cooks the chunk (a param changed), and the cook is where the lofted
    params catch up with it. _SYNCING keeps the adds and removes the sync itself makes
    from re-entering.
    """
    if not getattr(node, _SYNCING, False):
        _import_spec(node)
        _sync_ranges(node)
        _refresh_count(node)
    index = int(node.get_param("current_index").value or 0)
    for name, value in sample(node, index).items():
        node.get_param(name).value = value


def main():
    chunk_api = chunk.Chunk("Wedge")
    chunk_api.add_params([
        chunk.Slider(
            "seed",
            "Seed",
            default_value=0,
            is_int=True,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Changes every range's values without changing their intervals. Same "
            "seed and index always give the same variant.",
        ),
        chunk.Slider(
            "current_index",
            "Index",
            default_value=0,
            is_int=True,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Which variant is cooked. Scrub it to preview; `usdchunks sweep` runs "
            "the whole range of it headless.",
        ),
        chunk.Slider(
            "count",
            "Count",
            default_value=100,
            is_int=True,
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="How many variants a sweep produces unless --count overrides it.\n"
            "In Enumerate mode this is derived -- the product of the option counts -- and "
            "rewritten whenever the ranges change.",
        ),
        chunk.Menu(
            "mode",
            "Mode",
            menu_items=["Sample", ENUMERATE],
            default_value="Sample",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Sample: every range drawn from (Seed, Index). Good for datasets.\n"
            "Enumerate: walk the full cross product of the variantSets -- which is what a "
            "configurator is. Float and colour ranges have no option list, so they keep "
            "being sampled.",
        ),
    ])

    # The ranges, one row each. This is what the file stores; the OUTPUT params below the
    # controls are lofted from it (and rebuilt from it on load) and are the outports the
    # rest of the network wires to. A row stays editable after its param exists -- change
    # the bounds and the slider follows; change the kind and it is re-lofted.
    rows = chunk.ListGroup(
        "range_rows",
        "Ranges",
        tooltip="One row per range. Name is the outport. Number samples between Min and Max "
        "(Integer rounds); Colour samples each channel between Min Colour and Max Colour; "
        "Variants picks one of a variantSet's variants read off Prim -- wire it to a Set "
        "Variant chunk.",
    )
    rows.add_param(chunk.LineEdit("r_name", "Name", flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                  tooltip="Name of the range. Shows on its outport."))
    rows.add_param(chunk.Menu("r_kind", "Kind", menu_items=list(KINDS), default_value=KIND_NUMBER,
                              flags=chunk.ChunkFlags.ParamFlags.LABELED))
    rows.add_param(chunk.Slider("r_min", "Min", default_value=0.0, flags=chunk.ChunkFlags.ParamFlags.LABELED))
    rows.add_param(chunk.Slider("r_max", "Max", default_value=1.0, flags=chunk.ChunkFlags.ParamFlags.LABELED))
    rows.add_param(chunk.Checkbox("r_int", "Integer", default_value=False,
                                  flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                  tooltip="Sample whole numbers: a floor count, a slot index, a seed."))
    rows.add_param(chunk.ColorPicker("r_col_min", "Min Colour", default_value=_BLACK,
                                     flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                     tooltip="Colour only: the low end of each channel's range."))
    rows.add_param(chunk.ColorPicker("r_col_max", "Max Colour", default_value=_WHITE,
                                     flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                     tooltip="Colour only: the high end of each channel's range. "
                                     "A channel equal in Min and Max is held; make R wide and G, B "
                                     "narrow for every shade of red."))
    rows.add_param(chunk.LineEdit("r_prim", "Prim", flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                  tooltip="Variants only: the prim carrying the variantSet."))
    rows.add_param(chunk.LineEdit("r_set", "Variant Set", flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                  tooltip="Variants only: which of the prim's variantSets."))
    rows.add_param(chunk.LineEdit("r_weights", "Weights", flags=chunk.ChunkFlags.ParamFlags.LABELED,
                                  tooltip="Variants only, optional: comma-separated, one per variant "
                                  "in the set's order, e.g. 3,1,1. Empty is uniform."))
    chunk_api.add_param(rows)

    # The spec as text, hidden: what files saved before the list carry, and an import
    # channel -- text set here becomes rows and the field empties.
    spec = chunk.TextEdit(
        "ranges",
        "Ranges (text)",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="One range per line, imported into the rows above:\n"
        "  height 1 9\n"
        "  CarPaint color\n"
        "  Paint variants /World/car color 3,1,1",
    )
    spec.set_state(chunk.ChunkFlags.State.HIDDEN)
    chunk_api.add_param(spec)

    chunk_api.add_callback(_spec_changed, sender=chunk_api.get_param("ranges"))
    chunk_api.add_callback(_mode_changed, sender=chunk_api.get_param("mode"))
    # Runtime-lofted params do not survive a save; the rows do. Without this the wedge
    # reloads holding only its controls and a sweep yields N copies of variant 0.
    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.CORE,
        description=(
            "Turns one network into N variants. Each lofted range drives a param by "
            "connection; cooking at Index picks one value from every range.\n"
            "Every value is a pure function of (Seed, Index, range name), so any single "
            "variant regenerates on its own, on any machine, in any order."
        ),
    )
