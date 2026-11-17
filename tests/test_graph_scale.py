"""Scale benchmark for a 1000-node chunk graph.

Two tiers, because the cost of a large graph is not where you would first look for it:

* **Scheduler tier** -- 1000 bare propagators wired into a chain, exercising
  `chunk_scheduler` alone (dirty propagation, Kahn sort, solve, depth). No USD, no Qt.
  This is the part people assume is the bottleneck. It is not: it is linear and finishes
  a 1000-node chain in tens of milliseconds.

* **Network tier** -- real networks in three shapes, built, saved, opened and edited at
  two sizes, each phase checked by how it grows, never by its seconds: a deep chain of
  Transforms on one prim, a wide fan of moved cubes into one Merge, and a MaterialX shader
  tree (skipped where USD has no MaterialX, as in CI). Runs on every suite: small sizes,
  seconds each, but enough to catch a cook whose cost grew with the network again.

* **Full-stack tier** -- 1000 real `dot` chunks (the cheapest module there is: its
  `execute` clears its own layer and returns) created through the registry and wired
  through `add_dependency`, so every structural change drags the real USD bookkeeping
  behind it. This is where the time goes, and it does not go there linearly.

The full-stack tier is opt-in (`USD_CHUNKS_SCALE=1`): a 1000-node build is a couple of
minutes, which is worth having on demand but not on every suite run. Run the sweep
directly to get the phase table and the fitted growth exponent per phase:

    USD_CHUNK_DIRS=chunks python tests/test_graph_scale.py --sweep 25,50,100,200
    USD_CHUNK_DIRS=chunks python tests/test_graph_scale.py --n 1000

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

from usd_chunks import Scene, chunk, chunk_registry, chunk_scheduler, shader_defs
from usd_chunks.chunk_scene import SceneSender
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
    """A propagator with nothing but a name -- the scheduler tier's unit of work."""

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
        pytest.skip("USD_CHUNK_DIRS must point at chunks/ for the dot chunk to be found")

    _ACTIVE["scene"] = scene
    _ACTIVE["stage"] = stage

    def create() -> list:
        chunks = []
        for _ in range(count):
            created = module.create() if module.loaded else module.load()
            for made in created:
                scene.add_chunk(made)
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

    # chunk_stage.initialize is what loads the file -- it runs load_chunks against the sync's
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


# ------------------------------------------------------------------------------------------
# Network tier
# ------------------------------------------------------------------------------------------

#: The two sizes each network is measured at. Four times apart, so a phase that should be
#: linear grows about 4x and a quadratic one about 16x.
SMALL, LARGE = 25, 100

#: The most each phase may grow from SMALL to LARGE, as an exponent of the size ratio: 1 is
#: linear, 2 quadratic. Between what main measures and what the code before the November 2026
#: fixes did (#684-#696), with room above main for a slower, noisier CI runner. Measured, best
#: of 3 (build, open, leaf edit, root edit):
#:
#:   chain  main 1.38 1.32 0.55 1.55   before 1.91 2.22 1.79 2.79
#:   fan    main 1.19 1.13 0.30 0.30   before 1.20 1.15 0.33 0.30  (always linear: no shared prim)
#:   mtlx   main 1.14 1.24 0.30 0.35   before 1.48 1.52 0.84 0.60
#:
#: A chain's root edit is near quadratic by its shape: every Transform below cooks, and each
#: writes an op order as long as the chain.
MAX_EXPONENT = {
    "chain": {"build": 1.65, "open": 1.65, "edit leaf": 1.0, "edit root": 2.1},
    "fan": {"build": 1.5, "open": 1.5, "edit leaf": 0.7, "edit root": 0.7},
    "mtlx": {"build": 1.4, "open": 1.45, "edit leaf": 0.65, "edit root": 0.7},
}


def _chain(scene, n: int) -> dict:
    """A cube and n Transforms below it, all moving the same prim: deep, one prim."""
    with chunk.change_block():
        last = scene.create.cube(name="Box", prim_path="/World/box")
        first = None
        for i in range(n):
            last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=0.01)
            first = first or last
        scene.set_output(last)
    return {"leaf": (last.name, "ty", 0.02), "root": (first.name, "ty", 0.02)}


def _fan(scene, n: int) -> dict:
    """n cubes, each moved by a Transform, into one Merge: wide, a prim each."""
    with chunk.change_block():
        moved = []
        for i in range(n):
            cube = scene.create.cube(name=f"Box{i}", prim_path=f"/World/box{i}")
            moved.append(scene.create.transform(cube, name=f"T{i}", primpath_location=f"/World/box{i}", tx=i))
        merge = scene.create.merge(*moved, name="Merge")
        scene.set_output(merge)
    return {"leaf": (moved[0].name, "ty", 1.0), "root": ("Box0", "width", 1.5)}


def _shader_tree(scene, n: int) -> dict:
    """A tree of n MaterialX add nodes over constant leaves, into one Standard Surface."""
    with chunk.change_block():
        sphere = scene.create.sphere(name="Ball", prim_path="/World/ball", radius1=1.0)
        level = [scene.add("mtlx_shader", name=f"Const{i}", nodedef="ND_constant_color3",
                           value=(0.1 * (i % 10), 0.5, 0.5)) for i in range(max(2, n // 2))]
        leaf = level[0]
        while len(level) > 1:
            level = [scene.add("mtlx_shader", level[j], level[j + 1], name=f"Add{len(level)}_{j}",
                               nodedef="ND_add_color3")
                     for j in range(0, len(level) - 1, 2)] + level[len(level) - len(level) % 2:]
        surface = scene.add("mtlx_shader", level[0], sphere, name="Surface",
                            nodedef="ND_standard_surface_surfaceshader", bind_to="/World/ball")
        scene.set_output(surface)
    return {"leaf": (surface.name, "specular_roughness", 0.5), "root": (leaf.name, "value", (0.9, 0.2, 0.1))}


_SHAPES = {"chain": _chain, "fan": _fan, "mtlx": _shader_tree}


def network_phases(shape: str, n: int, directory: str) -> dict[str, float]:
    """Build a network of shape at size n, save it, open it, edit it: seconds per phase.

    Opened as the suite runs, with the open cache off (conftest): this measures what a
    cook costs, which reading results back would hide.
    """
    path = os.path.join(directory, f"{shape}_{n}.usda")
    phases = {}
    with Scene.new() as scene:
        phases["build"], edits = _time(lambda: _SHAPES[shape](scene, n))
        scene.save(path)
    phases["open"], scene = _time(lambda: Scene.open(path))
    try:
        for key in ("leaf", "root"):
            name, param, value = edits[key]
            target = scene.get_chunk(name)
            # Twice, timed the second: the first edit after opening pays first-time costs.
            scene.set(target, **{param: value})
            nudged = tuple(v * 0.9 for v in value) if isinstance(value, tuple) else value * 0.9
            phases[f"edit {key}"], _ = _time(lambda: scene.set(target, **{param: nudged}))
    finally:
        scene.close()
    return phases


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_no_phase_grows_faster_than_it_should(shape, tmp_path, monkeypatch):
    """Each phase grows with the network as it should: a leaf edit not at all, the rest linearly.

    Best of three at each size, as the scheduler test does: noise only ever adds time.
    """
    if shape == "mtlx" and "mtlx" not in shader_defs.available_source_types():
        pytest.skip("this USD has no MaterialX shader definitions (no usdMtlx plugin)")
    monkeypatch.setenv("USD_CHUNKS_OPEN_CACHE", "0")

    def best(n: int) -> dict[str, float]:
        runs = [network_phases(shape, n, str(tmp_path)) for _ in range(3)]
        return {phase: min(run[phase] for run in runs) for phase in runs[0]}

    small, large = best(SMALL), best(LARGE)
    span = math.log(LARGE / SMALL)
    grew = {phase: math.log(max(large[phase], 1e-4) / max(small[phase], 1e-4)) / span for phase in small}
    too_fast = {phase: round(e, 2) for phase, e in grew.items() if e > MAX_EXPONENT[shape][phase]}

    assert not too_fast, (
        f"{shape}: grew faster than allowed {too_fast} (exponents {({p: round(e, 2) for p, e in grew.items()})}; "
        f"seconds at {SMALL}: {_format(sorted(small.items()))}; at {LARGE}: {_format(sorted(large.items()))})"
    )


def test_scheduler_solves_1000_connected_nodes():
    """The DAG engine itself handles a 1000-node chain comfortably.

    Guards the scheduler against a regression back to the per-path recursive walk it
    replaced, which went exponential on shapes this size. The threshold is deliberately
    loose -- it is there to catch an algorithmic regression, not to police milliseconds on
    a busy CI box.
    """
    phases = scheduler_phases(NODE_COUNT)
    total = sum(seconds for _, seconds in phases)

    assert total < 5.0, f"1000-node scheduler chain took {total:.3f}s: {_format(phases)}"


def _best_of(fn: Callable[[], object], runs: int = 3) -> float:
    """Seconds for the fastest of runs calls to fn.

    Noise on a busy machine only ever adds time, never subtracts it, so the fastest
    sample is the one closest to what the code actually costs -- a single timing measures
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
    node count -- 4+ hours at this size. Scoped to the chunks downstream of the new edge it is
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
    linear, 2.0 quadratic, 3.0 cubic. It is the number worth reading -- it says whether a
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
