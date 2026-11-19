"""Blender's offer to install a chunk's missing packages: what it tells the user.

Skipped outside Blender, like test_blender_frontend.py: the module imports bpy, which only
exists inside Blender (see that file for why bpy is checked in sys.modules, not imported).
"""
from __future__ import annotations

import sys

import pytest

if "bpy" not in sys.modules:  # pragma: no cover - the usual case outside Blender
    pytest.skip("bpy only exists inside Blender", allow_module_level=True)

from usd_chunks.frontends.blender.addon import dependencies  # noqa: E402


def test_a_good_install_names_the_package_and_skips_a_zero_count():
    """Installing scipy for four Scatter Points chunks said "0 chunk node type(s) now
    available", which reads like a failure: their node type had built fine before."""
    assert (dependencies.installed_message(["scipy>=1.11,<2"], 0, 4)
            == "Installed scipy; 4 chunk(s) recooked")


def test_chunk_types_that_can_be_added_now_are_counted():
    assert (dependencies.installed_message(["xatlas>=0.0.9,<1", "numpy>=2,<3"], 1, 0)
            == "Installed xatlas, numpy; 1 chunk type(s) can be added now")
