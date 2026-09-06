"""Wedge — one network, N variants, reproducible from the file.

A chunk network produces one result. A wedge turns it into a family of them: each lofted
OUTPUT param is a *range*, and cooking at `current_index` picks one value from each and
drives whatever is wired to it. Scrub the index and the network changes; sweep the index
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

import colorsys
import hashlib

from usd_chunks import chunk

#: Params the wedge always has. Everything else on it is a lofted range.
_FIXED = frozenset({
    "seed", "current_index", "count",
    "new_name", "new_min", "new_max", "add_range",
    "new_color_name", "add_color",
    "hue_min", "hue_max", "sat_min", "sat_max", "val_min", "val_max",
})


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
    digest = hashlib.sha256(f"{seed}:{index}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _hsv_bands(node):
    """The shared HSV bands every colour range samples inside.

    Shared rather than per-entry, which is a real limitation: two colour ranges on one
    wedge cannot yet have different saturation bands. Per-entry option lists are issue 04.
    The bands exist at all because sampling RGB uniformly gives garish, evenly-grey noise --
    plausible paint is a free hue with saturation and value held in narrow ranges.
    """
    def band(lo_name, hi_name, lo_default, hi_default):
        lo = float(node.get_param(lo_name).value if node.get_param(lo_name).value is not None else lo_default)
        hi = float(node.get_param(hi_name).value if node.get_param(hi_name).value is not None else hi_default)
        return (hi, lo) if lo > hi else (lo, hi)

    return band("hue_min", "hue_max", 0.0, 1.0), band("sat_min", "sat_max", 0.0, 1.0), band("val_min", "val_max", 0.0, 1.0)


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


def sample(node, index: int) -> dict:
    """Every range's value at `index`, as {param name: value}.

    Split out from execute() because the sweep runner and the verifier want the numbers
    without cooking anything.
    """
    seed = int(node.get_param("seed").value or 0)
    hue, sat, val = _hsv_bands(node)
    values = {}
    for param in _ranges(node):
        if isinstance(param, chunk.ColorPicker):
            # Three independent draws, each keyed by a different name, so hue does not
            # correlate with saturation across variants.
            h = hue[0] + _unit(seed, index, param.name + ":h") * (hue[1] - hue[0])
            s_ = sat[0] + _unit(seed, index, param.name + ":s") * (sat[1] - sat[0])
            v = val[0] + _unit(seed, index, param.name + ":v") * (val[1] - val[0])
            values[param.name] = colorsys.hsv_to_rgb(h % 1.0, s_, v)
            continue
        low = param.min_value if param.min_value is not None else 0.0
        high = param.max_value if param.max_value is not None else 1.0
        if low > high:
            low, high = high, low
        value = low + _unit(seed, index, param.name) * (high - low)
        values[param.name] = int(round(value)) if getattr(param, "is_int", False) else value
    return values


def execute(node):
    index = int(node.get_param("current_index").value or 0)
    for name, value in sample(node, index).items():
        node.get_param(name).value = value


def add_range(owner, notice, sender) -> None:
    """Loft one more range param from the staging fields.

    The range is fixed at add time because the param it becomes carries OUTPUT, and an
    OUTPUT param's widget is disabled (`chunk_core.py:307`) -- which is what stops the user
    dragging a handle the wedge is computing, but also means the interval cannot be dragged
    afterwards.
    """
    node = owner.get_chunk()
    name = (node.get_param("new_name").value or "").strip()
    if not name:
        print("[Wedge] Name the range before adding it")
        return
    if name in _FIXED or any(p.name == name for p in _ranges(node)):
        print(f"[Wedge] {name!r} is already taken")
        return

    low = float(node.get_param("new_min").value or 0.0)
    high = float(node.get_param("new_max").value or 1.0)
    if low > high:
        low, high = high, low

    node.add_param(
        chunk.Slider(
            name,
            name,
            default_value=low,
            min_value=low,
            max_value=high,
            flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip=f"Varies between {low:g} and {high:g}. Wire it to the param to drive.",
        )
    )
    node.get_param("new_name").value = ""
    execute(node)


def add_color(owner, notice, sender) -> None:
    """Loft a colour range: an OUTPUT ColorPicker sampled in HSV inside the shared bands."""
    node = owner.get_chunk()
    name = (node.get_param("new_color_name").value or "").strip()
    if not name:
        print("[Wedge] Name the colour before adding it")
        return
    if name in _FIXED or any(p.name == name for p in _ranges(node)):
        print(f"[Wedge] {name!r} is already taken")
        return

    node.add_param(
        chunk.ColorPicker(
            name,
            name,
            default_value=(0.5, 0.5, 0.5),
            flags=chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Varies across the wedge's HSV bands. Wire it to the colour to drive.",
        )
    )
    node.get_param("new_color_name").value = ""
    execute(node)


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
            tooltip="How many variants a sweep produces unless --count overrides it.",
        ),
    ])

    add_grp = chunk.Group(
        "add",
        "Add Range",
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )
    add_grp.add_param(
        chunk.LineEdit("new_name", "Name", flags=chunk.ChunkFlags.ParamFlags.LABELED,
                       tooltip="Name of the new range. Shows on its outport.")
    )
    add_grp.add_param(
        chunk.Slider("new_min", "Min", default_value=0.0,
                     flags=chunk.ChunkFlags.ParamFlags.LABELED)
    )
    add_grp.add_param(
        chunk.Slider("new_max", "Max", default_value=1.0,
                     flags=chunk.ChunkFlags.ParamFlags.LABELED)
    )
    button = chunk.Button("add_range", "Add", tooltip="Loft a range with these bounds.")
    button.add_callback(add_range)
    add_grp.add_param(button)
    chunk_api.add_param(add_grp)

    col_grp = chunk.Group(
        "colour",
        "Colour Ranges",
        flags=chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED,
    )
    for name, label, default, tip in (
        ("hue_min", "Hue Min", 0.0, "Hue wraps, so Min > Max is a valid arc through red."),
        ("hue_max", "Hue Max", 1.0, ""),
        ("sat_min", "Sat Min", 0.15, "Hold saturation narrow -- uniform RGB gives garish paint."),
        ("sat_max", "Sat Max", 0.85, ""),
        ("val_min", "Val Min", 0.15, "Value below ~0.1 reads as black under any light."),
        ("val_max", "Val Max", 0.85, ""),
    ):
        col_grp.add_param(
            chunk.Slider(name, label, default_value=default, min_value=0.0, max_value=1.0,
                         flags=chunk.ChunkFlags.ParamFlags.LABELED, tooltip=tip)
        )
    col_grp.add_param(
        chunk.LineEdit("new_color_name", "Name", flags=chunk.ChunkFlags.ParamFlags.LABELED,
                       tooltip="Name of the new colour range.")
    )
    col_btn = chunk.Button("add_color", "Add Colour", tooltip="Loft a colour range.")
    col_btn.add_callback(add_color)
    col_grp.add_param(col_btn)
    chunk_api.add_param(col_grp)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.UTILS,
        description=(
            "Turns one network into N variants. Each lofted range drives a param by "
            "connection; cooking at Index picks one value from every range.\n"
            "Every value is a pure function of (Seed, Index, range name), so any single "
            "variant regenerates on its own, on any machine, in any order."
        ),
    )
