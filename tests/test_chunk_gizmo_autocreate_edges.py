"""Two questions about auto-create: multi-selection, and deleting the chunk it made.

Written to find out what actually happens, not to assert a decision already taken.
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
        self.source_hash = None
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
def scene(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        for name in ("A", "B", "C"):
            UsdGeom.Cube.Define(stage, f"/World/{name}")

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
    return stage, registry, module


def _prim(stage, name):
    return stage.GetPrimAtPath(f"/World/{name}")


# ---------------------------------------------------------------------------
# multi-selection
# ---------------------------------------------------------------------------

def test_each_selected_prim_gets_its_own_chunk(scene) -> None:
    """The host commits per prim, so N unowned prims in one drag make N chunks.

    That is noisy but correct, and the alternative is worse. transform.py authors ONE
    xformOp:transform on every path it targets, so a single chunk driving three prims would
    snap all three to the same place -- it has no per-prim delta to preserve their relative
    positions with. One chunk each keeps them where they are.
    """
    stage, _registry, module = scene

    descs = [chunk_gizmo.ensure_transform_chunk(_prim(stage, n)) for n in ("A", "B", "C")]

    assert module.creates == 3
    assert len({d.chunk_prim for d in descs}) == 3, "chunks must be distinct, not shared"


def test_each_chunk_targets_only_its_own_prim(scene) -> None:
    stage, _registry, _module = scene

    for name in ("A", "B", "C"):
        desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, name))
        created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
        assert created.get_param("primpath_location").value == f"/World/{name}"


def test_multi_selection_keeps_prims_where_they_were(scene) -> None:
    """The reason one-chunk-per-prim matters: they start in different places."""
    stage, _registry, _module = scene
    for name, x in (("A", 1.0), ("B", 5.0), ("C", 9.0)):
        with Usd.EditContext(stage, stage.GetRootLayer().subLayerPaths and
                             Sdf.Layer.Find(stage.GetRootLayer().subLayerPaths[0])):
            UsdGeom.Xformable(_prim(stage, name)).AddTransformOp().Set(
                Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(x, 0, 0)))

    for name in ("A", "B", "C"):
        desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, name))
        chunk_gizmo.chunk_at_prim_path(desc.chunk_prim).recook()

    for name, x in (("A", 1.0), ("B", 5.0), ("C", 9.0)):
        local = UsdGeom.Xformable(_prim(stage, name)).GetLocalTransformation(Usd.TimeCode.Default())
        local = local[0] if isinstance(local, tuple) else local
        assert Gf.IsClose(local.ExtractTranslation(), Gf.Vec3d(x, 0, 0), 1e-6), name


# ---------------------------------------------------------------------------
# deleting the chunk that was created
# ---------------------------------------------------------------------------

def test_deleting_the_chunk_releases_the_prim(scene) -> None:
    stage, registry, module = scene
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)

    created.request_delete()

    assert chunk_gizmo.placement_gizmos(_prim(stage, "A")) == [], (
        "the prim still reports as owned by a chunk that no longer exists"
    )


def test_the_prim_goes_back_where_it_was(scene) -> None:
    """Deleting the chunk takes its layer with it, so the override it authored goes too."""
    stage, _registry, _module = scene
    desc = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))
    created = chunk_gizmo.chunk_at_prim_path(desc.chunk_prim)
    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(7.0, 0, 0))
    )
    created.recook()

    created.request_delete()

    local = UsdGeom.Xformable(_prim(stage, "A")).GetLocalTransformation(Usd.TimeCode.Default())
    local = local[0] if isinstance(local, tuple) else local
    assert Gf.IsClose(local.ExtractTranslation(), Gf.Vec3d(0, 0, 0), 1e-6)


def test_dragging_again_after_a_delete_makes_a_fresh_chunk(scene) -> None:
    stage, _registry, module = scene
    first = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))
    chunk_gizmo.chunk_at_prim_path(first.chunk_prim).request_delete()

    second = chunk_gizmo.ensure_transform_chunk(_prim(stage, "A"))

    assert module.creates == 2
    assert second is not None
    assert chunk_gizmo.chunk_at_prim_path(second.chunk_prim) is not None
