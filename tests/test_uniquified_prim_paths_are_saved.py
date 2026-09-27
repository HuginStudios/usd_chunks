"""Two of the same chunk keep two prims after a reload.

A chunk whose default prim path is already taken renames itself when it cooks: the second
Camera gets /cameras/camera2. That new path is the only thing distinguishing it from the
first one -- and it was not saved, so reopening a scene with two cameras came back with both
chunks pointing at /cameras/camera1 and one camera prim on the stage where there had been two.

Two halves to it, and both had to go:

* The unique path was computed into the param's *default* at construction, from whatever the
  stage happened to hold at that moment. Defaults are deliberately not written into a saved
  scene -- running main() again reproduces them -- but this one is not reproducible: on reload
  no camera prim exists yet, so main() hands both chunks /cameras/camera1.

* Nothing wrote the resolved path back as a value. The write-back that exists compares the
  authored path against a path execute() had already uniquified, so it could never differ.
"""

from __future__ import annotations

import pathlib

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as chunk_usd_initialize, save_scene


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage: Usd.Stage) -> SceneSender:
    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Staged()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_initialize(stage, scene)
    return scene


def _add(scene: SceneSender, module: str):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module)
    chunk_obj = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(chunk_obj)
    return chunk_obj


def _paths(chunks) -> list[str]:
    return sorted(c.get_param("primpath_location").value for c in chunks)


#: module, the path the first one takes, the path the second has to uniquify to.
CASES = [
    ("camera", "/cameras/camera1", "/cameras/camera2"),
    ("light", "/lights/light1", "/lights/light2"),
    ("render_pass", "/Render/Passes/beauty", "/Render/Passes/beauty1"),
    ("render_product", "/Render/Products/beauty", "/Render/Products/beauty1"),
    ("render_var", "/Render/Vars/beauty", "/Render/Vars/beauty1"),
    ("render_settings", "/Render/settings", "/Render/settings1"),
]


@pytest.mark.parametrize("module, first, second", CASES, ids=[c[0] for c in CASES])
def test_two_of_the_same_chunk_keep_two_prims_across_a_save(module, first, second, tmp_path):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    scene = _scene_for(stage)

    _add(scene, module)
    _add(scene, module)
    live = [c for c in scene.get_chunks() if (c.get_config() and c.get_config().module) == module]
    assert _paths(live) == [first, second], "the second one has to rename itself to begin with"

    out = pathlib.Path(tmp_path) / "scene.usda"
    save_scene(scene, out)

    chunk_registry.ChunkLayerManager().reset()
    reopened = Usd.Stage.Open(str(out))
    reopened_scene = _scene_for(reopened)
    loaded = [
        c for c in reopened_scene.get_chunks()
        if (c.get_config() and c.get_config().module) == module
    ]

    assert len(loaded) == 2
    assert _paths(loaded) == [first, second]
    assert reopened.GetPrimAtPath(first).IsDefined()
    assert reopened.GetPrimAtPath(second).IsDefined(), (
        "both chunks authored over the same prim"
    )


@pytest.mark.parametrize("module, first, _second", CASES, ids=[c[0] for c in CASES])
def test_the_default_path_does_not_depend_on_what_the_stage_holds(module, first, _second):
    """The half that made the saved value necessary in the first place.

    A default computed from live stage state is not a default -- it is a value wearing one,
    and the save path is entitled to leave it out on the grounds that running main() again
    reproduces it. Running main() again does not reproduce this one.
    """
    mod = chunk_registry.ChunkRegistry().get_chunk_module(module)

    empty = Usd.Stage.CreateInMemory()
    _scene_for(empty)
    on_empty = (mod.create() if mod.loaded else mod.load())[0]

    crowded = Usd.Stage.CreateInMemory()
    crowded.DefinePrim(first, "Xform")
    chunk_registry.ChunkLayerManager().reset()
    _scene_for(crowded)
    on_crowded = (mod.create() if mod.loaded else mod.load())[0]

    assert on_empty.get_param("primpath_location").default_value == first
    assert on_crowded.get_param("primpath_location").default_value == first
