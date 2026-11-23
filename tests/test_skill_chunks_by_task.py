"""The build-network skill's chunks-by-task page lists every chunk, and only chunks that exist.

Agents planned from the twenty chunks the skill named and modelled the rest from boxes: a
fence as a box, a house as a box with a cube roof, while Fence and House chunks were there.
"""
from __future__ import annotations

import re
from pathlib import Path

from usd_chunks import Scene, chunk_catalog, chunk_registry

SKILL = Path(__file__).resolve().parent.parent / "skills" / "usd-chunks-build-network"
PAGE = SKILL / "chunks-by-task.md"
ROW = re.compile(r"^- `(\w+)`(?: / `(\w+)`)?:")


def _listed() -> set[str]:
    names = set()
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        if found := ROW.match(line):
            names.update(n for n in found.groups() if n)
    return names


def _modules(registry) -> set[str]:
    return {e.module for e in chunk_catalog.entries(registry) if e.presets is None and e.category != "Examples"}


def test_every_chunk_is_listed_and_every_listed_chunk_exists():
    with Scene():
        registry = chunk_registry.ChunkRegistry()
        modules = _modules(registry)
        every_name = {e.name for e in chunk_catalog.entries(registry)}
    listed = _listed()
    assert len(modules) > 50
    assert sorted(modules - listed) == [], "chunks missing from chunks-by-task.md"
    assert sorted(listed - every_name) == [], "names in chunks-by-task.md that are no chunk"


def test_backticked_presets_exist():
    """Menu entries named in a row (`picket_fence`, `distant_light`) are real script names."""
    with Scene():
        every_name = {e.name for e in chunk_catalog.entries(chunk_registry.ChunkRegistry())}
    text = PAGE.read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z]+_[a-z_]+)`", text)) - _listed()
    params = {"max_slope", "avoid_prims", "bake_all"}
    assert sorted(named - every_name - params) == []


def test_the_skill_points_at_the_page():
    assert "chunks-by-task.md" in (SKILL / "SKILL.md").read_text(encoding="utf-8")
