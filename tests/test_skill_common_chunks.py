"""The build-network skill's common-chunks list only names chunks and params that exist."""
from __future__ import annotations

import re
from pathlib import Path

from usd_chunks import Scene, chunk_catalog, chunk_registry

SHEET = Path(__file__).resolve().parent.parent / "skills" / "usd-chunks-build-network" / "common-chunks.md"
LINE = re.compile(r"^- (\w+): .* Params: ([\w, ]+)\.$")


def _rows() -> list[tuple[str, list[str]]]:
    rows = []
    for line in SHEET.read_text(encoding="utf-8").splitlines():
        if line.startswith("- "):
            found = LINE.match(line)
            assert found, f"not `- name: use. Params: a, b.`: {line}"
            rows.append((found[1], [p.strip() for p in found[2].split(",")]))
    return rows


def _names(params, out: set) -> set:
    for p in params:
        out.add(p.name)
        _names(p.children, out)
    return out


def test_every_chunk_and_param_in_the_list_exists():
    rows = _rows()
    assert len(rows) > 10
    with Scene():
        registry = chunk_registry.ChunkRegistry()
        missing = []
        for name, params in rows:
            found = chunk_catalog.describe(registry, name)
            if found is None:
                missing.append(name)
                continue
            known = _names(found[3], set())
            missing += [f"{name}.{p}" for p in params if p not in known]
    assert not missing, missing


def test_the_skill_points_at_the_list():
    assert "common-chunks.md" in (SHEET.parent / "SKILL.md").read_text(encoding="utf-8")
