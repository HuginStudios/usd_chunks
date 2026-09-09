"""RBD Solver reads authored UsdPhysics, and falls back to its params when there is none.

A sim-ready asset already states its mass, contact properties and collider approximation.
Before this the solver re-derived all of that from global sliders, so it could not usefully
consume a library asset -- the part the asset's author was careful about was discarded.

Fixtures author real UsdPhysics rather than shipping a third-party asset: the schemas are
the standard, so building against them keeps the tests deterministic and license-clean.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rbd_solver as rbd  # noqa: E402


DEFAULTS = dict(
    gravity=-9.81,
    mass=1.0,
    friction=0.5,
    restitution=0.0,
    shape_type="Convex Hull",
)


def _stage_with_box():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    return stage, UsdGeom.Cube.Define(stage, "/World/box").GetPrim()


def _bind_physics_material(stage, prim, **attrs):
    material = UsdShade.Material.Define(stage, "/World/PhysMat")
    api = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
    if "friction" in attrs:
        api.CreateDynamicFrictionAttr(attrs["friction"])
    if "static_friction" in attrs:
        api.CreateStaticFrictionAttr(attrs["static_friction"])
    if "restitution" in attrs:
        api.CreateRestitutionAttr(attrs["restitution"])
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, materialPurpose="physics")
    return api


def test_nothing_authored_leaves_every_param_alone():
    """The no-regression case: a scene with no UsdPhysics simulates as it always did."""
    stage, prim = _stage_with_box()

    result = rbd._authored_physics(stage, prim, DEFAULTS)

    for key, value in DEFAULTS.items():
        assert result[key] == value
    assert result["kinematic"] is False


def test_authored_mass_wins_over_the_param():
    stage, prim = _stage_with_box()
    UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(7.5)

    assert rbd._authored_physics(stage, prim, DEFAULTS)["mass"] == pytest.approx(7.5)


def test_an_applied_schema_with_nothing_authored_does_not_override():
    """The subtle one. Every UsdPhysics attribute has a schema fallback, so Get() always
    returns a value -- 0.0 for mass. Reading that instead of checking HasAuthoredValue
    would let merely *applying* MassAPI silently zero the user's Mass param."""
    stage, prim = _stage_with_box()
    UsdPhysics.MassAPI.Apply(prim)  # applied, but nothing authored

    assert rbd._authored_physics(stage, prim, DEFAULTS)["mass"] == DEFAULTS["mass"]


def test_bound_physics_material_supplies_friction_and_restitution():
    stage, prim = _stage_with_box()
    _bind_physics_material(stage, prim, friction=0.42, restitution=0.9)

    result = rbd._authored_physics(stage, prim, DEFAULTS)

    assert result["friction"] == pytest.approx(0.42, rel=1e-6)
    assert result["restitution"] == pytest.approx(0.9, rel=1e-6)


def test_static_friction_is_used_when_dynamic_is_absent():
    stage, prim = _stage_with_box()
    _bind_physics_material(stage, prim, static_friction=0.3)

    assert rbd._authored_physics(stage, prim, DEFAULTS)["friction"] == pytest.approx(0.3, rel=1e-6)


def test_a_shading_material_is_not_mistaken_for_a_physics_one():
    """Physics materials bind on their own purpose. A prim's look must not set its friction."""
    stage, prim = _stage_with_box()
    look = UsdShade.Material.Define(stage, "/World/Look")
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(look)

    assert rbd._authored_physics(stage, prim, DEFAULTS)["friction"] == DEFAULTS["friction"]


@pytest.mark.parametrize(
    "approximation,expected",
    [
        ("convexHull", "Convex Hull"),
        ("boundingCube", "Box"),
        ("boundingSphere", "Sphere"),
        ("meshSimplification", "Convex Hull"),
    ],
)
def test_mapped_approximations_pick_the_shape_type(approximation, expected):
    stage, prim = _stage_with_box()
    UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr(approximation)

    assert rbd._authored_physics(stage, prim, DEFAULTS)["shape_type"] == expected


@pytest.mark.parametrize("approximation", ["convexDecomposition", "sdf"])
def test_unmapped_approximations_keep_the_param(approximation, caplog):
    """These need several geoms or a distance field. Silently collapsing them to a convex
    hull would simulate differently from what the asset asks for, so the param stands and
    the mismatch is reported (at debug level -- this runs every cook, not just once, so it
    must not spam the terminal by default)."""
    stage, prim = _stage_with_box()
    UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr(approximation)

    with caplog.at_level(logging.DEBUG, logger="rbd_solver"):
        result = rbd._authored_physics(stage, prim, DEFAULTS)

    assert result["shape_type"] == DEFAULTS["shape_type"]
    assert approximation in caplog.text


def test_kinematic_rigid_body_is_reported():
    stage, prim = _stage_with_box()
    UsdPhysics.RigidBodyAPI.Apply(prim).CreateKinematicEnabledAttr(True)

    assert rbd._authored_physics(stage, prim, DEFAULTS)["kinematic"] is True


def test_scene_gravity_is_read_as_a_signed_scalar_along_up():
    stage, prim = _stage_with_box()
    scene = UsdPhysics.Scene.Define(stage, "/World/physicsScene")
    scene.CreateGravityMagnitudeAttr(981.0)
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))

    assert rbd._authored_physics(stage, prim, DEFAULTS)["gravity"] == pytest.approx(-981.0)


def test_gravity_pointing_along_up_keeps_its_sign():
    """Magnitude is unsigned in the schema; the direction decides which way it pulls."""
    stage, prim = _stage_with_box()
    scene = UsdPhysics.Scene.Define(stage, "/World/physicsScene")
    scene.CreateGravityMagnitudeAttr(500.0)
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, 1))

    assert rbd._authored_physics(stage, prim, DEFAULTS)["gravity"] == pytest.approx(500.0)


def test_a_scene_with_no_authored_magnitude_leaves_gravity_alone():
    stage, prim = _stage_with_box()
    UsdPhysics.Scene.Define(stage, "/World/physicsScene")  # applied, nothing authored

    assert rbd._authored_physics(stage, prim, DEFAULTS)["gravity"] == DEFAULTS["gravity"]


def test_a_fully_described_asset_supplies_everything_at_once():
    """What consuming a sim-ready library asset actually looks like."""
    stage, prim = _stage_with_box()
    UsdPhysics.RigidBodyAPI.Apply(prim)
    UsdPhysics.CollisionAPI.Apply(prim)
    UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(2.4)
    UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr("boundingCube")
    _bind_physics_material(stage, prim, friction=0.8, restitution=0.1)
    scene = UsdPhysics.Scene.Define(stage, "/World/physicsScene")
    scene.CreateGravityMagnitudeAttr(9.81)
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))

    result = rbd._authored_physics(stage, prim, DEFAULTS)

    assert result["mass"] == pytest.approx(2.4)
    assert result["friction"] == pytest.approx(0.8, rel=1e-6)
    assert result["restitution"] == pytest.approx(0.1, rel=1e-6)
    assert result["shape_type"] == "Box"
    assert result["gravity"] == pytest.approx(-9.81)
    assert result["kinematic"] is False
