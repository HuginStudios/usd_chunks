"""A Transform deep in a chain of Transforms on one prim walks the chain once per cook.

xform_op_order measures how deep each Transform on the prim sits, to put the ops in chain
order. It walked the chain from scratch for every one of them, so one cook at the bottom of
a 100-Transform chain made ~5000 exec-input lookups and took 0.26 s, and editing the top of
the chain cost the chain's length cubed.
"""

from __future__ import annotations

from pxr import UsdGeom

from usd_chunks import Scene, chunk, chunk_stage, chunk_topology, chunk_utils
from usd_chunks.gizmo import runtime

DEPTH = 40


def test_one_cook_walks_the_chain_once(monkeypatch):
    with Scene() as scene:
        with chunk.change_block():
            last = scene.create.cube(prim_path="/World/box")
            for i in range(DEPTH):
                last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=0.01)
            scene.set_output(last)

        lookups = []
        real = chunk_stage.exec_input_connections
        monkeypatch.setattr(chunk_stage, "exec_input_connections",
                            lambda prim: lookups.append(prim) or real(prim))
        # From nothing kept: the depths are kept between cooks (test_a_second_cook_...).
        chunk_topology.invalidate_edit_layer_memo()
        scene.set(last, ty=0.5)

        # One walk up the chain, plus a little slack; the old walk-per-Transform made
        # DEPTH * DEPTH / 2 (800 here).
        assert 0 < len(lookups) <= 2 * (DEPTH + 1), f"{len(lookups)} exec-input lookups for one cook"

        # And the ops are still in chain order, each Transform's once.
        order = [str(op) for op in UsdGeom.Xformable(scene.get_stage().GetPrimAtPath("/World/box"))
                 .GetXformOpOrderAttr().Get()]
        expected = [chunk_utils.existing_xform_op_name(scene.get_chunk(f"T{i}")) for i in range(DEPTH)]
        assert [op for op in order if op in expected] == expected


def test_exec_depth_memo_matches_a_fresh_walk():
    with Scene() as scene:
        with chunk.change_block():
            chunks = [scene.create.cube(prim_path="/World/box")]
            for i in range(10):
                chunks.append(scene.create.transform(chunks[-1], primpath_location="/World/box", tx=0.1))
            scene.set_output(chunks[-1])
        stage = scene.get_stage()
        paths = [chunk_stage.get_chunk_prim_path(c) for c in chunks]
        fresh = [runtime._exec_depth(stage, p) for p in paths]
        memo = {}
        shared = [runtime._exec_depth(stage, p, memo) for p in reversed(paths)][::-1]
        assert fresh == shared == list(range(len(chunks)))


def _chain(scene, depth):
    last = scene.create.cube(prim_path="/World/box")
    chunks = [last]
    for i in range(depth):
        last = scene.create.transform(last, name=f"T{i}", primpath_location="/World/box", ty=0.01)
        chunks.append(last)
    scene.set_output(last)
    return chunks


def test_a_second_cook_reads_no_exec_inputs(monkeypatch):
    """The depths are kept until an exec wire changes, so the chain is not walked again."""
    with Scene() as scene:
        with chunk.change_block():
            last = _chain(scene, DEPTH)[-1]
        scene.set(last, ty=0.5)

        lookups = []
        real = chunk_stage.exec_input_connections
        monkeypatch.setattr(chunk_stage, "exec_input_connections",
                            lambda prim: lookups.append(prim) or real(prim))
        scene.set(last, ty=0.6)

        assert len(lookups) < DEPTH // 4, f"{len(lookups)} exec-input lookups for a second cook"


def test_kept_depths_follow_a_rewire(monkeypatch):
    with Scene() as scene:
        with chunk.change_block():
            chunks = _chain(scene, 4)
        t0, t2, t3 = chunks[1], chunks[-2], chunks[-1]
        scene.set(t3, ty=0.5)  # fills the kept depths
        stage = scene.get_stage()
        prim = stage.GetPrimAtPath("/World/box")
        op = chunk_utils.existing_xform_op_name(t3)

        # T3 now sits right below T0: two hops from the top, not four.
        scene.connect(t0, t3)
        t3.remove_dependency(t2)

        kept = runtime.xform_op_order(t3, prim, op)
        assert runtime._exec_depth(stage, chunk_stage.get_chunk_prim_path(t3),
                                   chunk_topology.exec_depth_memo(stage)) == 2
        monkeypatch.setattr(chunk_topology, "exec_depth_memo", lambda _stage: {})
        assert kept == runtime.xform_op_order(t3, prim, op)
