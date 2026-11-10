"""Scatter Points says nothing about a surface or prototype that is above it a moment later.

A script's chunk cooks once before it is wired (chunk_scene.place), when the Sublayer that
brings in the surface is not above it yet. Scatter logged "surface prim not found" and
"prototype path(s) not found" twice each while building a correct network; every benchmark
agent saw it, and some went looking for the fault. It now waits for the wire. A path still
missing once it is wired is reported as before.
"""
from __future__ import annotations

import logging

import pytest
from pxr import Usd, UsdGeom

from usd_chunks import Scene, chunk


@pytest.fixture
def terrain(tmp_path) -> str:
    stage = Usd.Stage.CreateNew(str(tmp_path / "terrain.usda"))
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    mesh = UsdGeom.Mesh.Define(stage, "/World/terrain")
    mesh.CreatePointsAttr([(-5, 0, -5), (5, 0, -5), (5, 0, 5), (-5, 0, 5)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 1])
    stage.GetRootLayer().Save()
    return str(tmp_path / "terrain.usda")


def _not_found(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "not found" in r.getMessage()]


@pytest.mark.parametrize("in_block", [False, True])
def test_a_wired_scatter_builds_without_not_found_warnings(terrain, caplog, in_block) -> None:
    caplog.set_level(logging.WARNING)

    def build(scene):
        last = scene.create.sublayer(imports=[terrain])
        last = scene.create.usd_sphere(last, prim_path="/prototypes/rock", sphere_radius=0.2)
        return scene.create.scatter_points(
            last, name="Rocks", surface_prim="/World/terrain", inst_path="/World/rocks",
            prototypes=[{"proto_path": "/prototypes/rock"}],
        )

    with Scene.new() as scene:
        if in_block:
            with chunk.change_block():
                rocks = build(scene)
        else:
            rocks = build(scene)
        scene.set_output(rocks)
        instancer = UsdGeom.PointInstancer(scene.get_stage().GetPrimAtPath("/World/rocks"))
        assert len(instancer.GetPositionsAttr().Get() or []) > 0, "the wired cook placed nothing"

    assert _not_found(caplog) == []


def test_a_surface_still_missing_once_wired_is_reported(terrain, caplog) -> None:
    caplog.set_level(logging.WARNING)
    with Scene.new() as scene:
        last = scene.create.sublayer(imports=[terrain])
        scene.set_output(scene.create.scatter_points(last, name="Rocks", surface_prim="/World/terrian"))

    assert any("surface prim not found: /World/terrian" in m for m in _not_found(caplog))
