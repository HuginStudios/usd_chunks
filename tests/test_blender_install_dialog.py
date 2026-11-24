"""Blender's dialog that offers to install a chunk's missing packages: how it names them.

Skipped outside Blender, like test_blender_frontend.py: the module imports bpy, which only
exists inside Blender (see that file for why bpy is checked in sys.modules, not imported).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

if "bpy" not in sys.modules:  # pragma: no cover - the usual case outside Blender
    pytest.skip("bpy only exists inside Blender", allow_module_level=True)

from usd_chunks import chunk_registry  # noqa: E402
from usd_chunks.frontends.blender.addon import dependencies  # noqa: E402


def _chunk(name, module_name):
    module = SimpleNamespace(module_name=module_name)
    return SimpleNamespace(name=name, get_chunk_module=lambda: module)


@pytest.fixture
def network(monkeypatch):
    """The biome example's chunks: four Scatter Points nodes with names of their own."""
    chunks = [_chunk("Mesh", "mesh"), _chunk("Trees", "scatter_points"),
              _chunk("Bushes", "scatter_points"), _chunk("Ferns", "scatter_points"),
              _chunk("Grass", "scatter_points")]
    scene = SimpleNamespace(get_chunks=lambda: chunks)
    # raising=False: an uninitialised registry has no scene attribute yet.
    monkeypatch.setattr(chunk_registry.ChunkRegistry(), "scene", scene, raising=False)
    return chunks


def test_packages_are_named_without_their_version_range():
    assert dependencies.package_names(["scipy>=1.11,<2"]) == "scipy"
    assert dependencies.package_names(["mujoco>=3.1,<4", "numpy>=2,<3"]) == "mujoco, numpy"


def test_the_nodes_waiting_for_the_install_are_named(network):
    """The dialog said "for chunk scatter_points": the biome's nodes are Trees, Bushes,
    Ferns and Grass, so nothing said which of them were waiting."""
    assert (dependencies.chunk_text("scatter_points")
            == "scatter_points (Bushes, Ferns, Grass, Trees)")


def test_a_long_list_of_nodes_is_cut_short(network):
    network.append(_chunk("Rocks", "scatter_points"))  # sorts before Trees
    assert (dependencies.chunk_text("scatter_points")
            == "scatter_points (Bushes, Ferns, Grass, Rocks and 1 more)")


def test_a_chunk_not_in_the_network_is_named_by_its_module(network):
    assert dependencies.chunk_text("uv_unwrap") == "uv_unwrap"


class _Layout:
    """Records what a draw() puts in the dialog, one entry per row."""

    def __init__(self, rows):
        self.rows = rows

    def column(self):
        return self

    def box(self):
        self.rows.append("box")
        return self

    def label(self, text="", icon="NONE"):
        self.rows.append("label")

    def prop(self, data, name, text=""):
        self.rows.append(f"prop {name}")

    def separator(self):
        self.rows.append("separator")


def _draw(opened_offline, online, monkeypatch):
    monkeypatch.setattr(dependencies, "_online", lambda: online)
    monkeypatch.setattr(chunk_registry.ChunkDependencyManager(), "target_dir", lambda: "deps")
    item = SimpleNamespace(chunk="scatter_points", packages="scipy>=1.11,<2", install=True)
    rows = []
    op = SimpleNamespace(layout=_Layout(rows), items=[item], opened_offline=opened_offline)
    context = SimpleNamespace(preferences=SimpleNamespace(system=SimpleNamespace(use_online_access=online)))
    dependencies.USDCHUNKS_OT_install_dependencies.draw(op, context)
    return rows


def test_turning_online_access_on_keeps_the_dialog_the_same_size(network, monkeypatch):
    """Ticking Allow Online Access in the dialog dropped the box that held it, so the dialog
    shrank under the mouse: Install moved away, the click landed outside, and that declined."""
    offline = _draw(opened_offline=True, online=False, monkeypatch=monkeypatch)
    after_ticking = _draw(opened_offline=True, online=True, monkeypatch=monkeypatch)

    assert "prop use_online_access" in offline
    assert after_ticking == offline


def test_a_dialog_opened_online_shows_no_switch(network, monkeypatch):
    rows = _draw(opened_offline=False, online=True, monkeypatch=monkeypatch)

    assert "prop use_online_access" not in rows
