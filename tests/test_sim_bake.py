"""sim_bake: a bake reads on the main thread, solves on a worker or in a process, writes back.

Inline (headless, under test) it runs straight through. In a UI host its WORKER parts run on a
thread and the rest on the main thread, so the host keeps responding; in_subprocess moves a
solve that holds the GIL out of the process altogether.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import types

import pytest
from pxr import Gf, Sdf, Tf, Usd, UsdGeom, Vt

from usd_chunks import sim_bake


class _Node:
    """Anything a bake can hang its state on, like a chunk."""


def _wait(qapp, node, seconds=20.0):
    started = time.perf_counter()
    while sim_bake.is_running(node) and time.perf_counter() - started < seconds:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


@pytest.fixture
def threaded(qapp, monkeypatch):
    monkeypatch.setattr(sim_bake, "in_background", lambda: True)
    return qapp


def _bake(where, *, fail=False, steps=1):
    def bake(ctx):
        where.append(("read", threading.current_thread() is threading.main_thread()))
        yield sim_bake.WORKER
        for i in range(steps):
            ctx.progress(i, steps)
            yield
        if fail:
            raise ValueError("no colliders")
        where.append(("solve", threading.current_thread() is threading.main_thread()))
        yield sim_bake.MAIN
        where.append(("write", threading.current_thread() is threading.main_thread()))
        return 42

    return bake


def test_inline_runs_straight_through_and_finishes_before_returning():
    where, results, reports = [], [], []
    sim_bake.run(_Node(), _bake(where), report=reports.append, finish=results.append)
    assert results == [42]
    assert [w for w, _ in where] == ["read", "solve", "write"]


def test_in_a_ui_the_solve_runs_on_a_worker_and_the_rest_on_the_main_thread(threaded):
    node, where, results, reports = _Node(), [], [], []
    sim_bake.run(node, _bake(where), report=reports.append, finish=results.append)
    assert sim_bake.is_running(node), "run() returns while the bake goes on"
    _wait(threaded, node)
    assert results == [42]
    assert where == [("read", True), ("solve", False), ("write", True)]


def test_a_second_run_while_one_is_going_says_so(threaded):
    node, reports = _Node(), []
    sim_bake.run(node, _bake([], steps=50), report=reports.append)
    sim_bake.run(node, _bake([]), report=reports.append)
    assert "already simulating" in reports[-1]
    _wait(threaded, node)


def test_cancel_stops_it_and_nothing_is_written(threaded):
    node, where, results, reports = _Node(), [], [], []

    def slow(ctx):
        yield sim_bake.WORKER
        for i in range(10_000):
            time.sleep(0.001)
            yield
        where.append("solved")
        yield sim_bake.MAIN
        return 1

    sim_bake.run(node, slow, report=reports.append, finish=results.append)
    sim_bake.cancel(node)
    _wait(threaded, node)
    assert reports[-1] == "cancelled"
    assert results == [] and where == []
    assert not sim_bake.is_running(node)


def test_a_failing_solve_reports_and_frees_the_chunk(threaded):
    node, results, reports = _Node(), [], []
    sim_bake.run(node, _bake([], fail=True), report=reports.append, finish=results.append)
    _wait(threaded, node)
    assert reports[-1] == "simulation failed: no colliders"
    assert results == [] and not sim_bake.is_running(node)


def test_in_subprocess_runs_the_solve_in_another_process(tmp_path, monkeypatch):
    source = tmp_path / "engine.py"
    source.write_text(
        "import os\n"
        "def solve(progress, frames):\n"
        "    for f in range(frames):\n"
        "        progress(f, frames)\n"
        "    return {'pid': os.getpid(), 'frames': frames}\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("engine_for_test", source)
    engine = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "engine_for_test", engine)
    spec.loader.exec_module(engine)

    reports = []
    relay = types.SimpleNamespace(call=types.SimpleNamespace(emit=lambda fn: fn()))
    ctx = sim_bake.Context(_Node(), reports.append, None, relay)
    result = sim_bake.in_subprocess(ctx, engine.solve, frames=5)

    assert result["frames"] == 5 and result["pid"] != os.getpid()
    assert reports[-1].startswith("simulating... frame 5 of 5")


def test_in_subprocess_reports_the_childs_error(tmp_path, monkeypatch):
    source = tmp_path / "broken.py"
    source.write_text("def solve(progress):\n    raise RuntimeError('taichi is not installed')\n",
                      encoding="utf-8")
    spec = importlib.util.spec_from_file_location("broken_for_test", source)
    engine = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "broken_for_test", engine)
    spec.loader.exec_module(engine)
    relay = types.SimpleNamespace(call=types.SimpleNamespace(emit=lambda fn: fn()))
    ctx = sim_bake.Context(_Node(), lambda text: None, None, relay)
    with pytest.raises(RuntimeError, match="taichi is not installed"):
        sim_bake.in_subprocess(ctx, engine.solve)


def test_inline_in_subprocess_is_a_plain_call():
    seen = []

    def solve(progress, frames):
        progress(frames - 1, frames)
        seen.append(threading.get_ident())
        return frames

    ctx = sim_bake.Context(_Node(), lambda text: None, None)
    assert sim_bake.in_subprocess(ctx, solve, frames=3) == 3
    assert seen == [threading.get_ident()]


def test_write_samples_is_one_change_notice_for_every_frame():
    stage = Usd.Stage.CreateInMemory()
    sub = Sdf.Layer.CreateAnonymous()
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    stage.SetEditTarget(stage.GetEditTargetForLocalLayer(sub))
    points = UsdGeom.Points.Define(stage, "/Cloud").CreatePointsAttr()
    notices = []
    listener = Tf.Notice.Register(Usd.Notice.ObjectsChanged, lambda n, s: notices.append(n), stage)

    sim_bake.write_samples(points, ((f, Vt.Vec3fArray([Gf.Vec3f(f, 0, 0)])) for f in range(1, 101)),
                           default=Vt.Vec3fArray([Gf.Vec3f(100, 0, 0)]))

    listener.Revoke()
    assert len(notices) == 1
    assert points.GetTimeSamples() == [float(f) for f in range(1, 101)]
    assert points.Get(42)[0] == Gf.Vec3f(42, 0, 0)
    assert points.Get()[0] == Gf.Vec3f(100, 0, 0)
    assert sub.GetAttributeAtPath("/Cloud.points"), "written into the edit target"


def test_keep_and_restore_carry_a_bake_across_a_cleared_layer():
    node = _Node()
    layer = Sdf.Layer.CreateAnonymous()
    Sdf.CreatePrimInLayer(layer, "/Baked")
    sim_bake.keep(node, layer)
    layer.Clear()

    assert sim_bake.restore(node, layer)
    assert layer.GetPrimAtPath("/Baked")

    sim_bake.forget(node)
    layer.Clear()
    assert not sim_bake.restore(node, layer)
    assert not layer.GetPrimAtPath("/Baked")
