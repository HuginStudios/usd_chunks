"""Points painted with Scatter Brush have to survive a save and reopen.

Reported: paint points (Output: Points or Spray), save the scene, reopen, and the points are
gone. A saved scene is chunk prims and nothing else (chunk_stage.export_chunks); the painted
density field is recorded on the chunk prim for exactly that reason, but placed points lived
only in the chunk's cook layer and in memory, and neither is written.
"""

from __future__ import annotations

import pathlib

import pytest
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as chunk_usd_initialize, save_scene
from usd_chunks.gizmo import runtime as chunk_gizmo


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage: Usd.Stage) -> SceneSender:
    """A scene wired to `stage`, initialised the way opening a file does."""

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Staged()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_initialize(stage, scene)
    return scene


def _floor(stage, path="/World/Floor", size=20.0):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set([(0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0)])
    mesh.GetFaceVertexCountsAttr().Set([4])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
    return mesh


def _add_brush(scene: SceneSender, output: str):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("scatter_brush")
    brush = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(brush)
    brush.get_param("surface_prim").value = "/World/Floor"
    brush.get_param("output").value = output
    brush.get_param("radius").value = 2.0
    brush.get_param("strength").value = 1.0
    return brush


def _placed(stage: Usd.Stage, brush) -> list:
    path = brush.get_param("points_path").value
    prim = stage.GetPrimAtPath(path) if path else None
    values = UsdGeom.Points(prim).GetPointsAttr().Get() if prim and prim.IsValid() else None
    return [tuple(p) for p in values] if values else []


def _reopen(scene: SceneSender, tmp_path):
    out = pathlib.Path(tmp_path) / "scene.usda"
    save_scene(scene, out)
    chunk_registry.ChunkLayerManager().reset()
    reopened = Usd.Stage.Open(str(out))
    _floor(reopened)
    reopened_scene = _scene_for(reopened)
    brushes = [
        c for c in reopened_scene.get_chunks()
        if c.get_config() and c.get_config().module == "scatter_brush"
    ]
    return reopened, brushes


@pytest.mark.parametrize("output", ["Points", "Spray"])
def test_painted_points_are_still_there_after_a_reopen(tmp_path, output) -> None:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    _floor(stage)
    scene = _scene_for(stage)
    brush = _add_brush(scene, output)

    brush.start_gizmo()
    chunk_gizmo.apply_stroke(brush, "paint", [(Gf.Vec3d(5, 5, 0), 1.0, (0, 0, 1))])
    chunk_gizmo.apply_stroke(brush, "paint", [(Gf.Vec3d(12, 12, 0), 1.0, (0, 0, 1))])
    brush.stop_gizmo()

    before = _placed(stage, brush)
    assert before, "the strokes have to place something for this to mean anything"

    reopened, brushes = _reopen(scene, tmp_path)

    assert len(brushes) == 1
    after = sorted(_placed(reopened, brushes[0]))
    # Point by point: pytest 9.2's approx no longer takes a list of tuples.
    assert len(after) == len(before)
    for got, want in zip(after, sorted(before)):
        assert got == pytest.approx(want)


def _painted_scene(output):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    _floor(stage)
    scene = _scene_for(stage)
    brush = _add_brush(scene, output)
    brush.start_gizmo()
    chunk_gizmo.apply_stroke(brush, "paint", [(Gf.Vec3d(5, 5, 0), 1.0, (0, 0, 1))])
    brush.stop_gizmo()
    assert _placed(stage, brush)
    return stage, scene, brush


def test_clearing_points_survives_a_reopen(tmp_path) -> None:
    """The record follows a Clear too, or the cleared points come back on reopen."""
    stage, scene, brush = _painted_scene("Points")
    module = chunk_registry.ChunkRegistry().get_chunk_module("scatter_brush").module
    module._set_everywhere(brush, 0.0)  # what the Clear button calls
    assert _placed(stage, brush) == []

    reopened, brushes = _reopen(scene, tmp_path)

    assert _placed(reopened, brushes[0]) == []


def test_a_stroke_after_a_reopen_adds_to_the_saved_points(tmp_path) -> None:
    stage, scene, brush = _painted_scene("Points")
    before = _placed(stage, brush)

    reopened, brushes = _reopen(scene, tmp_path)
    again = brushes[0]
    again.start_gizmo()
    chunk_gizmo.apply_stroke(again, "paint", [(Gf.Vec3d(15, 15, 0), 1.0, (0, 0, 1))])
    again.stop_gizmo()

    assert len(_placed(reopened, again)) == len(before) + 1
