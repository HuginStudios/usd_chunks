"""Locators for bundled resource files (icons).

These must resolve *inside* the installed package. An earlier version walked up to a
``resources`` directory sitting next to the package, which works from a checkout but
resolves to a non-existent ``site-packages/resources`` once pip-installed.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def get_resource_path():
    return Path(files("usd_chunks")) / "resources"


def get_icons_path():
    return get_resource_path() / "icons"


def get_icon(name, ext=".png"):
    res = Path(get_icons_path() / name).with_suffix(ext)
    return res
