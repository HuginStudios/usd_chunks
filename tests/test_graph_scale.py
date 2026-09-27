"""Scale benchmark for a 1000-node chunk graph.

Two tiers, because the cost of a large graph is not where you would first look for it:

* **Scheduler tier** — 1000 bare propagators wired into a chain, exercising
  `chunk_scheduler` alone (dirty propagation, Kahn sort, solve, depth). No USD, no Qt.
  This is the part people assume is the bottleneck. It is not: it is linear and finishes
  a 1000-node chain in tens of milliseconds.

* **Full-stack tier** — 1000 real `dot` chunks (the cheapest module there is: its
  `execute` clears its own layer and returns) created through the registry and wired
  through `add_dependency`, so every structural change drags the real USD bookkeeping
  behind it. This is where the time goes, and it does not go there linearly.

The full-stack tier is opt-in (`USD_CHUNKS_SCALE=1`): a 1000-node build is a couple of
minutes, which is worth having on demand but not on every suite run. Run the sweep
directly to get the phase table and the fitted growth exponent per phase:

    USD_CHUNK_DIRS=scripts python tests/test_graph_scale.py --sweep 25,50,100,200
    USD_CHUNK_DIRS=scripts python tests/test_graph_scale.py --n 1000

`--sweep` is the useful mode: absolute numbers move with the machine, but the exponent
(how the phase grows as N doubles) is what says whether a phase is linear, quadratic or
cubic, and that is stable across machines.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable

import pytest
from pxr import Usd

from usd_chunks import chunk_registry, chunk_scheduler
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_notice import ChunkNoticeAlias, ChunkNoticePropagator
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import initialize as chunk_usd_init

# The scene/stage the last build_chunk_graph() ran against, so the reload tier can export
# the graph it just built without build_chunk_graph having to return anything but timings.
_ACTIVE: dict = {}

NODE_COUNT = 1000

# The full-stack tier is minutes at 1000 nodes -- fine to ask for, too slow to make every
# suite run pay for.
SCALE_TESTS_ENABLED = os.environ.get("USD_CHUNKS_SCALE") == "1"


class _ChainNode(ChunkNoticePropagator):
    """A propagator with nothing but a name — the scheduler tier's unit of work."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    def get_notice_alias(self):
        return ChunkNoticeAlias.VALUE_CHANGED

    def is_blocked(self):
        return False


class _BenchScene(SceneSender):
    """A SceneSender over a single in-memory stage, with no selection and no host."""

    def __init__(self, stage: Usd.Stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _time(fn: Callable[[], object]) -> tuple[float, object]:
    """Run fn and return (seconds, result)."""
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def build_scheduler_chain(count: int) -> list[_ChainNode]:
    """A linear chain of count propagators, each depending on the one before it."""
    nodes = [_ChainNode(f"n{i}") for i in range(count)]
    for previous, current in zip(nodes, nodes[1:]):
        current.add_dependency(previous)
    return nodes


def scheduler_phases(count: int) -> list[tuple[str, float]]:
    """Time each scheduler operation over a chain of count nodes."""
    build, nodes = _time(lambda: build_scheduler_chain(count))
    head = nodes[0].get_dag_node()
    tail = nodes[-1].get_dag_node()

    dirty, _ = _time(lambda: chunk_scheduler.propagate_dirty(head))
    solve, solved = _time(lambda: sum(1 for _ in chunk_scheduler.solve(head)))
    depth, _ = _time(lambda: chunk_scheduler.get_depth(tail))

    assert solved == count, f"solve visited {solved} of {count} nodes"
    return [("build+wire", build), ("propagate_dirty", dirty), ("solve", solve), ("get_depth", depth)]


def build_chunk_graph(count: int) -> list[tuple[str, float]]:
    """Create count `dot` chunks on a fresh stage, chain them, and time each phase.

    `dot` is chosen because its module body does nothing but clear its own layer: whatever
    this measures is framework overhead, not the cost of a chunk doing real work.
    """
    # The layer manager is a singleton and keeps every graph/chunk layer it has ever
    # handed out. Without this reset a sweep measures each point against the accumulated
    # layers of all the ones before it, which reads exactly like superlinear growth in
    # the code under test. Reset first, so each point starts from an empty layer stack.
    chunk_registry.ChunkLayerManager().reset()

    stage = Usd.Stage.CreateInMemory()
    scene = _BenchScene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "scale-bench")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_init(stage, scene)

    module = registry.get_chunk_module("dot")
    if not module:
        pytest.skip("USD_CHUNK_DIRS must point at scripts/ for the dot chunk to be found")

    _ACTIVE["scene"] = scene
    _ACTIVE["stage"] = stage

    def create() -> list:
        chunks = []
        for _ in range(count):
            created = module.create() if module.loaded else module.load()
            for chunk in created:
                scene.add_chunk(chunk)
            chunks.append(created[0])
        return chunks

    create_time, chunks = _time(create)

    def wire() -> None:
        for previous, current in zip(chunks, chunks[1:]):
            current.add_dependency(previous)

    wire_time, _ = _time(wire)
    # Solving from the head walks the whole chain; solving from the tail should touch
    # almost nothing, and the gap between the two is what says whether a solve is
    # correctly scoped to the dirty frontier.
    head_solve, _ = _time(chunks[0].execute)
    tail_solve, _ = _time(chunks[-1].execute)

    return [
        ("create", create_time),
        ("wire", wire_time),
        ("solve from head", head_solve),
        ("solve from tail", tail_solve),
    ]


def reload_chunk_graph(count: int, directory: str) -> list[tuple[str, float]]:
    """Build count chained chunks, export them, then time opening that file back up.

    A different cost to build_chunk_graph and the one that actually matters for a saved
    chunkgroup: nothing here goes through the interactive create-one-then-wire-one path, so
    none of the per-edge structural bookkeeping fires. load_chunks walks the file's chunk
    prims once and rebuilds the graph from what is already authored there.
    """
    build_chunk_graph(count)
    scene = _ACTIVE["scene"]
    export_path = os.path.join(directory, f"chain_{count}.usda")
    export, _ = _time(lambda: chunk_stage.export_chunks(scene, export_path))

    # A brand-new stage and scene, as if the file were being opened cold.
    chunk_registry.ChunkLayerManager().reset()
    stage = Usd.Stage.Open(export_path)
    fresh = _BenchScene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(fresh, "scale-bench")
    chunk_registry.ChunkLayerManager().initialize(stage)

    # chunk_stage.initialize is what loads the file — it runs load_chunks against the sync's
    # own path bookkeeping. Calling load_chunks again afterwards with a fresh set does not
    # measure a second open, it builds a duplicate of every chunk already there.
    load, _ = _time(lambda: chunk_usd_init(stage, fresh))
    loaded = fresh.get_chunks()
    assert len(loaded) >= count, f"loaded {len(loaded)} chunks, expected {count}"

    # Cook the whole restored graph from its head, the same measurement build_chunk_graph
    # takes, so the two tiers are directly comparable.
    head = min(loaded, key=lambda c: chunk_scheduler.get_depth(c.get_dag_node()))
    execute, _ = _time(head.execute)

    return [("export", export), ("load", load), ("exec loaded", execute)]


def test_scheduler_solves_1000_connected_nodes():
    """The DAG engine itself handles a 1000-node chain comfortably.

    Guards the scheduler against a regression back to the per-path recursive walk it
    replaced, which went exponential on shapes this size. The threshold is deliberately
    loose — it is there to catch an algorithmic regression, not to police milliseconds on
    a busy CI box.
    """
    phases = scheduler_phases(NODE_COUNT)
    total = sum(seconds for _, seconds in phases)

    assert total < 5.0, f"1000-node scheduler chain took {total:.3f}s: {_format(phases)}"


def _best_of(fn: Callable[[], object], runs: int = 3) -> float:
    """Seconds for the fastest of runs calls to fn.

    Noise on a busy machine only ever adds time, never subtracts it, so the fastest
    sample is the one closest to what the code actually costs — a single timing measures
    the box as much as the code.
    """
    return min(_time(fn)[0] for _ in range(runs))


def test_scheduler_solve_is_linear_in_node_count():
    """Doubling the node count must not more than roughly double the solve.

    Ratio rather than absolute time, so the assertion means the same thing on any
    machine. A quadratic regression shows up here as ~4x.

    Each side is the best of three runs rather than a single timing: measured on an idle
    machine the honest ratio is ~2.1 (constant factors, not shape) with single timings
    reaching 2.7, which left the 3x threshold almost no room. Inside the full suite that
    tipped over often enough to fail roughly one run in three while passing every time in
    isolation. Best-of-three brings the spread back under 2.4 and leaves 3x where it was,
    between honest growth and the ~4x a quadratic regression would show.
    """
    small = _best_of(lambda: sum(1 for _ in _solve_fresh_chain(500)))
    large = _best_of(lambda: sum(1 for _ in _solve_fresh_chain(1000)))

    assert large < small * 3.0, f"solve grew {large / small:.1f}x when node count doubled"


def _solve_fresh_chain(count: int):
    """Build a chain of count nodes and return the solve generator, fully consumed."""
    nodes = build_scheduler_chain(count)
    head = nodes[0].get_dag_node()
    chunk_scheduler.propagate_dirty(head)
    return list(chunk_scheduler.solve(head))


@pytest.mark.skipif(not SCALE_TESTS_ENABLED, reason="set USD_CHUNKS_SCALE=1 to run the full-stack scale test")
def test_chunk_graph_builds_1000_connected_nodes():
    """1000 real chunks, chained, end to end.

    Guards the fix that made this reachable at all: `reconcile_edit_targets` used to re-run
    over every chunk in the scene on every single wire, making the wiring phase cubic in the
    node count — 4+ hours at this size. Scoped to the chunks downstream of the new edge it is
    quadratic and measured in seconds.

    The threshold is several times the measured time, so it fails on a return to cubic
    growth rather than on a slow machine.
    """
    phases = build_chunk_graph(NODE_COUNT)
    total = sum(seconds for _, seconds in phases)

    assert total < 600.0, f"1000-node chunk graph took {total:.1f}s: {_format(phases)}"


def _format(phases: list[tuple[str, float]]) -> str:
    return ", ".join(f"{name}={seconds:.3f}s" for name, seconds in phases)


def _sweep(counts: list[int], builder: Callable[[int], list[tuple[str, float]]], title: str) -> None:
    """Run builder at each count and print a phase table plus the fitted growth exponent.

    The exponent is a straight log-log slope between the smallest and largest run: 1.0 is
    linear, 2.0 quadratic, 3.0 cubic. It is the number worth reading — it says whether a
    phase will survive being asked for ten times as many nodes.
    """
    print(f"\n=== {title} ===")
    results = {}
    for count in counts:
        phases = builder(count)
        results[count] = dict(phases)
        print(f"N={count:>5}  " + "  ".join(f"{name}={seconds:7.3f}s" for name, seconds in phases))

    if len(counts) < 2:
        return

    first, last = counts[0], counts[-1]
    span = math.log(last / first)
    print(f"\ngrowth exponent ({first} -> {last}; 1=linear, 2=quadratic, 3=cubic)")
    print(f"  {'phase':<16} {'exponent':>8}  {'projected @' + str(NODE_COUNT):>18}")
    for name in results[last]:
        small, large = results[first][name], results[last][name]
        if small <= 0 or large <= 0:
            continue
        exponent = math.log(large / small) / span
        projected = large * (NODE_COUNT / last) ** exponent
        print(f"  {name:<16} {exponent:8.2f}  {_duration(projected):>18}")


def _duration(seconds: float) -> str:
    """Seconds as something readable, since the projections span milliseconds to hours."""
    if seconds < 90:
        return f"{seconds:.1f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.1f}h"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, help="run a single full-stack build at this node count")
    parser.add_argument("--sweep", help="comma-separated node counts to sweep the full stack over")
    parser.add_argument("--scheduler", action="store_true", help="sweep the scheduler tier instead")
    parser.add_argument("--reload", action="store_true", help="time exporting and reopening the graph")
    args = parser.parse_args()

    if args.reload:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            counts = [int(n) for n in args.sweep.split(",")] if args.sweep else [args.n or NODE_COUNT]
            _sweep(counts, lambda n: reload_chunk_graph(n, tmp), "save + reopen (chunkgroup file)")
    elif args.scheduler:
        _sweep([250, 500, 1000, 2000], scheduler_phases, "scheduler tier (no USD)")
    elif args.sweep:
        _sweep([int(n) for n in args.sweep.split(",")], build_chunk_graph, "full stack (chunks + USD)")
    else:
        _sweep([args.n or NODE_COUNT], build_chunk_graph, "full stack (chunks + USD)")
