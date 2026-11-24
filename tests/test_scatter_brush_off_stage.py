"""A Scatter Brush outside the chain the Output cooks declines a stroke and says why.

Only the chunks feeding the active Output or View are cooked into the stage. Make two brushes
one after the other and the Output can end up wired to the second; painting the first failed
inside the stroke ("Layer ... is not in the local LayerStack"), logged a traceback and lost
the stroke without saying what was wrong. Such a chunk is blocked: its own callbacks (stroke
end, Fill, Clear, Show Paint) do nothing, so the stroke is declined the same way, with one
warning that says to wire it in.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene
from usd_chunks.gizmo import runtime as chunk_gizmo

FLOOR = "/World/Floor"
SIZE = 20.0
DIVISIONS = 20


def _floor(stage) -> None:
    mesh = UsdGeom.Mesh.Define(stage, FLOOR)
    row = DIVISIONS + 1
    step = SIZE / DIVISIONS
    mesh.GetPointsAttr().Set([Gf.Vec3f(ix * step, iy * step, 0.0) for iy in range(row) for ix in range(row)])
    mesh.GetFaceVertexCountsAttr().Set([4] * DIVISIONS * DIVISIONS)
    mesh.GetFaceVertexIndicesAttr().Set([
        i for iy in range(DIVISIONS) for ix in range(DIVISIONS)
        for i in (iy * row + ix, iy * row + ix + 1, (iy + 1) * row + ix + 1, (iy + 1) * row + ix)
    ])


def _paint(brush, x: float) -> None:
    brush.start_gizmo()
    chunk_gizmo.apply_stroke(brush, "paint", [(Gf.Vec3d(x, 10.0, 0.0), 1.0)])
    brush.stop_gizmo()


def _painted_xs(stage, brush) -> np.ndarray:
    primvar = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath(FLOOR)).GetPrimvar(brush.get_param("density_name").value)
    if not primvar or not primvar.HasValue():
        return np.zeros(0)
    weights = np.asarray(primvar.ComputeFlattened())
    points = np.asarray(UsdGeom.Mesh(stage.GetPrimAtPath(FLOOR)).GetPointsAttr().Get())
    return points[weights > 0.0, 0]


@pytest.fixture
def scene():
    """(scene, stage, off, on): two brushes; the Output is wired to `on`, so `off` is off the stage."""
    with Scene.new() as scene:
        stage = scene.get_stage()
        _floor(stage)
        off = scene.create.scatter_brush(surface_prim=FLOOR, radius=3.0, strength=1.0)
        on = scene.create.scatter_brush(surface_prim=FLOOR, radius=3.0, strength=1.0)
        scene.set_output(on)
        assert not stage.HasLocalLayer(off.get_edit_context()), "the setup needs a brush off the stage"
        yield scene, stage, off, on


def test_a_brush_outside_the_chain_declines_and_says_why(scene, caplog) -> None:
    _, stage, off, _ = scene

    with caplog.at_level(logging.WARNING):
        _paint(off, 3.0)
        _paint(off, 4.0)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    said = [r for r in caplog.records if "not in the chain that ends at the Output" in r.getMessage()]
    assert len(said) == 1, "once, not once per stroke"
    assert not len(_painted_xs(stage, off))


def test_the_stroke_is_declined(scene) -> None:
    _, _, off, _ = scene

    off.start_gizmo()
    handled = chunk_gizmo.apply_stroke(off, "paint", [(Gf.Vec3d(3.0, 10.0, 0.0), 1.0)])
    off.stop_gizmo()

    assert handled is False


def test_wired_in_it_paints(scene, caplog) -> None:
    scene_, stage, off, _ = scene
    _paint(off, 3.0)

    scene_.set_output(off)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _paint(off, 3.0)

    xs = _painted_xs(stage, off)
    assert len(xs) and xs.max() < SIZE / 2
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_points_mode_declines_too(scene, caplog) -> None:
    _, stage, off, _ = scene
    off.get_param("output").value = "Points"

    with caplog.at_level(logging.WARNING):
        _paint(off, 3.0)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
