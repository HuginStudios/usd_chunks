"""get_upstream_stage keeps the live stage's up axis and units.

Live-reported: on a Z-up stage, Curves to Mesh's Ribbon stood the road up as a wall. It reads
the up axis off the upstream stage, whose fresh root layer had none and so read as Y.
"""
from __future__ import annotations

from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry, chunk_stage
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as _chunk_usd_init


def _setup(up="Z", mpu=0.01):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up)
    UsdGeom.SetStageMetersPerUnit(stage, mpu)

    class _StageScene(SceneSender):
        def get_stage(self):
            return stage

        def get_selected_prims(self):
            return []

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    _chunk_usd_init(stage, scene)
    return stage, scene, reg


def _mk(reg, scene, name):
    mod = reg.get_chunk_module(name)
    c = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(c)
    return c


def test_the_upstream_stage_has_the_live_stages_up_axis_and_units():
    stage, scene, reg = _setup(up="Z", mpu=0.01)
    reader = _mk(reg, scene, "prim")

    up = chunk_stage.get_upstream_stage(stage, scene, reader)

    assert UsdGeom.GetStageUpAxis(up) == UsdGeom.Tokens.z
    assert UsdGeom.GetStageMetersPerUnit(up) == 0.01


def test_a_ribbon_lies_flat_on_a_z_up_stage():
    stage, scene, reg = _setup(up="Z", mpu=1.0)
    curves = UsdGeom.BasisCurves.Define(stage, "/road")
    curves.CreateTypeAttr().Set(UsdGeom.Tokens.linear)
    curves.CreateCurveVertexCountsAttr().Set([3])
    curves.CreatePointsAttr().Set([Gf.Vec3f(0, 0, 0), Gf.Vec3f(10, 0, 0), Gf.Vec3f(20, 5, 0)])
    curves.CreateWidthsAttr().Set([4.0, 4.0, 4.0])
    curves.SetWidthsInterpolation(UsdGeom.Tokens.vertex)

    sweep = _mk(reg, scene, "curves_to_mesh")
    sweep.get_param("primpath_location").value = "/road_mesh"
    sweep.get_param("profile").value = "Ribbon"
    sweep.get_param("curves_prim").value = "/road"

    points = UsdGeom.Mesh(stage.GetPrimAtPath("/road_mesh")).GetPointsAttr().Get()
    assert points, "the sweep authored no mesh"
    assert max(abs(p[2]) for p in points) < 1e-5, "the ribbon stands up off a Z-up ground"
    assert max(abs(p[1]) for p in points) > 1.0, "the ribbon has no width across the ground"
