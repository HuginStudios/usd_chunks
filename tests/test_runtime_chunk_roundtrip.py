"""A chunk built in the interpreter survives a save.

    from usd_chunks import chunk
    c = chunk.Chunk("hello")
    chunk.get_scene().add_chunk(c)
    c.add_param(chunk.Slider("slider"))

Everything about that lands on the stage already -- the prim, the param as inputs:slider,
its value, the node position. The one thing missing was info:chunkType, which is how a load
finds the module to rebuild a chunk from, and it was only ever written for a chunk that HAD
a module. So the prim was saved complete and skipped on the way back in.

A module-less chunk is now saved as the generic `runtime` type, whose module rebuilds the
params from those same attributes. Behaviour does not come back -- callbacks live in the
session that made them, and nothing here saves code.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk, chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.plugins import chunk_usd
from usd_chunks.plugins.chunk_usd import get_chunk_prim_path, initialize as _chunk_usd_init


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


@pytest.fixture
def scene_with_runtime_chunk(qapp):
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    c = chunk.Chunk("hello")
    scene.add_chunk(c)
    c.add_param(chunk.Slider("slider"))
    qapp.processEvents()
    c.get_param("slider").value = 0.75
    qapp.processEvents()
    return stage, scene, c


def test_it_is_saved_with_a_type(scene_with_runtime_chunk):
    """Without a type there is nothing for a load to rebuild from."""
    stage, _scene, c = scene_with_runtime_chunk

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
    assert prim.GetAttribute("info:chunkType").Get() == "runtime"


def test_the_params_and_values_are_on_the_prim(scene_with_runtime_chunk):
    """Pinned: this always worked, and is what makes rebuilding possible at all."""
    stage, _scene, c = scene_with_runtime_chunk

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(c)))
    assert prim.GetAttribute("inputs:slider").Get() == pytest.approx(0.75)


def test_a_real_module_keeps_its_own_type(scene_with_runtime_chunk):
    """The fallback must never stand in for a module that exists."""
    stage, scene, _c = scene_with_runtime_chunk
    reg = chunk_registry.ChunkRegistry()
    mod = reg.get_chunk_module("prim")
    real = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(real)

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(real)))
    assert prim.GetAttribute("info:chunkType").Get() == "prim"


def test_the_runtime_type_is_not_offered_in_the_menu():
    """It exists to be loaded, not to be picked."""
    reg = chunk_registry.ChunkRegistry()

    entries = reg.get_menu_entries()

    assert not [e for e in entries if e.module.module_name == "runtime"], (
        "the runtime type must stay out of the chunk menu"
    )


def test_it_loads_back_with_its_params(qapp):
    """The round trip: read the saved prims into a fresh scene."""
    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    made = chunk.Chunk("hello")
    scene.add_chunk(made)
    made.add_param(chunk.Slider("slider"))
    qapp.processEvents()
    made.get_param("slider").value = 0.75
    qapp.processEvents()

    # A fresh scene reading the same stage, as reopening the file does.
    reloaded_scene = _StageScene()
    reg.initialize(reloaded_scene, "pytest")
    chunk_usd.load_chunks(stage, reloaded_scene, set())
    qapp.processEvents()

    # By name, not by prim path: a second scene in the same process builds its own default
    # chunkgroup, so the restored chunk hangs under that one. Which container it lands in is
    # this test harness, not the feature.
    restored = next((c for c in reloaded_scene.get_chunks() if c.name == "hello"), None)
    assert restored is not None, "the chunk must come back at all"
    assert restored is not made, "and be rebuilt from the file, not the live object"
    param = restored.get_param("slider")
    assert param is not None, "and bring its param with it"
    assert param.value == pytest.approx(0.75), "and its value"


def test_a_module_less_context_is_not_given_the_runtime_type(qapp):
    """load_chunks recognises a plain tab context by the ABSENCE of a type.

    Stamping the fallback onto one would have it rebuilt as a chunk inside itself, so the
    fallback deliberately stops at contexts.
    """
    from usd_chunks.chunk_flags import ChunkFlags

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)

    context = chunk.Chunk("a_tab", flags=ChunkFlags.GroupFlags.CONTEXT)
    scene.add_chunk(context)
    qapp.processEvents()

    prim = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(context)))
    if prim and prim.IsValid():
        assert prim.GetAttribute("info:chunkType").Get() != "runtime"
