"""Gizmos on the Offset Transform chunk.

Unlike rbd_solver, this one has all three of translate/rotate/scale, so it declares a single
whole-chunk manipulator rather than separate handles -- the same shape as transform.py.

The consequence worth testing is the one that is easy to state and easy to break: declaring
a transform handle is how a chunk claims its prim's placement. Dragging a prim this chunk
targets has to edit THESE params, not auto-create a Transform chunk stacked on top doing
the same job.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd

offset_mod = importlib.import_module("scripts.offset_transform")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


class _StubModule:
    """A registered `transform` module, so a wrongly-triggered auto-create is observable
    rather than silently doing nothing."""

    def __init__(self):
        self.loaded = True
        self.creates = 0

    def create(self):
        self.creates += 1
        transform_mod = importlib.import_module("scripts.transform")
        c = transform_mod.main()
        c._config = ChunkConfig(module="transform")
        c._module = transform_mod
        return [c]

    def load(self):
        return self.create()


@pytest.fixture
def offset(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/World/Cube")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)

    module = _StubModule()
    monkeypatch.setitem(registry.registered_modules, "transform", module)
    monkeypatch.setattr(chunk_usd, "_params_tab_create", False, raising=False)
    monkeypatch.setattr(chunk_usd, "_pin_new_chunks", None, raising=False)
    monkeypatch.setattr(chunk_usd, "_create_node_pos", None, raising=False)

    node = offset_mod.main()
    node._config = ChunkConfig(module="offset_transform")
    node._module = offset_mod
    sender.add_chunk(node)
    node.get_param("prim_path").value = "/World/Cube"
    return stage, node, module


def _cube(stage):
    return stage.GetPrimAtPath("/World/Cube")


def test_it_declares_one_whole_chunk_manipulator(offset) -> None:
    _stage, node, _module = offset
    gizmos = chunk_gizmo.get_gizmos(node)

    assert [g.type for g in gizmos] == ["transform"]


def test_the_three_groups_are_absorbed_not_left_beside_it(offset) -> None:
    """Three separate handles plus a combined one over the same nine params would fight for
    the same drag, and a host has no way to know they are the same thing."""
    _stage, node, _module = offset

    assert {g.name for g in chunk_gizmo.get_gizmos(node)} == {"transform"}


def test_the_handle_drives_all_nine_params(offset) -> None:
    _stage, node, _module = offset

    assert len(chunk_gizmo.get_gizmos(node)[0].driver) == chunk_gizmo.core.GIZMO_TRANSFORM_ARITY


def test_it_resolves_from_the_prim_it_targets(offset) -> None:
    stage, _node, _module = offset

    types = {d.type for d in chunk_gizmo.resolve_gizmos(_cube(stage))}

    assert types == {"transform"}


def test_dragging_writes_the_offset_params(offset) -> None:
    stage, node, _module = offset
    desc = chunk_gizmo.transform_desc(_cube(stage))

    chunk_gizmo.apply_bindings(
        desc.chunk_prim, desc.name, Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(1.0, 2.0, 3.0))
    )

    assert [node.get_param(n).value for n in ("tx", "ty", "tz")] == pytest.approx([1.0, 2.0, 3.0])


def test_it_blocks_the_auto_created_transform_chunk(offset) -> None:
    """The point of declaring the handle. Without it, dragging this prim would stack a
    Transform chunk on top of an Offset Transform already driving the same placement."""
    stage, _node, module = offset

    desc = chunk_gizmo.ensure_transform_chunk(_cube(stage))

    assert module.creates == 0, "auto-create fired despite a chunk already offering a handle"
    assert desc is not None
