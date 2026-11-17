"""During one cook, the Edit Target walk above a chunk runs once, however often it is asked.

A cook runs on a cook stage (skip_cook), and the scene's memo of these answers is kept for
the scene's own stage, so a cook asked from scratch every time. A MaterialX Shader asks once
per input it authors: a Standard Surface walked the whole graph above it ~40 times per cook,
which was 40% of opening a 100-node shader network.
"""

from __future__ import annotations

from pxr import Usd

from usd_chunks import Scene, chunk, chunk_notice, chunk_stage, chunk_topology


def _chain(scene, n):
    last = scene.create.dot()
    for _ in range(n - 1):
        last = scene.create.dot(last)
    return last


def test_a_cook_walks_once_and_a_new_cook_walks_again(monkeypatch):
    with Scene() as scene:
        with chunk.change_block():
            last = _chain(scene, 12)
            scene.set_output(last)
        stage = scene.get_stage()
        prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(last))
        # The scene's own answer first, so the memo belongs to the scene's stage.
        chunk_topology.upstream_edit_target_layer(stage, prim)

        walks = []
        real = chunk_topology._iter_exec_upstream
        monkeypatch.setattr(chunk_topology, "_iter_exec_upstream",
                            lambda *a: walks.append(a) or real(*a))

        # A cook stage: another stage of the same layers.
        cook_stage = Usd.Stage.Open(stage.GetRootLayer(), stage.GetSessionLayer())
        cook_prim = cook_stage.GetPrimAtPath(prim.GetPath())
        monkeypatch.setattr(chunk_notice, "_executing_chunk", last)

        first = chunk_topology.upstream_edit_target_layer(cook_stage, cook_prim)
        one_ask = len(walks)  # the chunk's walk, and its group's
        assert one_ask
        answers = {chunk_topology.upstream_edit_target_layer(cook_stage, cook_prim) for _ in range(19)}
        assert len(walks) == one_ask, f"{len(walks)} walks for 20 asks in one cook, {one_ask} for one"
        assert answers == {first}

        monkeypatch.setattr(chunk_notice, "_cook_serial", chunk_notice._cook_serial + 2)
        chunk_topology.upstream_edit_target_layer(cook_stage, cook_prim)
        assert len(walks) == 2 * one_ask, "the next cook must not reuse the last one's answers"


def test_outside_a_cook_nothing_is_kept(monkeypatch):
    with Scene() as scene:
        with chunk.change_block():
            last = _chain(scene, 5)
            scene.set_output(last)
        stage = scene.get_stage()
        prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(last))
        chunk_topology.upstream_edit_target_layer(stage, prim)

        walks = []
        real = chunk_topology._iter_exec_upstream
        monkeypatch.setattr(chunk_topology, "_iter_exec_upstream",
                            lambda *a: walks.append(a) or real(*a))
        other = Usd.Stage.Open(stage.GetRootLayer(), stage.GetSessionLayer())
        monkeypatch.setattr(chunk_notice, "_executing_chunk", None)
        chunk_topology.upstream_edit_target_layer(other, other.GetPrimAtPath(prim.GetPath()))
        one_ask = len(walks)
        for _ in range(2):
            chunk_topology.upstream_edit_target_layer(other, other.GetPrimAtPath(prim.GetPath()))
        assert len(walks) == 3 * one_ask
