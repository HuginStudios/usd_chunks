"""The Qt graph gives a new graph its Output, the way it used to give it a view.

Dropping the first node wires an Output to it and cooks. After that the Output stays
where it was wired: a second node dropped beside the first is not moved in front of it,
and starts blocked until someone wires it in.
"""

from __future__ import annotations

import pytest
from pxr import Usd

from qt_wait import pump_until
from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as _chunk_usd_init
from usd_chunks.frontends.qt.factory import ChunkWidgetDocker


@pytest.fixture
def graph(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _Scene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _Scene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    docker = ChunkWidgetDocker()
    docker.resize(500, 600)
    docker.show()
    qapp.processEvents()
    yield qapp, reg, scene, stage
    docker.close()
    docker.deleteLater()


def _drop(reg, scene, module="transform"):
    mod = reg.get_chunk_module(module)
    chunks = mod.create() if mod.loaded else mod.load()
    for c in chunks:
        scene.add_chunk(c)
    return chunks[0]


def _outputs(scene):
    return [c for c in scene.get_chunks() if chunk_stage.is_output_chunk(c)]


def _fed_by(stage, output):
    prim = stage.GetPrimAtPath(get_chunk_prim_path(output))
    return [str(p.GetPrimPath()) for p in prim.GetAttribute("inputs:chunk").GetConnections()]


def test_the_first_node_gets_an_output_wired_to_it(graph):
    qapp, reg, scene, stage = graph
    first = _drop(reg, scene)
    assert pump_until(qapp, lambda: _outputs(scene)), "no Output was made for the first node"
    (output,) = _outputs(scene)
    assert _fed_by(stage, output) == [get_chunk_prim_path(first)]
    assert not any(chunk_stage.is_view_chunk(c) for c in scene.get_chunks()), "no default view any more"
    assert pump_until(qapp, lambda: not first.is_blocked()), "the first node feeds the Output, so it cooks"


def test_the_output_stays_where_it_was_wired(graph):
    qapp, reg, scene, stage = graph
    first = _drop(reg, scene)
    assert pump_until(qapp, lambda: _outputs(scene))
    second = _drop(reg, scene)
    pump_until(qapp, lambda: False, seconds=0.5)

    (output,) = _outputs(scene)
    assert _fed_by(stage, output) == [get_chunk_prim_path(first)]
    assert second.is_blocked(), "not wired to the Output, so it doesn't run"
