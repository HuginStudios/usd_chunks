"""A shader chunk authors only in its own layer unless it has a redirect to make.

wire_shader_input cleared the redirect for an unconnected shader input with
ClearConnections in the edit layer (the session layer, or the user's Edit Target). With no
spec there, that creates one: every cook left an empty `over` and a bare attribute in that
layer, for every input and every material path the chunk had ever used. Those kept a
renamed material's old shader prim alive, the param kept writing its value there, and both
ended up in exported files.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry, chunk_stage, chunk_utils
from usd_chunks.chunk_scene import SceneSender


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def surface():
    """A Preview Surface cooked at its default path, then moved to /World/materials/Green."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    module = registry.get_chunk_module("preview_surface")
    chunk = (module.create() if module.loaded else module.load())[0]
    scene.add_chunk(chunk)
    chunk.execute()
    chunk.get_param("mat_path").value = "/World/materials/Green"
    chunk.execute()
    return stage, chunk


def _attr_specs(layer):
    found = []
    layer.Traverse(Sdf.Path.absoluteRootPath,
                   lambda path: found.append(str(path)) if path.IsPropertyPath() else None)
    return found


def test_the_session_layer_gets_no_empty_shader_inputs(surface):
    stage, _chunk = surface
    assert _attr_specs(stage.GetSessionLayer()) == []


def test_a_moved_material_leaves_nothing_at_its_old_path(surface):
    stage, _chunk = surface
    assert not stage.GetPrimAtPath("/materials/surface/surface").IsValid()
    assert stage.GetPrimAtPath("/World/materials/Green/surface").IsValid()


def test_a_redirect_the_edit_layer_holds_is_still_cleared(surface):
    """The case the clear is for: a redirect left from when the input was wired."""
    stage, chunk = surface
    shader = stage.GetPrimAtPath("/World/materials/Green/surface")
    attr = shader.GetAttribute("inputs:diffuseColor")
    edit_layer = chunk_utils.get_edit_layer(stage)
    with Usd.EditContext(stage, edit_layer):
        attr.SetConnections([Sdf.Path("/World/materials/Green/tex.outputs:rgb")])
    chunk_prim = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(chunk))

    assert not chunk_utils.wire_shader_input(stage, chunk_prim, "diffuse_color", attr)

    spec = edit_layer.GetAttributeAtPath(attr.GetPath())
    assert spec is None or not spec.connectionPathList.GetAddedOrExplicitItems()


def test_uv_chunks_leave_nothing_outside_their_layer_either():
    """UV Texture and UV Transform wire their `st` / `in` inputs the same way."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, scene)
    for name in ("uv_texture", "uv_transform"):
        module = registry.get_chunk_module(name)
        chunk = (module.create() if module.loaded else module.load())[0]
        scene.add_chunk(chunk)
        chunk.execute()
    # The chunk graph keeps its own bookkeeping under /chunks in the session layer.
    assert [p for p in _attr_specs(stage.GetSessionLayer()) if not p.startswith("/chunks")] == []
