"""A view made for a chunk inside a group lands inside that group.

`create_view_for` added the view with `scene.add_chunk`, which parents to the scene's
current context. The Blender, Flet and ImGui graphs dive without setting it, so pressing V
inside a group made the view at the top level, wired across the group's edge, and the group
never got a view of its own.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import get_chunk_prim_path, initialize as init_chunk_usd
from usd_chunks.graph import engine as chunk_graph


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


def _make(registry, module_name, name):
    module = registry.get_chunk_module(module_name)
    chunk = (module.create() if module.loaded else module.load())[0]
    chunk.name = name
    return chunk


@pytest.fixture
def grouped():
    """A chunkgroup holding one Transform, with the scene's current context at the top."""
    stage = Usd.Stage.CreateInMemory()
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    init_chunk_usd(stage, scene)
    group = _make(registry, "chunkgroup", "Group")
    scene.add_chunk(group)
    with scene.creating_in(group):
        inner = _make(registry, "transform", "Inner")
        scene.add_chunk(inner)
    return stage, scene, group, inner


def test_a_view_of_a_chunk_in_a_group_is_made_in_that_group(grouped):
    stage, scene, group, inner = grouped
    inner_path = get_chunk_prim_path(inner)
    group_path = get_chunk_prim_path(group)
    assert str(Sdf.Path(inner_path).GetParentPath()) == group_path

    slot = chunk_graph.create_view_for(stage, scene, inner_path)

    assert slot is not None
    views = [p for p in stage.Traverse()
             if p.GetAttribute(chunk_graph.VIEW_SLOT_ATTR).IsValid()]
    assert [str(p.GetPath().GetParentPath()) for p in views] == [group_path]
    wired = views[0].GetAttribute("inputs:chunk").GetConnections()
    assert [str(c.GetPrimPath()) for c in wired] == [inner_path]


def test_the_scenes_own_context_is_left_as_it_was(grouped):
    """Pushed and popped: the next chunk created at the top still lands at the top."""
    stage, scene, group, inner = grouped
    before = scene.current_context
    chunk_graph.create_view_for(stage, scene, get_chunk_prim_path(inner))
    assert scene.current_context is before
