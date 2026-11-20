"""Reload Chunks reads edited chunk modules again and keeps the network as it was.

It used to clear the scene's chunk list and build fresh chunks from each module: every
param went back to its default, wires were lost, the new chunks went into a second root
group the graph wasn't showing (so the network looked empty), and the edited code never
ran because the old module stayed in sys.modules.
"""
from __future__ import annotations

import sys
import uuid

import pytest
from pxr import Usd

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_script import Scene

_SOURCE = '''
from __future__ import annotations

from pxr import Sdf

from usd_chunks import chunk


def main():
    c = chunk.Chunk("Reload Probe")
    c.add_param(chunk.Slider("size", default_value=1.0))
    return c


def execute(node):
    prim = node.get_stage().DefinePrim("/probe_" + node.name)
    prim.CreateAttribute("tag", Sdf.ValueTypeNames.String).Set("TAG")
    prim.CreateAttribute("size", Sdf.ValueTypeNames.Float).Set(node.get_param("size").value)


def config():
    return chunk.DefaultConfig(category="Test", description="Writes a tagged prim, for the reload tests.")
'''


@pytest.fixture
def probe(tmp_path, scene):
    """A chunk module on disk, and a function that rewrites its tag."""
    name = f"reload_probe_{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{name}.py"

    def write(tag):
        path.write_text(_SOURCE.replace("TAG", tag), encoding="utf-8")

    write("before")
    chunk_registry.ChunkRegistry().import_chunk(path)
    yield name, write
    registry = chunk_registry.ChunkRegistry()
    registry.registered_modules.pop(name, None)
    sys.modules.pop(name, None)


@pytest.fixture
def scene():
    s = Scene(Usd.Stage.CreateInMemory(), "pytest")
    yield s
    s.close()


def _probe_prims(stage):
    return {str(p.GetPath()): (p.GetAttribute("tag").Get(), p.GetAttribute("size").Get())
            for p in stage.GetPseudoRoot().GetChildren() if p.GetName().startswith("probe_")}


def _chunk_names(scene):
    return sorted(c.name for c in scene.get_chunks())


def test_params_names_and_wires_survive_a_reload(probe, scene):
    name, _ = probe
    a = scene.add(name, size=5.0)
    b = scene.add(name, a, size=2.0)
    names = _chunk_names(scene)
    roots = scene.get_stage().GetPrimAtPath("/chunks").GetChildren()

    chunk_stage.ChunkStageSync().reload_chunks()

    assert _chunk_names(scene) == names
    assert scene.get_stage().GetPrimAtPath("/chunks").GetChildren() == roots, "a second root group"
    a2, b2 = scene.get_chunk(a.name), scene.get_chunk(b.name)
    assert a2.get_param("size").value == 5.0
    assert b2.get_param("size").value == 2.0
    assert scene.get_inputs(b2) == [a2]


def test_the_edited_module_is_what_cooks_after_a_reload(probe, scene):
    name, write = probe
    c = scene.add(name, size=5.0)

    write("after the edit")
    chunk_stage.ChunkStageSync().reload_chunks()

    assert set(_probe_prims(scene.get_stage()).values()) == {("after the edit", 5.0)}
    scene.get_chunk(c.name).get_param("size").value = 7.0
    assert set(_probe_prims(scene.get_stage()).values()) == {("after the edit", 7.0)}


def test_reloading_leaves_no_layers_behind(probe, scene):
    name, _ = probe
    a = scene.add(name)
    scene.add(name, a)
    stage = scene.get_stage()
    layers = len(stage.GetUsedLayers())

    for _ in range(3):
        chunk_stage.ChunkStageSync().reload_chunks()

    assert len(stage.GetUsedLayers()) == layers

