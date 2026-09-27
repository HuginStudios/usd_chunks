"""Gizmos on the RBD solver, and the resolve gap it exposed.

Two offset handles -- Position Offset and Rotation Offset -- because those place the body
before the sim runs, which is precisely the thing you want to drag in the viewport rather
than type into a collapsed section.

Getting them to appear needed a change in chunk_gizmo. resolve_gizmos found chunks via the
layer stack, i.e. chunks that had already authored something on the prim. rbd_solver is
button-driven and authors nothing until you press Run, so its offset handles -- the ones
you want BEFORE a simulation -- would not have shown up until after the first one.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks import chunk_stage

rbd_mod = importlib.import_module("chunks.dynamics.rbd_solver")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def solver():
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Cube.Define(stage, "/World/Cube")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_stage.ChunkStageSync().initialize(stage, sender)

    node = rbd_mod.main()
    node._config = ChunkConfig(module="rbd_solver")
    node._module = rbd_mod
    sender.add_chunk(node)
    node.get_param("prim_path").value = "/World/Cube"
    return stage, node


def _by_name(node):
    return {g.name: g for g in chunk_gizmo.get_gizmos(node)}


def test_the_solver_declares_no_placement_handle(solver) -> None:
    """The solver's own position/rotation offsets are gone, and with them its handles.

    Placing a body before a sim is a Transform's job now that Transform chunks compose, and
    doing it in two places was actively harmful: with a chair wired Transform -> RBD Solver,
    going back to adjust the Transform while the solver held its own offset put the chair
    back at the origin.
    """
    _stage, node = solver

    assert chunk_gizmo.get_gizmos(node) == []


def test_an_upstream_transform_still_owns_the_prim(solver) -> None:
    """So dragging the body edits the Transform, and the solver never competes for it."""
    stage, node = solver

    assert chunk_gizmo.placement_gizmos(_cube_of(stage)) == []


def _cube_of(stage):
    return stage.GetPrimAtPath("/World/Cube")
