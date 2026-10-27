"""Locators for bundled resource files (icons).

These resolve *inside* the installed package through importlib.resources, never relative
to the package directory: a ``resources`` directory beside the package exists in a
checkout but not in ``site-packages`` once pip-installed.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def get_resource_path():
    """The package's bundled ``resources`` directory."""
    return Path(files("usd_chunks")) / "resources"


def get_icons_path():
    """The directory holding the bundled icons."""
    return get_resource_path() / "icons"


def get_icon(name, ext=".png"):
    """Path of a bundled icon, given its name without extension."""
    res = Path(get_icons_path() / name).with_suffix(ext)
    return res


def get_node_icon(name: str) -> Path | None:
    """The built-in node-graph icon of this name (resources/icons/node/<name>.svg), if any.

    Args:
        name: An icon name such as "sun" or "move".

    Returns:
        The icon's path, or None when there is no built-in icon of that name.
    """
    if not name or "/" in name or "\\" in name:
        return None
    path = get_icons_path() / "node" / f"{name}.svg"
    return path if path.is_file() else None
