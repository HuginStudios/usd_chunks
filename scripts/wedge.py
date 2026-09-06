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

from usd_chunks import chunk, chunk_notice

#: Params the wedge always has. Everything else on it is a lofted range.
_FIXED = frozenset({
    "seed", "current_index", "count",
    "new_name", "new_min", "new_max", "add_range",
    "new_color_name", "add_color", "ranges",
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


def _srgb_to_linear(c: float) -> float:
    """sRGB channel -> linear, the standard piecewise transfer.

    colorsys works in sRGB-shaped values, but a MaterialX standard_surface's base_color is
    *linear* -- porsche_configurator/variants.py authors Guards Red as (0.55, 0.010, 0.010),
    the linear form of sRGB (204, 27, 27). Note how far the dark channels fall.

    Handing sRGB numbers over as linear lifts every dark channel far above where it belongs
    and the saturation collapses: a sheet of pastel cars that reads as a taste decision
    rather than a transfer-function error, and that no amount of raising the saturation band
    can fix.
    """
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


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


def _parse_spec(text):
    """The ranges the chunk carries, as (name, kind, low, high) rows.

    One range per line -- `name low high`, or `name color` -- because the spec has to
    survive a save, and a string param does while runtime-lofted params do not. Making the
    text authoritative is also what the PRD asked for: the ranges are data in the .usda,
    the lofted params are a view of it, and the file stays readable and diffable.
    """
    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        name = parts[0]
        if len(parts) >= 2 and parts[1].lower() == "color":
            rows.append((name, "color", 0.0, 1.0))
        elif len(parts) >= 3:
            try:
                low, high = float(parts[1]), float(parts[2])
            except ValueError:
                print(f"[Wedge] cannot read range {line!r}")
                continue
            rows.append((name, "float", min(low, high), max(low, high)))
        else:
            print(f"[Wedge] cannot read range {line!r}")
    return rows


def _sync_ranges(node):
    """Rebuild the lofted OUTPUT params so they match the spec.

    Called on load as well as on edit. A chunk does not own params it lofted at runtime --
    they are gone by the time the file is reopened, so a wedge restored from disk arrives
    with only _FIXED and drives nothing, and every variant of a sweep comes out identical
    to variant 0. Same failure mtlx_shader's _chunk_loaded exists to prevent, and the same
    cure: loft from the thing that did survive.
    """
    wanted = {name: (kind, low, high) for name, kind, low, high in _parse_spec(
        node.get_param("ranges").value if node.get_param("ranges") else ""
    )}
    have = {p.name: p for p in _ranges(node)}

    for name, param in have.items():
        if name not in wanted:
            node.remove_param_or_group(param)

    flags = chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.LABELED
    for name, (kind, low, high) in wanted.items():
        if name in have:
            continue
        if kind == "color":
            node.add_param(chunk.ColorPicker(name, name, default_value=(0.5, 0.5, 0.5), flags=flags,
                                             tooltip="Varies across the wedge's HSV bands."))
        else:
            node.add_param(chunk.Slider(name, name, default_value=low, min_value=low,
                                        max_value=high, flags=flags,
                                        tooltip=f"Varies between {low:g} and {high:g}."))


def _spec_changed(owner, _notice, _sender):
    node = owner.get_chunk()
    _sync_ranges(node)
    execute(node)


def _chunk_loaded(owner, _notice, _sender):
    """Loft the ranges back after a load.

    The spec restores as a plain string value; the params it describes do not restore at
    all. Without this the wedge reopens holding only its controls, and a sweep produces N
    copies of one variant -- silently, from a file that looks intact.
    """
    _sync_ranges(owner)


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
            values[param.name] = tuple(
                _srgb_to_linear(c) for c in colorsys.hsv_to_rgb(h % 1.0, s_, v)
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
    index = int(node.get_param("current_index").value or 0)
    for name, value in sample(node, index).items():
        node.get_param(name).value = value


def _append_spec(node, line):
    """Add a row to the spec. Assigning it fires _spec_changed, which lofts the param."""
    existing = (node.get_param("ranges").value or "").rstrip()
    node.get_param("ranges").value = f"{existing}\n{line}" if existing else line


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

    _append_spec(node, f"{name} {low:g} {high:g}")
    node.get_param("new_name").value = ""


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

    _append_spec(node, f"{name} color")
    node.get_param("new_color_name").value = ""


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

    chunk_api.add_param(
        chunk.TextEdit(
            "ranges",
            "Ranges",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="One range per line: `name min max`, or `name color`.\n"
            "This is what the file stores -- the sliders below are a view of it, and are "
            "rebuilt from it every time the network loads.",
        )
    )

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

    chunk_api.add_callback(_spec_changed, sender=chunk_api.get_param("ranges"))
    # Runtime-lofted params do not survive a save; the spec does. Without this the wedge
    # reloads holding only its controls and a sweep yields N copies of variant 0.
    chunk_api.add_callback(_chunk_loaded, notice_alias=chunk_notice.ChunkNoticeAlias.CHUNK_LOADED)

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
