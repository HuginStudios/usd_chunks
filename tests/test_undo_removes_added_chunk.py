"""Undoing a chunk's creation removes the chunk, not just its prim.

Reported: drag a prim with the gizmo, a Transform chunk is made; Ctrl+Z took the node off
the graph, but the chunk stayed in scene.get_chunks() with its layer still in the stack,
so the prim stayed moved. Undo puts the chunk prim's specs back and leaves the rest to the
chunk system; the chunk system only dropped a chunk whose prim left the stage when the
chunk had come in through a reference.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_stage, chunk_undo
from usd_chunks.gizmo import runtime as chunk_gizmo

from graph_fixtures import _Scene


@pytest.fixture
def env():
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/World/Cube")
    scene = _Scene(stage)
    registry = chunk_registry.ChunkRegistry()
    registry.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.initialize(stage, scene)
    # Another chunk, so the graph is not emptied by the undo.
    entry = registry.registered_modules["sublayer"]
    scene.add_chunk((entry.create() if entry.loaded else entry.load())[0])
    stack = chunk_undo.UndoStack(stage)
    yield stage, scene, registry, stack
    stack.close()


def _names(scene):
    return sorted(c.name for c in scene.get_chunks())


def _ops(stage):
    return UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cube")).GetOrderedXformOps()


def _drag(stage, scene):
    """What a gizmo drag on an unowned prim does: make a Transform chunk, then move it."""
    desc = chunk_gizmo.ensure_transform_chunk(stage.GetPrimAtPath("/World/Cube"))
    assert desc is not None
    transform = next(c for c in scene.get_chunks() if c.name.startswith("Transform"))
    transform.get_param("tx").value = 5.0
    return transform


def test_undoing_a_gizmo_drag_removes_its_chunk_and_the_move(env):
    stage, scene, _registry, stack = env
    layers_before = len(stage.GetLayerStack())
    stack.note_input()
    _drag(stage, scene)
    assert _ops(stage), "the drag did not move the cube"

    assert stack.undo()

    assert _names(scene) == ["Sublayer"]
    assert len(stage.GetLayerStack()) == layers_before, "the chunk's layer is still in the stack"
    assert not _ops(stage), "the cube is still moved"


def test_redo_brings_the_chunk_and_the_move_back(env):
    stage, scene, _registry, stack = env
    stack.note_input()
    _drag(stage, scene)
    stack.undo()

    assert stack.redo()

    transform = next(c for c in scene.get_chunks() if c.name.startswith("Transform"))
    assert transform.get_param("tx").value == 5.0
    assert _ops(stage), "redo did not move the cube again"


def test_redoing_a_delete_removes_the_chunk_again(env):
    stage, scene, registry, stack = env
    entry = registry.registered_modules["transform"]
    transform = (entry.create() if entry.loaded else entry.load())[0]
    scene.add_chunk(transform)
    stack.note_input()
    transform.request_delete()
    stack.undo()
    assert _names(scene) == ["Sublayer", "Transform"]

    assert stack.redo()

    assert _names(scene) == ["Sublayer"]


def test_a_chunk_in_a_disabled_group_is_kept(env):
    """A prim hidden by a deactivated parent is still there: its chunk stays."""
    stage, scene, registry, _stack = env
    entry = registry.registered_modules["transform"]
    transform = (entry.create() if entry.loaded else entry.load())[0]
    scene.add_chunk(transform)
    group = stage.GetPrimAtPath(chunk_stage.get_chunk_prim_path(transform)).GetParent()

    group.SetActive(False)

    assert "Transform" in _names(scene)
