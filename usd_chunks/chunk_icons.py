"""Which icon a chunk's node shows, for any frontend to draw.

An icon is the name of a built-in icon (usd_chunks/resources/icons/node/<name>.svg: line
icons in one colour, tinted when drawn) or a path to an image file.

A node's icon, first match wins:

1. The chunk prim's UsdUINodeGraphNodeAPI icon (ui:nodegraph:node:icon), for one node.
2. The chunk's own `icon`, when its main() sets one.
3. The `icon` in its module's config().

Nothing else picks one: a chunk that sets no icon shows none. Only a chunk knows what it
is, so nothing outside it lists chunks or guesses from their names.

Its colour is the prim's UsdUINodeGraphNodeAPI displayColor when authored, else the
built-in icon's own colour.
"""
from __future__ import annotations

from pxr import Usd, UsdUI

# Each built-in icon's own colour, as a hex string. About the icons, not about any chunk.
ICON_COLORS = {
    "cube": "#4ade80", "sun": "#facc15", "move": "#22d3ee", "rotate": "#60a5fa",
    "scale": "#22d3ee", "pivot": "#3b82f6", "eye": "#60a5fa", "link": "#2dd4bf",
    "ruler": "#c084fc", "shuffle": "#c084fc", "bars": "#c084fc", "posts": "#c084fc",
    "rails": "#c084fc", "camera": "#60a5fa", "sphere": "#f472b6", "palette": "#a855f7",
    "layers": "#818cf8", "waves": "#38bdf8", "output": "#38bdf8", "view": "#fb923c",
    "folder": "#fbbf24", "code": "#94a3b8", "sliders": "#94a3b8", "image": "#f472b6",
    "merge": "#94a3b8", "scatter": "#4ade80", "sprout": "#4ade80", "home": "#fb923c",
    "sparkles": "#fb923c", "zap": "#f87171", "bone": "#e5e7eb", "film": "#f472b6",
    "tag": "#fbbf24", "sigma": "#a78bfa", "dice": "#a78bfa", "database": "#818cf8",
    "download": "#38bdf8", "curve": "#4ade80", "grid": "#818cf8", "brush": "#4ade80",
    "box_select": "#818cf8",
}
DEFAULT_ICON_COLOR = "#94a3b8"


def icon_color(icon: str) -> str:
    """The colour a built-in icon is drawn in by default.

    Args:
        icon: An icon name or image path.

    Returns:
        A hex colour; a neutral grey for an icon with no colour of its own.
    """
    return ICON_COLORS.get(icon, DEFAULT_ICON_COLOR)


def node_icon(chunk, prim: Usd.Prim | None = None) -> tuple[str, tuple[float, float, float] | None]:
    """The icon a chunk's node shows, and the colour authored for it in USD.

    Args:
        chunk: The chunk the node shows.
        prim: Its chunk prim, read for an authored UsdUI icon and displayColor.

    Returns:
        The icon (a built-in name or an image path, "" for none), and the authored
        displayColor as (r, g, b) in 0-1, or None when none is authored.
    """
    icon = ""
    color = None
    if prim is not None and prim.IsValid():
        api = UsdUI.NodeGraphNodeAPI(prim)
        icon_attr = api.GetIconAttr()
        if icon_attr and icon_attr.HasAuthoredValue():
            asset = icon_attr.Get()
            icon = (asset.resolvedPath or asset.path) if asset else ""
        color_attr = api.GetDisplayColorAttr()
        if color_attr and color_attr.HasAuthoredValue():
            value = color_attr.Get()
            color = tuple(min(max(float(v), 0.0), 1.0) for v in value)
    if not icon:
        icon = getattr(chunk, "icon", "") or ""
    if not icon:
        try:
            config = chunk.get_config()
        except Exception:  # noqa: BLE001 - a chunk mid-teardown still paints
            config = None
        icon = getattr(config, "icon", "") or ""
    return icon, color
