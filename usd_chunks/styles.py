"""Styles: ready-made looks put on an object in one go -- worn paint, rubble, hanging vines.

A style is a saved chunk network (a ChunkGroup file, see the chunkgroup-asset skill) whose
prim paths are written against "/", which stands for the object it is put on. It goes on
through a Reference chunk: a Reference that hosts a network resolves the network's prim
paths under its Location (chunk_stage.host_networks), so with Location /crate the style's
"/" is /crate and its "/rubble" is /crate/rubble. One file works on any object, and the same
style on two objects is two Reference nodes that don't fight.

Styles are found in a ``styles`` folder inside each chunk folder the registry searched
(USD_CHUNK_DIRS and the bundled chunks), and in each folder on USD_CHUNK_STYLE_DIRS. A style
is ``<name>.usda``, with an optional ``<name>.png`` beside it: its picture. Its label is the
file name in words ("worn_paint" -> "Worn Paint"), and its summary the first line of the
group's description.

Every style is a menu entry under Styles (the Reference chunk's config lists them), so it is
in the Tab menu of every frontend, and ``scene.create.worn_paint(...)`` in a script.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from pxr import Sdf

from usd_chunks import chunk_naming, chunk_registry

logger = logging.getLogger(__name__)

#: Extra folders of styles, separated like PATH.
STYLE_DIRS_ENV = "USD_CHUNK_STYLE_DIRS"

#: The folder inside a chunk folder that holds its styles.
STYLE_FOLDER = "styles"

#: The menu a style is listed in, from the top of the menu.
MENU = "Styles"


@dataclass(frozen=True)
class Style:
    """One style file.

    Attributes:
        name: The file name without its extension, as a script names it.
        label: The name in words, as a menu shows it.
        path: The .usda file.
        thumbnail: The picture beside it, or None.
        summary: The first line of the group's description; "" without one.
    """

    name: str
    label: str
    path: Path
    thumbnail: Path | None
    summary: str


def style_dirs() -> list[Path]:
    """The folders styles are found in, first wins on a duplicate name.

    Returns:
        Each USD_CHUNK_STYLE_DIRS folder, then the ``styles`` folder of each chunk folder on
        USD_CHUNK_DIRS (the bundled chunks included) and of any other the registry searched,
        those that exist.
    """
    found: list[Path] = []
    extra = [Path(p) for p in os.environ.get(STYLE_DIRS_ENV, "").split(os.pathsep) if p.strip()]
    # The chunk folders in USD_CHUNK_DIRS order, then any other the registry searched (the
    # marketplace's): the registry keeps its folders as a set, which has no order.
    searched = [Path(os.path.abspath(d)) for d in chunk_registry.USD_CHUNK_DIRS()]
    searched += sorted((Path(d) for d in getattr(chunk_registry.ChunkRegistry, "registered_directories", set())
                        if Path(d) not in searched), key=str)
    for folder in extra + [d / STYLE_FOLDER for d in searched]:
        folder = folder.resolve()
        if folder.is_dir() and folder not in found:
            found.append(folder)
    return found


def label_for(name: str) -> str:
    """A file name in words: "worn_paint" -> "Worn Paint"."""
    return " ".join(word.capitalize() for word in name.replace("-", "_").split("_") if word)


def summary_of(path: Path) -> str:
    """The first line of the description of the chunk group a style file holds."""
    layer = Sdf.Layer.FindOrOpen(str(path))
    if layer is None:
        return ""
    for root in layer.rootPrims:
        for prim in [root, *root.nameChildren]:
            if prim.typeName != chunk_naming.CHUNK_PRIM_TYPENAME:
                continue
            doc = prim.attributes.get("inputs:doc")
            text = str(doc.default or "") if doc is not None else ""
            if text.strip():
                return text.strip().splitlines()[0].strip()
    return ""


def find_styles() -> list[Style]:
    """Every style in the style folders, by label.

    Returns:
        The styles; one per file name, from the first folder that has it.
    """
    styles: dict[str, Style] = {}
    for folder in style_dirs():
        for path in sorted(folder.glob("*.usda")):
            name = path.stem
            if name in styles:
                continue
            picture = path.with_suffix(".png")
            styles[name] = Style(name, label_for(name), path, picture if picture.is_file() else None,
                                 summary_of(path))
    return sorted(styles.values(), key=lambda s: s.label)


def selected_root(chunk) -> str | None:
    """Where a new style goes: the first prim selected in the viewport.

    A menu preset (chunk_registry.MenuEntry): called with the new Reference chunk as it is
    made, so the style lands on whatever is selected at that moment.

    Returns:
        The prim's path, or None with nothing selected -- the Location is left empty then,
        and the style works on the whole stage.
    """
    scene = getattr(chunk_registry.ChunkRegistry(), "scene", None)
    try:
        prims = list(scene.get_selected_prims()) if scene is not None else []
    except Exception:  # noqa: BLE001 - a host with no selection to give still makes the node
        prims = []
    for prim in prims:
        path = str(prim.GetPath())
        if path != "/":
            return path
    return None


def menu_variants() -> list[tuple[str, dict]]:
    """The Reference chunk's menu entries for the styles, under Styles.

    Returns:
        (label, presets) pairs, as ChunkConfig.variants takes them.
    """
    return [(f"/{MENU}/{style.label}", {"file_path": str(style.path), "primpath_location": selected_root})
            for style in find_styles()]
