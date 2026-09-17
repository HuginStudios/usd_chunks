"""Auto Simulate must not re-bake for a change nothing computes from.

Reported live: with `ScatterBrush -> ScatterPoints -> RBDSolver` and Auto Simulate on, touching
the brush re-ran MuJoCo over and over. Fine with Auto Simulate off.

A watch on `propagate_dirty` in that session showed *every* propagation coming back
`presentation=True` and not one as a data change. So nothing was dirtying the chain -- the
presentation-only routing was working -- and the solver was re-baking off its **global
CHUNK_CHANGED listener** instead, which hears changes the dirty path had already dismissed.

**The timing is the whole difficulty, and is why a first attempt was reverted.** The record
these tests read (`DagNode._changes`) is cleared in a `finally` once the entire solve finishes,
and notices fire *during* the solve. Asking afterwards therefore returns an empty record for
every case -- so the earlier test reported "presentation" for nothing at all and would have
passed whether the guard worked or not.

These drive a real notice and ask from inside it, which is where the solver asks.
"""
from __future__ import annotations

import importlib


from pxr import Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


chunk_registry.ChunkRegistry().initialize(_DummyScene(), "pytest")

rbd = importlib.import_module("scripts.dynamics.rbd_solver")


def _probe(source):
    """What the solver's listener would see, asked at the moment it would ask.

    A global CHUNK_CHANGED listener, exactly like `_on_any_chunk_changed`, recording the
    verdict for each notice it hears from `source`. Reading `_only_presentation_changed` from
    here is the only place the answer means anything.
    """
    seen = []
    watcher = chunk.Chunk("Watcher")

    def _heard(owner, notice, sender):
        if sender is source:
            seen.append(rbd._only_presentation_changed(sender))

    watcher.add_callback(
        _heard,
        notice_alias=chunk.ChunkNoticeAlias.CHUNK_CHANGED,
        listen_globally=True,
        send_on_create=False,
    )
    # Held deliberately: a NoticeConnection does not keep its callback alive, and `_heard` is
    # a closure that dies with this function otherwise. It fired perfectly in a scratch script
    # where it happened to be module scope, and silently never once from here.
    watcher._probe_callback = _heard
    return seen, watcher


def _brush():
    brush_mod = importlib.import_module("scripts.modelling.scatter_brush")
    c = brush_mod.main()
    c._module = brush_mod
    return c


# ---------------------------------------------------------------------------

def test_a_presentation_only_change_is_recognised_from_inside_the_notice() -> None:
    """The case that was re-baking. Brush settings steer a *stroke*, which happens outside any
    cook -- nothing downstream computes from them, and the framework already says so."""
    c = _brush()
    seen, _watcher = _probe(c)

    c.get_param("radius").value = 23.0

    assert seen, "no CHUNK_CHANGED reached the listener at all"
    assert all(seen), f"a brush setting read as a real input: {seen}"


def test_a_real_input_still_re_runs_the_simulation() -> None:
    """The guard has to be earned per change. Which mesh is painted is a genuine input, and a
    simulation built on a stale one is wrong rather than merely slow."""
    c = _brush()
    seen, _watcher = _probe(c)

    c.get_param("surface_prim").value = "/World/SomewhereElse"

    assert seen, "no CHUNK_CHANGED reached the listener at all"
    assert not all(seen), "a real input was dismissed as presentation only"


def test_the_verdict_is_only_meaningful_inside_the_notice() -> None:
    """Pinning the trap that reverted the first attempt.

    `_changes` is cleared once the solve finishes, so the same question asked afterwards
    returns False for everything -- including the case the guard exists for. A test that reads
    it late passes whether the fix is present or not, which is worse than one that fails.
    """
    c = _brush()
    seen, _watcher = _probe(c)

    c.get_param("radius").value = 31.0
    inside = list(seen)
    after = rbd._only_presentation_changed(c)

    assert all(inside), "the setup is wrong if this was not presentation-only in the notice"
    assert after is False, (
        "asking after the solve now agrees with asking inside it -- this test is the reason "
        "the first attempt was reverted, so if it fails, check whether the guard still needs "
        "to run inside the notice"
    )


def test_an_unreadable_sender_is_assumed_to_matter() -> None:
    """The safe default, and the direction to be wrong in: a missed re-run is a wrong
    simulation, an extra one is only slow."""

    class _Bare:
        def get_dag_node(self):
            raise RuntimeError("no node")

    assert rbd._only_presentation_changed(_Bare()) is False


def test_a_sender_with_nothing_recorded_is_assumed_to_matter() -> None:
    class _Empty:
        def get_dag_node(self):
            class _Node:
                def only_presentation_changed(self):
                    # What DagNode returns with nothing recorded. Kept as the real method
                    # rather than an empty iter_changes(), or this passes through the
                    # unreadable-sender except branch and stops testing anything.
                    return False

            return _Node()

    assert rbd._only_presentation_changed(_Empty()) is False


# ---------------------------------------------------------------------------
# and the listener has to actually consult it
# ---------------------------------------------------------------------------

def _solver_hearing(source, monkeypatch):
    """Drive `_on_any_chunk_changed` from inside a real notice, and record what it decides.

    The predicate tests above pass whether or not the listener calls it -- they exercise the
    verdict, not its use. This drives the listener itself, at the only moment its answer is
    meaningful, and watches for the one thing that costs: a scheduled re-bake.
    """
    stage = Usd.Stage.CreateInMemory()

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_Staged(), "pytest")

    solver = chunk.Chunk("RBDSolver")
    solver.add_param(chunk.Checkbox("auto_sim", "Auto Simulate", default_value=True))

    # The wiring the listener exists for. Real graph connections are USD-level and not worth
    # standing up here; what matters is that the brush counts as upstream.
    monkeypatch.setattr(chunk_stage, "view_cook_list", lambda *a, **k: [source])
    scheduled = []
    monkeypatch.setattr(rbd, "_schedule_auto_run", lambda c: scheduled.append(c))

    watcher = chunk.Chunk("Watcher")

    def _heard(owner, notice, sender):
        if sender is source:
            rbd._on_any_chunk_changed(solver, notice, sender)

    watcher.add_callback(
        _heard,
        notice_alias=chunk.ChunkNoticeAlias.CHUNK_CHANGED,
        listen_globally=True,
        send_on_create=False,
    )
    watcher._probe_callback = _heard
    return scheduled, solver, watcher


def test_the_listener_does_not_re_bake_for_a_presentation_only_change(monkeypatch) -> None:
    """The reported bug, end to end: with Auto Simulate on, a brush setting upstream re-ran
    MuJoCo."""
    c = _brush()
    scheduled, solver, _watcher = _solver_hearing(c, monkeypatch)

    c.get_param("radius").value = 27.0

    assert scheduled == [], "a brush setting scheduled a re-bake"
    assert not solver.has_state(chunk.ChunkFlags.State.STALE), "and it marked the solver stale"


def test_the_listener_still_re_bakes_for_a_real_change(monkeypatch) -> None:
    """The guard must not cost a legitimate re-run: a simulation built on a stale input is
    wrong, where an extra bake is only slow."""
    c = _brush()
    scheduled, solver, _watcher = _solver_hearing(c, monkeypatch)

    c.get_param("surface_prim").value = "/World/SomewhereElse"

    assert scheduled, "a real upstream change did not re-run the simulation"
    assert solver.has_state(chunk.ChunkFlags.State.STALE)
