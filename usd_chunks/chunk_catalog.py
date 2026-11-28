"""What chunks are installed, and what one takes: the data behind `usdchunks list` / `show`.

Scripts could already ask -- dir(scene.create), help(scene.create.mesh) -- but only from a
running Scene, and a chunk's param names only once it was created. An agent or a person
finding their way spent most of their time reading chunk sources instead. This reads the
registry, toolkit-free: list() from each module's config() alone, describe() by loading the
one module (main() builds params without touching a stage) and walking its params.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from usd_chunks import chunk_registry

#: Params that hold no value a script sets: layout and readouts.
_NO_VALUE = {"Label", "Separator"}


def script_name(label: str) -> str:
    """A menu label as a Python name: "Dome Light" -> "dome_light". The name scene.create
    gives a menu entry."""
    return re.sub(r"\W+", "_", label.strip()).strip("_").lower()


@dataclass
class Entry:
    """One thing `scene.create.<name>()` can make: a module, or a menu entry presetting one."""

    name: str
    module: str
    category: str
    summary: str
    presets: dict[str, Any] | None = None
    #: What a menu entry is for, in task words, when its module's description doesn't say (a
    #: MaterialX node: its definition carries no documentation). Searched like a description.
    about: str = ""


@dataclass
class Param:
    """A param as a script sees it: its name, what it holds, and its default."""

    name: str
    label: str
    kind: str
    default: Any = None
    min_value: Any = None
    max_value: Any = None
    items: list[str] = field(default_factory=list)
    tooltip: str = ""
    children: list[Param] = field(default_factory=list)


def _category(config) -> str:
    category = getattr(config, "category", None)
    return str(getattr(category, "value", category) or "")


#: What the common MaterialX nodes do, by node family (the nodedef's name without ND_ and its type),
#: in the words someone looking for them uses. Their definitions carry no documentation, so without
#: this "tint" or "brighten" found nothing and the only way in was knowing the node's name.
MTLX_TASKS = {
    "colorcorrect": "Colour correction: hue, saturation, gamma, lift, gain, contrast and exposure. Tint, grade, "
                    "brighten or darken a texture or a library material's colour.",
    "hsvadjust": "Shift a colour's hue, saturation and value: tint, desaturate, brighten or darken.",
    "saturate": "Raise or lower a colour's saturation.",
    "multiply": "Multiply two values: tint a texture by a colour, scale a value.",
    "mix": "Blend two inputs by an amount or a mask: layer two colours or textures, a weave of two tones.",
    "noise2d": "Procedural noise pattern in UV space: break up a colour, vary roughness.",
    "noise3d": "Procedural noise pattern in 3D: break up a colour, vary roughness, a weave or grain.",
    "fractal3d": "Layered (fractal) noise in 3D: grain, dirt, mottling, wear.",
    "cellnoise3d": "Cell noise in 3D: a random value per cell, for flecks or tiles that vary.",
    "worleynoise3d": "Worley (cellular) noise in 3D: cells, stones, scales, cracks.",
    "image": "Read an image texture file.",
    "tiledimage": "Read an image texture file, repeated (tiled) across UVs.",
    "ramplr": "A colour gradient left to right across UVs.",
    "ramptb": "A colour gradient top to bottom across UVs.",
    "remap": "Remap a value from one range to another.",
    "smoothstep": "A soft step between two values: soften a mask's edge.",
}


def mtlx_task(nodedef: str) -> str:
    """What a MaterialX node is for (MTLX_TASKS), from its nodedef's name: ND_colorcorrect_color3."""
    name = str(nodedef or "")
    name = name[3:] if name.startswith("ND_") else name
    family = max((f for f in MTLX_TASKS if name == f or name.startswith(f + "_")), key=len, default="")
    return MTLX_TASKS.get(family, "")


def _summary(description: str) -> str:
    lines = chunk_registry.clean_description(description or "").splitlines()
    return lines[0].strip() if lines else ""


def entries(registry: chunk_registry.ChunkRegistry, query: str = "") -> list[Entry]:
    """Every module and menu entry, sorted by name; with query, the ones it finds, best first.

    A query is task words ("scatter rocks", "fence"): see `search`.

    Named as scene.create resolves them: a module by its file name first, then a menu entry
    by its label. A label two entries share names neither, so it is left out, and so is a
    private module (`_name`), which scene.create refuses to make.
    """
    found: dict[str, Entry] = {}
    variants: dict[str, list[Entry]] = {}
    for item in registry.get_menu_entries(sort_result=False):
        module = item.module.module_name.split(".")[-1]
        if module.startswith("_"):
            continue
        config = item.module.config
        if item.presets is None:
            found[module] = Entry(module, module, _category(config), _summary(config.description))
        else:
            summary = f"{module} with " + ", ".join(f"{k}={v!r}" for k, v in item.presets.items())
            about = mtlx_task(item.presets.get("nodedef", "")) if "nodedef" in item.presets else ""
            if about:
                summary = f"{about} ({summary})"
            name = script_name(item.label)
            variants.setdefault(name, []).append(Entry(name, module, _category(config), summary, item.presets,
                                                       about))
    for name, same in variants.items():
        if name not in found and len(same) == 1:
            found[name] = same[0]
    rows = sorted(found.values(), key=lambda e: e.name)
    if not query.split():
        return rows
    descriptions = {m.module_name.split(".")[-1]: m.config.description or "" for m in registry.get_chunk_modules()}
    return search(rows, descriptions, query)


#: How much a query word counts where it is found: in the name, the summary line, the rest.
_NAME, _SUMMARY, _DESCRIPTION = 6.0, 3.0, 1.0


#: Spellings that mean the same word: a query in either finds a text in either.
_SAME_WORD = {"colour": "color", "colours": "colors", "grey": "gray", "greys": "grays"}


def _words(text: str) -> str:
    """text lowered, underscores as spaces, so "usd_cube" holds the word "cube"; British spellings
    as American ("colour" as "color"), so either finds either."""
    words = re.sub(r"[_\W]+", " ", text.lower()).split()
    return " " + " ".join(_SAME_WORD.get(w, w) for w in words) + " "


def _word_score(word: str, name: str, summary: str, description: str) -> float:
    """How well one query word matches: a word of the text that starts with it ("rock" finds
    "rocks", but "rain" doesn't find "terrain"), best place first."""
    needle = " " + word
    if needle in name:
        return _NAME
    if needle in summary:
        return _SUMMARY
    if needle in description:
        return _DESCRIPTION
    return 0.0


def search(rows: list[Entry], descriptions: dict[str, str], query: str) -> list[Entry]:
    """The rows query finds, best first.

    Each word of the query matches the start of a word in a row's name, its summary line or
    the rest of its module's description, counting most in the name. Rows that match every
    word come first; when none does, rows that match some of them. A menu entry is matched
    by its own name and presets, not its module's description, so "terrain" finds Fence once
    rather than every fence preset. Ties go by name.
    """
    words = [_words(w).strip() for w in query.split()]
    words = [w for w in words if w]
    scored = []
    for e in rows:
        name = _words(e.name) + (_words(e.module) if e.presets is None else "")
        if e.presets is None:
            summary, description = _words(e.summary), _words(descriptions.get(e.module, ""))
        else:
            summary, description = _words(e.summary), _words(e.about)
        per_word = [_word_score(w, name, summary, description) for w in words]
        if any(per_word):
            scored.append((all(per_word), sum(per_word), e))
    if any(every for every, _, _ in scored):
        scored = [s for s in scored if s[0]]
    scored.sort(key=lambda s: (-s[1], s[2].name))
    return [e for _, _, e in scored]


def find(registry: chunk_registry.ChunkRegistry, name: str) -> Entry | None:
    """The module or menu entry a script name means."""
    return next((e for e in entries(registry) if e.name == name), None)


def describe(registry: chunk_registry.ChunkRegistry, name: str) -> tuple[Entry, str, list[Entry], list[Param]] | None:
    """(the entry, the module's full description, its menu entries, its params as a tree)."""
    entry = find(registry, name)
    if entry is None:
        return None
    module = registry.get_chunk_module(entry.module)
    description = chunk_registry.clean_description(module.config.description or "")
    variants = [e for e in entries(registry) if e.module == entry.module and e.presets is not None]
    chunks = module.create() if module.loaded else module.load()
    params = _tree(chunks[0]) if chunks else []
    if not chunks:
        # Its config() still reads (that's how it is listed), so say what it is and why its
        # params can't be listed, rather than show a chunk that takes nothing.
        needs = ", ".join(module.config.requirements or [])
        description += ("\n\nIts params can't be listed here: the module didn't load"
                        f" ({module.load_error or 'see the log'})."
                        + (f" It needs {needs}." if needs else ""))
    return entry, description, variants, params


def _tree(chunk) -> list[Param]:
    return [p for p in (_param(x) for x in chunk.get_my_params()) if p is not None]


def _param(p) -> Param | None:
    if type(p).__name__ == "ProxyParam":  # a ListGroup row's param stands in for the template's
        p = getattr(p, "_param_ref", p)
    cls = type(p).__name__
    if cls in _NO_VALUE:
        return None
    label = p.get_label() if hasattr(p, "get_label") else getattr(p, "label", "") or ""
    tooltip = (getattr(p, "tooltip", "") or "").strip()
    if cls in ("Group", "ListGroup"):
        level = p.get_my_params()
        if cls == "ListGroup":  # its level is the rows; a row's params are the template
            # With no rows yet (a new Sublayer), the template itself: its children.
            level = level[0].get_my_params() if level else p.children.get_params()
        kids = [c for c in (_param(x) for x in level) if c is not None]
        return Param(p.name, label, "list" if cls == "ListGroup" else "group", tooltip=tooltip, children=kids)
    if cls == "Vector":
        parts = p.get_params()
        inner = _kind(parts[0]) if parts else "float"
        default = getattr(p, "default_value", None)
        return Param(p.name, label, f"{inner}[{len(parts)}]", _plain(default), tooltip=tooltip)
    return Param(p.name, label, _kind(p), _plain(getattr(p, "default_value", None)),
                 getattr(p, "min_value", None), getattr(p, "max_value", None),
                 [str(i) for i in (getattr(p, "menu_items", None) or [])] if cls in ("Menu", "ImageMenu") else [],
                 tooltip)


def _kind(p) -> str:
    cls = type(p).__name__
    if cls in ("Slider", "Spinbox"):
        return "int" if getattr(p, "is_int", False) else "float"
    return {"Checkbox": "bool", "LineEdit": "text", "TextEdit": "text", "FileBrowser": "file",
            "Menu": "menu", "ImageMenu": "menu", "ColorPicker": "color", "Matrix": "matrix",
            "Button": "button"}.get(cls, cls.lower())


def _plain(value):
    """A default as a script would write it: tuples for vectors, not Gf types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return tuple(value)
    except TypeError:
        return str(value)
