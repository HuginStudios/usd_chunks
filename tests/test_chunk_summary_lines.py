"""Every chunk's description opens with a summary line: what `usdchunks list` shows.

`usdchunks list` and the create menu's search show a description's first line only, cut at
90 characters, and that line is what a person or an agent picks a chunk by. Given only that
list, an agent asked for a spiral staircase answered "no chunk does this": Duplicate Prim's
line said "deep copies a prim to a new path". With the copies named in that line ("Copies a
prim N times in a row, ring or spiral (stairs); ...") it picked it every time.

The rule: a description opens with one line of at most 90 characters, then a blank line (or
nothing more).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from usd_chunks import Scene, chunk_registry

CHUNKS = Path(__file__).resolve().parents[1] / "chunks"
LIMIT = 90


def _summary_problem(description: str) -> str:
    lines = chunk_registry.clean_description(description or "").strip().splitlines()
    if not lines:
        return "no description"
    if len(lines[0]) > LIMIT:
        return f"first line is {len(lines[0])} characters, more than {LIMIT}"
    if len(lines) > 1 and lines[1].strip():
        return "the first line runs on: a summary line needs a blank line after it"
    return ""


@pytest.fixture(scope="module")
def modules():
    with Scene() as scene:
        found = {}
        for m in scene.registry.get_chunk_modules():
            path = Path(m.module_file).resolve()
            if CHUNKS in path.parents:
                found[path.relative_to(CHUNKS).as_posix()] = m.config.description
        yield found


def test_every_chunk_opens_with_a_summary_line(modules) -> None:
    broken = {p: why for p, d in modules.items() if (why := _summary_problem(d))}
    assert not broken, "\n".join(f"{p}: {why}" for p, why in sorted(broken.items()))
