"""The exec-graph queries in chunk_stage, on a small real graph.

exec_downstream and exec_tails share one traversal of the stage. The downstream walk used
to re-traverse the stage per level and only ever skipped its start, so a cycle further
down kept it going forever.
"""
from __future__ import annotations

import pytest
from pxr import Usd

from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.chunk_core import SceneSender
from usd_chunks import chunk_stage
from usd_chunks.chunk_stage import connect_exec, get_chunk_prim_path


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def stack(stage, dst, *sources):
    """Several wires on dst's one exec input. connect_exec replaces, so author directly --
    on the attr's own layer, or a stronger opinion outvotes it."""
    attr = stage.GetPrimAtPath(get_chunk_prim_path(dst)).GetAttribute("inputs:chunk")
    with Usd.EditContext(stage, chunk_utils.get_attr_home_layer(attr)):
        attr.SetConnections([f"{get_chunk_prim_path(s)}.outputs:chunk" for s in sources])


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)
    mod = chunk_registry.ChunkRegistry().get_chunk_module("prim")

    def make(n):
        out = []
        for _ in range(n):
            c = (mod.create() if mod.loaded else mod.load())[0]
            scene.add_chunk(c)
            out.append(c)
        qapp.processEvents()
        return out

    def wire(a, b):
        connect_exec(stage, scene, get_chunk_prim_path(a), get_chunk_prim_path(b))

    return stage, scene, make, wire


def test_a_diamond(graph):
    """A feeds B and C, both feed D."""
    stage, scene, make, wire = graph
    a, b, c, d = make(4)
    wire(a, b)
    wire(a, c)
    wire(b, d)
    stack(stage, d, b, c)

    assert chunk_stage.exec_downstream(a, stage, scene) == {b, c, d}
    assert chunk_stage.exec_downstream(c, stage, scene) == {d}
    tails = chunk_stage.exec_tails(stage, scene, None)
    assert d in tails
    assert not {a, b, c} & set(tails)
    assert chunk_stage.exec_upstream(d) == {a, b, c, d}


def test_a_cycle_downstream_terminates(graph):
    """A -> B -> C -> B. The old walk only ever skipped the start, so B and C kept
    re-entering its frontier."""
    stage, scene, make, wire = graph
    a, b, c = make(3)
    wire(a, b)
    wire(b, c)
    stack(stage, b, a, c)

    assert chunk_stage.exec_downstream(a, stage, scene) == {b, c}
    assert chunk_stage.exec_upstream(c) == {a, b, c}


def test_a_cycle_through_the_start_excludes_it(graph):
    stage, scene, make, wire = graph
    a, b = make(2)
    wire(a, b)
    wire(b, a)

    assert chunk_stage.exec_downstream(a, stage, scene) == {b}


def test_exec_dependencies_ignore_param_wires_and_wired_dependencies_do_not(graph):
    """The narrow and the wide reading of "what feeds me" stay different."""
    stage, scene, make, wire = graph
    a, b = make(2)
    wire(a, b)
    assert chunk_stage.exec_dependencies(b) == [a]
    assert chunk_stage._wired_dependencies(b) == [a]
    assert chunk_stage.exec_dependencies(a) == []
