"""Two Scatter Brushes on one mesh must each keep their own field.

Reported: one brush paints where vines may grow on a cliff, a second paints grass on the
clifftop of the same rock. The vine then grew on the clifftop. Every brush wrote the same
primvar (`scatterDensity`), so the stronger brush layer won on the shared mesh and both
consumers read its paint. Toggling Show Paint on one brush then re-read the composed field and
wrote the other brush's paint into its own layer.
"""

from __future__ import annotations

import numpy as np
from pxr import Gf, Usd, UsdGeom

from usd_chunks import chunk_registry
from usd_chunks.chunk_scene import SceneSender
from usd_chunks.chunk_stage import initialize as chunk_usd_initialize
from usd_chunks.gizmo import runtime as chunk_gizmo

FLOOR = "/World/Floor"
SIZE = 20.0
DIVISIONS = 20


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _scene_for(stage: Usd.Stage) -> SceneSender:
    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    scene = _Staged()
    chunk_registry.ChunkRegistry().initialize(scene, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd_initialize(stage, scene)
    return scene


def _floor(stage: Usd.Stage) -> None:
    mesh = UsdGeom.Mesh.Define(stage, FLOOR)
    step = SIZE / DIVISIONS
    row = DIVISIONS + 1
    mesh.GetPointsAttr().Set(
        [Gf.Vec3f(ix * step, iy * step, 0.0) for iy in range(row) for ix in range(row)]
    )
    mesh.GetFaceVertexCountsAttr().Set([4] * DIVISIONS * DIVISIONS)
    mesh.GetFaceVertexIndicesAttr().Set(
        [
            i
            for iy in range(DIVISIONS)
            for ix in range(DIVISIONS)
            for i in (iy * row + ix, iy * row + ix + 1, (iy + 1) * row + ix + 1, (iy + 1) * row + ix)
        ]
    )


def _add_brush(scene: SceneSender):
    mod = chunk_registry.ChunkRegistry().get_chunk_module("scatter_brush")
    brush = (mod.create() if mod.loaded else mod.load())[0]
    scene.add_chunk(brush)
    brush.get_param("surface_prim").value = FLOOR
    brush.get_param("radius").value = 3.0
    brush.get_param("strength").value = 1.0
    return brush


def _paint(brush, x: float, y: float) -> None:
    brush.start_gizmo()
    chunk_gizmo.apply_stroke(brush, "paint", [(Gf.Vec3d(x, y, 0.0), 1.0)])
    brush.stop_gizmo()


def _field(stage: Usd.Stage, brush) -> np.ndarray:
    """The field a consumer wired to `brush` reads: the composed primvar its Density Field names."""
    name = brush.get_param("density_name").value
    primvar = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(FLOOR)).GetPrimvar(name)
    assert primvar and primvar.HasValue(), f"no field named {name!r} on the floor"
    return np.asarray(primvar.ComputeFlattened(), dtype=np.float64)


def _xs(stage: Usd.Stage, weights: np.ndarray) -> np.ndarray:
    points = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath(FLOOR)).GetPointsAttr().Get())
    return points[weights > 0.0, 0]


def _two_brushes():
    stage = Usd.Stage.CreateInMemory()
    _floor(stage)
    scene = _scene_for(stage)
    left, right = _add_brush(scene), _add_brush(scene)
    _paint(left, 3.0, 10.0)
    _paint(right, 17.0, 10.0)
    return stage, left, right


def test_each_brush_names_its_own_field() -> None:
    _, left, right = _two_brushes()

    assert left.get_param("density_name").value != right.get_param("density_name").value


def test_each_field_holds_only_its_own_brushs_paint() -> None:
    stage, left, right = _two_brushes()

    left_xs = _xs(stage, _field(stage, left))
    right_xs = _xs(stage, _field(stage, right))

    assert len(left_xs) and left_xs.max() < SIZE / 2
    assert len(right_xs) and right_xs.min() > SIZE / 2


def test_toggling_show_paint_keeps_the_other_brushs_paint_out() -> None:
    stage, left, right = _two_brushes()

    for value in (False, True, False):
        left.get_param("show_paint").value = value
        right.get_param("show_paint").value = value

    assert _xs(stage, _field(stage, left)).max() < SIZE / 2
    assert _xs(stage, _field(stage, right)).min() > SIZE / 2
