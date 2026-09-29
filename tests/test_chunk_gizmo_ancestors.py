"""A chunk driving a mesh does not thereby drive the mesh's parents.

Reported live: with the apple scene sublayered, dragging
`/food_apple_01_4k/food_apple_01/apple_50k_uv_001` created a Transform chunk for the mesh.
Then selecting the parent `/food_apple_01_4k` and dragging did NOT create a second one --
it reused the mesh's chunk, so the drag went somewhere unrelated.

The asymmetry is the giveaway, and was reported as one: doing it the other way round works.
Authoring an override on a prim implicitly creates empty `over` specs for every ancestor,
because the descendant needs a path to hang off, and those specs are in each ancestor's
prim stack. A descendant is not scaffolding for an ancestor, so only one direction breaks.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage

transform_mod = importlib.import_module("chunks.transforms.transform")

MESH = "/apple/grp/mesh"
PARENT = "/apple"


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _StubModule:
    def __init__(self):
        self.loaded = True
        self.creates = 0
        # Registered under "transform", so this is where the chunks it creates find their
        # hooks (ChunkBase.get_chunk_module): the real transform module's.
        self.module = transform_mod

    def create(self):
        self.creates += 1
        c = transform_mod.main()
        c._config = ChunkConfig(module="transform")
        return [c]

    load = create


@pytest.fixture
def hierarchy(monkeypatch):
    """Nested like the reported asset: parent Xform, an intermediate group, then the mesh."""
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xform.Define(stage, PARENT)
        UsdGeom.Xform.Define(stage, "/apple/grp")
        UsdGeom.Mesh.Define(stage, MESH)

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)

    module = _StubModule()
    monkeypatch.setitem(registry.registered_modules, "transform", module)
    monkeypatch.setattr(chunk_stage, "_params_tab_create", False, raising=False)
    monkeypatch.setattr(chunk_stage, "_pin_new_chunks", None, raising=False)
    monkeypatch.setattr(chunk_stage, "_create_node_pos", None, raising=False)
    return stage, module


def _drag(stage, path, translation=(0.0, 3.0, 0.0)):
    prim = stage.GetPrimAtPath(path)
    desc = chunk_gizmo.ensure_transform_chunk(prim)
    assert desc is not None
    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*translation))
    )
    chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).execute()
    return desc


def test_dragging_the_mesh_then_the_parent_makes_two_chunks(hierarchy) -> None:
    """The reported order. The second drag used to find the mesh's chunk and reuse it."""
    stage, module = hierarchy

    mesh_desc = _drag(stage, MESH)
    parent_desc = _drag(stage, PARENT)

    assert module.creates == 2
    assert parent_desc.chunk_prim != mesh_desc.chunk_prim


def test_dragging_the_parent_then_the_mesh_makes_two_chunks(hierarchy) -> None:
    """The order that always worked, kept so the fix cannot regress it."""
    stage, module = hierarchy

    parent_desc = _drag(stage, PARENT)
    mesh_desc = _drag(stage, MESH)

    assert module.creates == 2
    assert parent_desc.chunk_prim != mesh_desc.chunk_prim


def test_each_chunk_targets_its_own_prim(hierarchy) -> None:
    stage, _module = hierarchy
    _drag(stage, MESH)
    _drag(stage, PARENT)

    for path in (MESH, PARENT):
        handles = chunk_gizmo.placement_gizmos(stage.GetPrimAtPath(path))
        assert handles, path
        assert all([str(a) for a in h.anchor] == [path] for h in handles), (
            f"{path} got a handle anchored somewhere else"
        )


def test_the_parent_is_not_owned_by_the_meshs_chunk(hierarchy) -> None:
    """The check auto-create makes. A parent that reads as owned never gets its own chunk."""
    stage, _module = hierarchy
    _drag(stage, MESH)

    assert chunk_gizmo.placement_gizmos(stage.GetPrimAtPath(PARENT)) == []
    assert chunk_gizmo.chunks_for_prim(stage.GetPrimAtPath(PARENT)) == []


def test_the_intermediate_group_is_not_owned_either(hierarchy) -> None:
    stage, _module = hierarchy
    _drag(stage, MESH)

    assert chunk_gizmo.chunks_for_prim(stage.GetPrimAtPath("/apple/grp")) == []


def test_the_mesh_is_still_owned_by_its_own_chunk(hierarchy) -> None:
    """The scaffolding rule must not throw away the real claim along with the empty ones."""
    stage, _module = hierarchy
    _drag(stage, MESH)

    owners = chunk_gizmo.chunks_for_prim(stage.GetPrimAtPath(MESH))

    assert [c.name for c in owners] == ["Transform"]


def test_dragging_the_parent_moves_the_parent(hierarchy) -> None:
    """End to end: the second drag has to land on the prim that was dragged."""
    stage, _module = hierarchy
    _drag(stage, MESH, (0.0, 3.0, 0.0))
    _drag(stage, PARENT, (7.0, 0.0, 0.0))

    def _local(path):
        matrix = UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation(
            Usd.TimeCode.Default()
        )
        matrix = matrix[0] if isinstance(matrix, tuple) else matrix
        return matrix.ExtractTranslation()

    assert Gf.IsClose(_local(PARENT), Gf.Vec3d(7.0, 0.0, 0.0), 1e-6)
    assert Gf.IsClose(_local(MESH), Gf.Vec3d(0.0, 3.0, 0.0), 1e-6)
