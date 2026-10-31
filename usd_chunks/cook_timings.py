"""How long each chunk's cook took, kept while `USD_CHUNKS_TIMINGS` is on.

Off by default: a cook then pays one boolean check. On (`USD_CHUNKS_TIMINGS=1`, or
`usdchunks run --timings`), every cook that runs its module is recorded with its total time
and its own time -- the total less any cook started inside it, so a chunk that cooks its
upstream does not take the blame for it. Each is logged as it finishes, which is what a
host like usdview shows; `report()` sums them per chunk, slowest first, for the CLI.

Phases outside the cooks (opening the stage, loading the network) are the caller's to time;
`phase(name)` records one beside the cooks so a report shows both.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager

_log = logging.getLogger(__name__)

enabled = os.environ.get("USD_CHUNKS_TIMINGS", "0") not in ("", "0")

# One entry per cook that ran: (chunk path or name, total seconds, own seconds).
_cooks: list = []

# Named phases timed with phase(), in the order they ran: (name, seconds).
_phases: list = []

# For each cook running now, the time spent in cooks started inside it so far.
_nested: list = []


@contextmanager
def timed(chunk, step="cook"):
    """Record chunk's cook, when timings are on; the body is the cook.

    step="commit" records its commit hook instead (chunk_scheduler.commit_all), listed apart
    from its cook: a chunk that writes a file or renders does it there.
    """
    if not enabled:
        yield
        return
    _nested.append(0.0)
    started = time.perf_counter()
    try:
        yield
    finally:
        total = time.perf_counter() - started
        inner = _nested.pop()
        if _nested:
            _nested[-1] += total
        path = getattr(chunk, "prim_path", None) or chunk.name
        _cooks.append((path if step == "cook" else f"{path} ({step})", total, total - inner))
        _log.info("%s %s in %.3f s (own %.3f s)", "Cooked" if step == "cook" else "Committed",
                  path, total, total - inner)


@contextmanager
def phase(name):
    """Record the body as a named phase, when timings are on."""
    if not enabled:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        _phases.append((name, time.perf_counter() - started))


def clear():
    """Forget everything recorded so far."""
    _cooks.clear()
    _phases.clear()


def by_chunk():
    """Each chunk's (path, cooks, total seconds, own seconds), the most own time first."""
    sums = {}
    for path, total, own in _cooks:
        count, t, o = sums.get(path, (0, 0.0, 0.0))
        sums[path] = (count + 1, t + total, o + own)
    return sorted(((p, *v) for p, v in sums.items()), key=lambda row: -row[3])


def report(limit=20):
    """The phases, then the slowest chunks by their own time, as text."""
    lines = []
    if _phases:
        lines.append("Phases")
        lines += [f"  {name:<36} {secs:9.2f} s" for name, secs in _phases]
    rows = by_chunk()
    if rows:
        own_sum = sum(row[3] for row in rows)
        lines.append(f"Cooks and commits ({len(_cooks)} in {len(rows)} rows, {own_sum:.2f} s)")
        lines.append(f"  {'chunk':<36} {'own':>9}   {'total':>9}   cooks")
        for path, count, total, own in rows[:limit]:
            lines.append(f"  {path[-36:]:<36} {own:9.2f} s {total:9.2f} s   {count}")
        if len(rows) > limit:
            rest = sum(row[3] for row in rows[limit:])
            lines.append(f"  {f'... {len(rows) - limit} more':<36} {rest:9.2f} s")
    return "\n".join(lines)
