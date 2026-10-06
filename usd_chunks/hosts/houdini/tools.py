"""The TAB menu: one entry per chunk, under ``usdchunks/<category>`` in LOP networks.

Houdini's TAB menu lists shelf tools. Every chunk node is the same type
(``usdchunks::chunk``), so its entries are written here as tools instead -- one per
chunk_registry.MenuEntry, i.e. per chunk module plus one per declared variant -- into a
shelf file in the user cache, which is then loaded. The generic type itself is hidden from
the menu; on its own it is not a chunk yet.

Built once the UI is up (pythonrc defers it), because it discovers every chunk module.
"""
from __future__ import annotations

import logging
import os
import re
from xml.sax.saxutils import escape, quoteattr

import hou

from usd_chunks import chunk_cache, chunk_registry
from usd_chunks.hosts.houdini import lop
from usd_chunks.hosts.houdini.scene import ensure_scene

logger = logging.getLogger(__name__)

#: The TAB menu's top-level folder.
ROOT_MENU = "usdchunks"
ICON = "NETWORKS_lop"
#: The chunk module whose TAB entry makes a chunkgroup node (chunkgroup.GROUP_TYPE).
GROUP_MODULE = "chunkgroup"


def _label(text: str) -> str:
    """A menu label: a module's snake_case name reads as Houdini's Title Case."""
    if text == text.lower():
        return " ".join(w.capitalize() for w in text.replace("_", " ").split())
    return text


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _tool_name(namespace: tuple[str, ...]) -> str:
    return "usdchunks_" + re.sub(r"\W+", "_", "_".join(namespace)).strip("_").lower()


def _tool(name: str, label: str, submenu: str, script: str, help_text: str = "") -> str:
    return (
        f"  <tool name={quoteattr(name)} label={quoteattr(label)} icon={quoteattr(ICON)}>\n"
        f"    <helpText>{_cdata(help_text)}</helpText>\n"
        "    <toolMenuContext name=\"network\">\n"
        "      <contextNetType>LOP</contextNetType>\n"
        "    </toolMenuContext>\n"
        f"    <toolSubmenu>{escape(submenu)}</toolSubmenu>\n"
        f"    <script scriptType=\"python\">{_cdata(script)}</script>\n"
        "  </tool>\n"
    )


def shelf_xml(entries) -> str:
    """A shelf document with one LOP TAB menu tool per menu entry, and Import Network."""
    tools = [_tool(
        "usdchunks_import_network", "Import Network...", ROOT_MENU,
        "from usd_chunks.hosts.houdini import chunkgroup\nchunkgroup.import_from_tool(kwargs)\n",
        "Import a usd_chunks network (.usda) as chunk LOPs: a chunkgroup becomes a subnet, "
        "its wires LOP wires.")]
    seen = set()
    for entry in entries:
        name = _tool_name(entry.namespace)
        if name in seen:
            continue
        seen.add(name)
        submenu = "/".join((ROOT_MENU, *(_label(p) for p in entry.namespace[:-1])))
        variant = "/".join(entry.namespace) if entry.presets is not None else ""
        if entry.module.module_name == GROUP_MODULE:
            # A chunkgroup is its own node type, a container, not a chunk node.
            script = "from usd_chunks.hosts.houdini import chunkgroup\nchunkgroup.create_from_tool(kwargs)\n"
        else:
            script = (
                "from usd_chunks.hosts.houdini import lop\n"
                f"lop.create_from_tool(kwargs, {entry.module.module_name!r}, {variant!r})\n"
            )
        tools.append(_tool(name, _label(entry.namespace[-1]), submenu, script, entry.tooltip or ""))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<shelfDocument>\n'
            + "".join(tools) + "</shelfDocument>\n")


def shelf_path() -> str:
    return chunk_cache.user_cache_dir("houdini", "usdchunks_tab_menu.shelf")


def install_tab_menu() -> str:
    """Write the chunk tools' shelf file and load it. Returns its path."""
    ensure_scene()
    path = shelf_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(shelf_xml(chunk_registry.ChunkRegistry().get_menu_entries()))
    hou.shelves.loadFile(path)
    if (node_type := lop.node_type()) is not None:
        node_type.setHidden(True)
    logger.info("usd_chunks: TAB menu tools loaded from %s", path)
    return path
