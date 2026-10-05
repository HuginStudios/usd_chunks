"""Auto Simulate must not re-bake for a change nothing computes from -- and re-bake for one it does.

Reported live: with `ScatterBrush -> ScatterPoints -> RBDSolver` and Auto Simulate on, touching
the brush re-ran MuJoCo over and over. The solver then learned of upstream changes from a
global CHUNK_CHANGED listener, which heard every chunk -- including changes the dirty path had
already dismissed as presentation only.

Chunks now speak to each other only through wires (chunk_notice: notices stay within a chunk),
so there is no listener to fool. An upstream change reaches the solver as a cook, through its
wires, and only when it is a data change; a brush stroke cooks nothing below the brush. The
solver takes any cook its own controls did not ask for as an input having changed
(_note_input_change).
"""
from __future__ import annotations

import importlib

import pytest

from usd_chunks import Scene, chunk, chunk_cook
from chunk_stubs import register_stub_module

rbd = importlib.import_module("chunks.dynamics.rbd_solver")


@pytest.fixture(autouse=True)
def _scene():
    with Scene() as scene:
        yield scene


def _solver(auto_sim: bool):
    """A solver as main() builds it, not stale yet."""
    solver = rbd.main()
    # Not stale first: turning Auto Simulate on for a stale solver schedules a run, rightly.
    solver.unset_state(chunk.ChunkFlags.State.STALE)
    solver.get_param("auto_sim").value = auto_sim
    # Setting Auto Simulate is a control's change, which marks the cook it brings as ours.
    solver._rbd_controls_cook = False
    return solver


@pytest.fixture
def scheduled(monkeypatch):
    """The re-bakes Auto Simulate asks for, recorded instead of run."""
    runs = []
    monkeypatch.setattr(rbd, "_schedule_auto_run", lambda c: runs.append(c))
    return runs


# ---------------------------------------------------------------------------
# The solver's own reading of a cook
# ---------------------------------------------------------------------------

def test_a_cook_no_control_asked_for_marks_the_solver_stale(scheduled) -> None:
    solver = _solver(auto_sim=True)

    rbd._note_input_change(solver)

    assert solver.has_state(chunk.ChunkFlags.State.STALE)
    assert scheduled == [solver], "Auto Simulate re-bakes for an input change"


def test_without_auto_simulate_it_is_only_marked(scheduled) -> None:
    solver = _solver(auto_sim=False)

    rbd._note_input_change(solver)

    assert solver.has_state(chunk.ChunkFlags.State.STALE)
    assert scheduled == []


def test_a_controls_own_cook_is_not_an_input_change(scheduled) -> None:
    """Run, Reset, Auto Simulate: a control cooks the chunk like any param, and is no input."""
    solver = _solver(auto_sim=True)
    solver._rbd_controls_cook = True

    rbd._note_input_change(solver)

    assert not solver.has_state(chunk.ChunkFlags.State.STALE)
    assert scheduled == []
    # Said for that one cook only: the next is an input again.
    rbd._note_input_change(solver)
    assert solver.has_state(chunk.ChunkFlags.State.STALE)


def test_the_solver_listens_to_no_other_chunk() -> None:
    """main() registers no global listener for chunk notices -- add_callback would refuse one."""
    solver = rbd.main()

    for data in solver._callbacks.callbacks:
        conn = data["conn"]
        assert not conn.globally, f"{conn.get_method()} listens to every chunk"


# ---------------------------------------------------------------------------
# End to end: what reaches a chunk wired below the brush
# ---------------------------------------------------------------------------

def _below_the_brush(scene, monkeypatch):
    """A Scatter Brush with a chunk wired below it -- the solver's place -- and what reaches it.

    Reaching, not cooking: a chunk that reads nothing the brush wrote is skipped (skip_cook),
    as the solver would be. What matters is whether the change gets to it at all.
    """
    cooks = []
    below = register_stub_module()
    brush = scene.create.scatter_brush()
    (c,) = below.load()
    scene.add_chunk(c)
    c.add_dependency(brush)
    # Wiring marks both dirty without solving; settle it, so what follows is the edit alone.
    brush.execute()
    dispatch = chunk_cook.dispatch

    def reached(chunk_, *args):
        if chunk_ is c:
            cooks.append(chunk_.name)
        return dispatch(chunk_, *args)

    monkeypatch.setattr(chunk_cook, "dispatch", reached)
    return brush, c, cooks


def test_a_brush_setting_does_not_reach_below_the_brush(_scene, monkeypatch) -> None:
    """The reported bug, end to end: a brush stroke must not reach the solver at all."""
    brush, _below, reached = _below_the_brush(_scene, monkeypatch)

    brush.get_param("radius").value = 27.0

    assert reached == [], "a presentation-only brush setting reached the chunk below it"


def test_a_real_brush_change_still_reaches_below_it(_scene, monkeypatch) -> None:
    """The guard must not cost a legitimate re-run: a simulation built on a stale input is
    wrong, where an extra bake is only slow."""
    brush, below, reached = _below_the_brush(_scene, monkeypatch)

    brush.get_param("surface_prim").value = "/World/SomewhereElse"

    assert reached == [below.name], "a real upstream change did not reach the chunk below"
