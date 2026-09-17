"""Saving a scene over the file it was opened from.

Sdf keeps one layer per path for the whole process, and export_chunks wrote the file
without touching that cached copy. The file on disk was right, but everything else in the
session still saw the scene as it was before the save: reopening it, dropping it back into
the graph (both go through FindOrOpen), or a later Save() of the stale layer over the new
file.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender
from usd_chunks.chunk_stage import (
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
    save_scene,
)


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage):
    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    s = _StageScene()
    chunk_registry.ChunkRegistry().initialize(s, "pytest")
    chunk_registry.ChunkLayerManager().reset()
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, s)
    return s


def _mk(scene, key):
    mod = chunk_registry.ChunkRegistry().get_chunk_module(key)
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _chunk_values(layer):
    """{chunk prim name: inputs:prim_type} for every prim chunk in layer."""
    found = {}

    def visit(path):
        spec = layer.GetPrimAtPath(path) if path.IsPrimPath() else None
        if spec and spec.typeName == "Chunk":
            attr = spec.attributes.get("inputs:prim_type")
            if attr is not None:
                found[path.name] = attr.default

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return found


def _on_disk(path):
    return _chunk_values(Sdf.Layer.OpenAsAnonymous(str(path)))


def _by_name(scene, name):
    return next(c for c in scene.get_chunks() if get_chunk_prim_path(c).endswith("/" + name))


@pytest.fixture(params=["usda", "usdc"])
def reopened(qapp, tmp_path, request):
    out = tmp_path / f"scene.{request.param}"
    stage = Usd.Stage.CreateInMemory()
    scene = _scene_for(stage)
    _mk(scene, "prim")
    _mk(scene, "prim")
    qapp.processEvents()
    save_scene(scene, out)
    assert set(_on_disk(out)) == {"Prim", "Prim1"}

    stage = Usd.Stage.Open(str(out))
    scene = _scene_for(stage)
    qapp.processEvents()
    return out, stage, scene


def test_the_cached_layer_matches_the_saved_file(qapp, reopened):
    out, _stage, scene = reopened
    _mk(scene, "prim")
    _by_name(scene, "Prim").get_param("prim_type").value = "Scope"
    qapp.processEvents()

    save_scene(scene, out)

    assert _on_disk(out) == {"Prim": "Scope", "Prim1": "Xform", "Prim2": "Xform"}
    assert _chunk_values(Sdf.Layer.FindOrOpen(str(out))) == _on_disk(out), (
        "anything reopening the path in this session must see what was just saved"
    )


def test_reopening_in_the_same_session_restores_the_save(qapp, reopened):
    out, _stage, scene = reopened
    _mk(scene, "prim")
    qapp.processEvents()
    save_scene(scene, out)

    stage = Usd.Stage.Open(str(out))
    scene = _scene_for(stage)
    qapp.processEvents()

    assert sorted(get_chunk_prim_path(c).rsplit("/", 1)[-1] for c in scene.get_chunks()
                  if "Prim" in get_chunk_prim_path(c)) == ["Prim", "Prim1", "Prim2"]


def test_editing_after_saving_over_still_lands_in_the_next_save(qapp, reopened):
    out, _stage, scene = reopened
    _mk(scene, "prim")
    qapp.processEvents()
    save_scene(scene, out)

    _by_name(scene, "Prim2").get_param("prim_type").value = "Scope"
    _by_name(scene, "Prim1").request_delete()
    qapp.processEvents()
    save_scene(scene, out)

    assert _on_disk(out) == {"Prim": "Xform", "Prim2": "Scope"}
