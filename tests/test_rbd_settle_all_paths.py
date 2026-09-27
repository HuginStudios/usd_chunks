"""Settle To Rest applies to every kind of target, not just articulated assets.

The checkbox was only ever wired into the articulated branch of _run. Tick it on a scatter
pile or a single prop -- which is most of what it is actually reached for -- and it did
nothing at all: the solver baked the full animation exactly as if it were off. The tooltip
said "Articulated assets only", but nothing in the UI said so at the moment of ticking it,
so it read as the feature being unreliable rather than absent.

What a settle owes its caller, on any path:

* the resting pose written as a DEFAULT with no time samples, because a prim carrying
  PhysicsRigidBodyAPI and animated transforms at once is contradictory -- a downstream
  simulator ends up fighting the keyframes;
* that pose taken once things have actually stopped moving, rather than wherever the frame
  count happened to end;
* no change at all to a normal animated bake.

The instancer case carries an extra obligation, which is why it asserts on Default()
explicitly: Scatter Points authors its layout as a default in a weaker layer, so a bake
that writes only time samples leaves anything reading the instancer at Default() -- rather
than at a frame -- resolving straight past the simulation to the raw scattered layout.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chunks"))
import rbd_solver as rbd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rbd_general_colliders import _box_mesh, _stage  # noqa: E402

needs_mujoco = pytest.mark.skipif(not rbd._HAS_MUJOCO, reason="needs mujoco")

pytestmark = needs_mujoco

FRAMES = 12  # deliberately far too few to settle from this height


def _baked_layer(stage):
    layer = Sdf.Layer.CreateAnonymous("bake")
    stage.GetRootLayer().subLayerPaths.insert(0, layer.identifier)
    return layer


def _drop_prim(stage, settle_only):
    """Drop a box onto the ground plane through the real single-prim entry point."""
    _box_mesh(stage, "/crayon", centre=(0.0, 0.0, 120.0), size=(2.0, 2.0, 2.0))
    prim = stage.GetPrimAtPath("/crayon")
    rbd.simulate_mujoco(
        stage, prim, _baked_layer(stage), FRAMES, 24.0, -981.0, 1.0, 20,
        [0, 0, 0], [0, 0, 0], 1, Gf.Matrix4d(1.0),
        collision_prims=[], settle_only=settle_only,
    )
    return prim


def _scatter(stage, settle_only, drop_from=120.0):
    """Drop a small grid of instances onto the ground plane, as Scatter Points would."""
    _box_mesh(stage, "/proto/crayon", centre=(0.0, 0.0, 0.0), size=(1.2, 1.2, 3.0))
    inst = UsdGeom.PointInstancer.Define(stage, "/scatter")
    inst.GetPrototypesRel().SetTargets([Sdf.Path("/proto")])
    grid = [(px, py) for px in (-6.0, 0.0, 6.0) for py in (-5.0, 5.0)]
    inst.GetPositionsAttr().Set([Gf.Vec3f(px, py, drop_from) for px, py in grid])
    inst.GetProtoIndicesAttr().Set([0] * len(grid))
    inst.GetOrientationsAttr().Set([Gf.Quath(1, 0, 0, 0)] * len(grid))

    rbd.simulate_mujoco_instancer(
        stage, inst.GetPrim(), _baked_layer(stage), [], FRAMES, 24.0, -981.0, 1.0, 20,
        [0, 0, 0], [0, 0, 0], 1, shape_type="Box", settle_only=settle_only,
    )
    return inst


def _op_attr(prim):
    ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
    assert ops, "the bake must have authored an xform op"
    return ops[0].GetAttr()


class TestSinglePrim:
    def test_settle_writes_a_default_and_no_samples(self):
        stage = _stage()
        prim = _drop_prim(stage, settle_only=True)

        attr = _op_attr(prim)
        assert attr.GetTimeSamples() == [], "a settle must not animate the prim"
        assert attr.Get() is not None, "the resting pose belongs in the default"

    def test_the_settled_pose_is_lower_than_where_it_started(self):
        """It really is the end of a drop, not the pose it was authored at."""
        stage = _stage()
        prim = _drop_prim(stage, settle_only=True)

        settled_z = _op_attr(prim).Get().ExtractTranslation()[2]
        assert settled_z < -50.0, f"the body should have fallen; moved {settled_z:.1f}"

    def test_settling_outlasts_the_frame_count(self):
        """FRAMES is far too short to land from this height, so a settle that merely took
        the last frame would stop mid-air. The extra stepping is what makes it land."""
        stage = _stage()
        settled = _drop_prim(stage, settle_only=True)
        settled_z = _op_attr(settled).Get().ExtractTranslation()[2]

        stage2 = _stage()
        animated = _drop_prim(stage2, settle_only=False)
        last_frame_z = _op_attr(animated).Get(Usd.TimeCode(FRAMES)).ExtractTranslation()[2]

        assert settled_z < last_frame_z - 1.0, (
            f"settled {settled_z:.1f} should be well below frame {FRAMES}'s {last_frame_z:.1f}"
        )

    def test_an_animated_bake_is_unchanged(self):
        stage = _stage()
        prim = _drop_prim(stage, settle_only=False)

        assert len(_op_attr(prim).GetTimeSamples()) == FRAMES


class TestPointInstancer:
    def test_settle_writes_a_default_and_no_samples(self):
        stage = _stage()
        inst = _scatter(stage, settle_only=True)

        attr = inst.GetPositionsAttr()
        assert attr.GetTimeSamples() == [], "a settle must not animate the instancer"
        assert attr.Get() is not None

    def test_the_default_holds_the_simulation_not_the_scattered_layout(self):
        """The reason a settled instancer must write a default at all.

        Scatter Points authors its layout as a default in a weaker layer. A bake that
        writes only time samples resolves past that simulation at Default() -- so anything
        reading the instancer there, rather than at a frame, gets the un-simulated pile.
        """
        stage = _stage()
        inst = _scatter(stage, settle_only=True, drop_from=120.0)

        at_default = inst.GetPositionsAttr().Get(Usd.TimeCode.Default())
        assert at_default is not None
        heights = [float(p[2]) for p in at_default]
        assert max(heights) < 120.0 - 1.0, (
            f"Default() still resolves to the un-simulated layout: {heights}"
        )

    def test_instances_come_to_rest_rather_than_stopping_mid_air(self):
        stage = _stage()
        inst = _scatter(stage, settle_only=True, drop_from=120.0)

        heights = np.array([float(p[2]) for p in inst.GetPositionsAttr().Get()])
        # They land on the ground plane at z=0; the prototype is 3 units tall.
        assert np.all(heights < 10.0), f"instances did not land: {heights}"
        assert np.all(np.isfinite(heights)), "the pile must not blow up"

    def test_an_animated_bake_is_unchanged(self):
        stage = _stage()
        inst = _scatter(stage, settle_only=False)

        assert len(inst.GetPositionsAttr().GetTimeSamples()) == FRAMES
