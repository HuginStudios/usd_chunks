"""A Camera's lens is in the stage's units: tenths of a scene unit, as UsdGeom.Camera keeps it.

The chunk's params are millimetres and were written as they are, which is right only in a
centimetre scene. In metres (every Scene.new) a 50 mm lens became 5 m: the framing, a ratio,
stayed right, but depth of field uses the real lens and blurred about a hundred times too much.
A cliff scene needed f-stop 280 to look like f/2.8.
"""

from __future__ import annotations

import pytest
from pxr import Gf, UsdGeom

from usd_chunks import Scene


def _camera(meters_per_unit: float, **lens):
    """(Gf.Camera of the prim, stage) for a new Camera chunk in a stage of these units."""
    scene = Scene.new().__enter__()
    stage = scene.get_stage()
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    camera = scene.create.camera()
    if lens:
        scene.set(camera, **lens)
    prim = UsdGeom.Camera(stage.GetPrimAtPath(camera.get_param("primpath_location").value))
    return prim.GetCamera(), scene


@pytest.mark.parametrize("meters_per_unit", [1.0, 0.01])
def test_a_50mm_lens_is_50mm_in_any_units(meters_per_unit) -> None:
    gf, scene = _camera(meters_per_unit, focalLength=50.0, horizontalAperture=36.0)
    try:
        # Gf.Camera.FOCAL_LENGTH_UNIT: the attribute's unit, a tenth of a scene unit.
        metres = gf.focalLength * Gf.Camera.FOCAL_LENGTH_UNIT * meters_per_unit
        sensor = gf.horizontalAperture * Gf.Camera.APERTURE_UNIT * meters_per_unit
        assert metres == pytest.approx(0.050)
        assert sensor == pytest.approx(0.036)
    finally:
        scene.__exit__(None, None, None)


def test_the_framing_is_the_same_in_metres_and_centimetres() -> None:
    fovs = []
    for meters_per_unit in (1.0, 0.01):
        gf, scene = _camera(meters_per_unit, focalLength=35.0)
        try:
            fovs.append(gf.GetFieldOfView(Gf.Camera.FOVHorizontal))
        finally:
            scene.__exit__(None, None, None)
    assert fovs[0] == pytest.approx(fovs[1])


def test_f_stop_and_focus_keep_their_values() -> None:
    """f-stop has no unit, and focus distance is in scene units in both the params and USD."""
    gf, scene = _camera(1.0, fStop=2.8, focusDistance=2.3)
    try:
        assert gf.fStop == pytest.approx(2.8)
        assert gf.focusDistance == pytest.approx(2.3)
    finally:
        scene.__exit__(None, None, None)
