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


def _summary(description: str) -> str:
    lines = chunk_registry.clean_description(description or "").splitlines()
    return lines[0].strip() if lines else ""


def entries(registry: chunk_registry.ChunkRegistry, query: str = "") -> list[Entry]:
    """Every module and menu entry, sorted by name; with query, those whose name, module or
    description mention it (any case).

    Named as scene.create resolves them: a module by its file name first, then a menu entry
    by its label. A label two entries share names neither, so it is left out.
    """
    found: dict[str, Entry] = {}
    variants: dict[str, list[Entry]] = {}
    for item in registry.get_menu_entries(sort_result=False):
        module = item.module.module_name.split(".")[-1]
        config = item.module.config
        if item.presets is None:
            found[module] = Entry(module, module, _category(config), _summary(config.description))
        else:
            summary = f"{module} with " + ", ".join(f"{k}={v!r}" for k, v in item.presets.items())
            name = script_name(item.label)
            variants.setdefault(name, []).append(Entry(name, module, _category(config), summary, item.presets))
    for name, same in variants.items():
        if name not in found and len(same) == 1:
            found[name] = same[0]
    rows = sorted(found.values(), key=lambda e: e.name)
    if not query:
        return rows
    q = query.lower()
    descriptions = {m.module_name.split(".")[-1]: (m.config.description or "").lower()
                    for m in registry.get_chunk_modules()}
    return [e for e in rows if q in e.name or q in e.module or q in descriptions.get(e.module, "")]


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
            level = level[0].get_my_params() if level else p.get_params()
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
