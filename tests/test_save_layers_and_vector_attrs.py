"""Two host-facing paths no test reached.

- ChunkLayerManager.save_layers / save_layer: usdview's "Save Layers" menu, one file per
  chunk layer into a chosen folder.
- A Vector built by show_attribute from a real prim's float3 / color3f attribute (the
  Property Editor's translate, displayColor, ...): reads it, and writes it back.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd

from usd_chunks import chunk, chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender

from chunk_stubs import attach_stub_module


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture()
def scene():
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    return scene


def _maker(scene, name="maker"):
    c = chunk.Chunk(name)
    attach_stub_module(c)
    c.add_param(chunk.Slider("p"))
    scene.add_chunk(c)
    return c


# -- save layers -------------------------------------------------------------------------------

def test_save_layers_writes_a_file_per_chunk_layer(scene, tmp_path):
    c = _maker(scene)
    with Usd.EditContext(scene.get_stage(), c.get_edit_context()):
        scene.get_stage().DefinePrim("/Made", "Xform")

    saved = chunk_registry.ChunkLayerManager().save_layers(tmp_path)

    files = {Path(f).name for f in saved}
    assert "maker.usda" in files
    written = Sdf.Layer.FindOrOpen(str(tmp_path / "maker.usda"))
    assert written.GetPrimAtPath("/Made") is not None, "the chunk's own edits were not saved"


def test_save_layer_writes_one_chunk(scene, tmp_path):
    c = _maker(scene, "single")

    out = chunk_registry.ChunkLayerManager().save_layer(c, tmp_path)

    assert out == tmp_path / "single.usda" and out.exists()


def test_save_layers_into_a_missing_folder_writes_nothing(scene, tmp_path):
    _maker(scene)

    assert chunk_registry.ChunkLayerManager().save_layers(tmp_path / "nope") == {}
    assert not (tmp_path / "nope").exists()


# -- a Vector over a real attribute ------------------------------------------------------------

@pytest.fixture()
def shown(scene):
    """show_attribute of a prim with a float3 and a color3f, inside a chunk's group."""
    stage = scene.get_stage()
    prim = stage.DefinePrim("/Thing", "Xform")
    prim.CreateAttribute("offset", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(1, 2, 3))
    prim.CreateAttribute("tint", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.5, 0.25, 0.0))
    c = chunk.Chunk("editor")
    attach_stub_module(c)
    group = c.add_param(chunk.Group("props"))
    scene.add_chunk(c)
    chunk.show_attribute(prim, group, collapse_non_inputs=False)
    return prim, group


def _vector(group, name):
    return next(p for p in group.get_params() if p.name == name and isinstance(p, chunk.Vector))


def test_a_float3_shows_as_a_vector_holding_its_value(shown):
    _, group = shown

    vec = _vector(group, "offset")

    assert vec.length == 3
    assert list(vec.value) == pytest.approx([1, 2, 3])


def test_editing_the_vector_writes_the_attribute(shown):
    prim, group = shown
    vec = _vector(group, "offset")

    vec.value = (4.0, 5.0, 6.0)

    assert list(prim.GetAttribute("offset").Get()) == pytest.approx([4, 5, 6])


def test_editing_one_component_writes_just_that_component(shown):
    prim, group = shown
    vec = _vector(group, "offset")

    vec.get_my_params()[1].value = 9.0

    assert list(prim.GetAttribute("offset").Get()) == pytest.approx([1, 9, 3])


def test_a_color3f_shows_as_a_zero_to_one_vector(shown):
    _, group = shown

    vec = _vector(group, "tint")

    assert list(vec.value) == pytest.approx([0.5, 0.25, 0.0])
    assert all(c.max_value == 1.0 and c.min_value == 0.0 for c in vec.get_my_params())
