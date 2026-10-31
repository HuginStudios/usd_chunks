"""USD_CHUNKS_TIMINGS: each cook's time, its own time apart from cooks inside it, and a report.

Off, nothing is recorded. On, a cook that runs its module is timed where every cook runs
(chunk_cook's run), so a chunk is never missed or misnamed -- the render a network ends in
included.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from usd_chunks import Scene, chunk_scheduler, cook_timings


@pytest.fixture
def timings(monkeypatch):
    monkeypatch.setattr(cook_timings, "enabled", True)
    cook_timings.clear()
    yield cook_timings
    cook_timings.clear()


def test_off_records_nothing(monkeypatch) -> None:
    monkeypatch.setattr(cook_timings, "enabled", False)
    cook_timings.clear()
    with cook_timings.timed(SimpleNamespace(name="a", prim_path="/a")), cook_timings.phase("p"):
        pass
    assert cook_timings.by_chunk() == []
    assert cook_timings.report() == ""


def test_a_cook_inside_another_is_not_counted_as_the_outer_ones_own(timings) -> None:
    with timings.timed(SimpleNamespace(name="outer", prim_path="/outer")):
        time.sleep(0.05)
        with timings.timed(SimpleNamespace(name="inner", prim_path="/inner")):
            time.sleep(0.1)
    rows = {path: (count, total, own) for path, count, total, own in timings.by_chunk()}
    assert rows["/inner"][2] >= 0.1
    assert rows["/outer"][1] >= 0.15
    assert 0.05 <= rows["/outer"][2] < 0.1


def test_a_commit_is_recorded_apart_from_the_cook(timings) -> None:
    # A Render chunk renders in its commit hook, so that is where its time is.
    chunk = SimpleNamespace(name="Render", prim_path="/chunks/Render", is_blocked=lambda: False,
                            _module_hook=lambda name: (lambda node: time.sleep(0.05)) if name == "commit" else None)
    chunk_scheduler.commit_all([chunk], order=list)
    rows = {path: own for path, count, total, own in timings.by_chunk()}
    assert rows["/chunks/Render (commit)"] >= 0.05


def test_a_scene_records_each_chunk_it_cooks(timings) -> None:
    with Scene() as scene:
        scene.create.preview_surface(mat_path="/look/glass", roughness=0.4, name="glass")
        scene.get_chunk("glass").execute()
    paths = [row[0] for row in timings.by_chunk()]
    assert any(path.endswith("glass") for path in paths)
    with timings.phase("load network"):
        pass
    report = timings.report()
    assert "load network" in report and "glass" in report
