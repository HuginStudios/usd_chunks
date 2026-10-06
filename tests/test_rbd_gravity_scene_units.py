"""The RBD Solver's default gravity follows the stage's units.

It was a fixed -981, right only in a centimetre scene. In a metre-scale set a ball dropped
4 units hit the ground on frame 4 instead of about frame 22. Now Gravity 0 (the default)
means Earth gravity in this stage's units (sim_bake.gravity, shared with the Particle
Solver), and a value set by hand is used as it is.
"""

from __future__ import annotations

import pytest
from pxr import UsdGeom

from usd_chunks import Scene, sim_bake


@pytest.mark.parametrize("meters_per_unit, expected", [(0.01, -981.0), (1.0, -9.81), (0.1, -98.1)])
def test_default_gravity_is_earth_gravity_in_scene_units(meters_per_unit, expected) -> None:
    with Scene() as scene:
        UsdGeom.SetStageMetersPerUnit(scene.get_stage(), meters_per_unit)
        rbd = scene.create.rbd_solver()
        assert sim_bake.gravity(rbd.get_param("gravity").value, scene.get_stage()) == pytest.approx(expected)


def test_a_gravity_set_by_hand_is_used_as_it_is() -> None:
    with Scene() as scene:
        UsdGeom.SetStageMetersPerUnit(scene.get_stage(), 1.0)
        rbd = scene.create.rbd_solver(gravity=-3.7)
        assert sim_bake.gravity(rbd.get_param("gravity").value, scene.get_stage()) == pytest.approx(-3.7)
