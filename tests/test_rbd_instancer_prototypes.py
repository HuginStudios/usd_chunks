"""A PointInstancer with several prototypes must collide each instance with its OWN shape.

Reported live: a Scatter Points instancer fed a set of differently-sized rocks into the RBD
Solver, and the pile behaved as though every rock were the first one -- small rocks resting
on nothing, big rocks sunk into the floor.

The cause is that the solver only ever looked at ``GetPrototypesRel().GetTargets()[0]``:
one bounding box and one convex hull were measured from prototype 0 and then handed to
every body, and ``protoIndices`` -- the array that says which prototype each instance
actually is -- was never read at all.

Both collider families are checked below, because they fail in different places: the
sphere/box sizes come from the prototype's bound, and the hull comes from its points.
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, UsdGeom

import scripts.rbd_solver as rbd_mod

if not rbd_mod._HAS_MUJOCO:
    pytest.skip("mujoco not installed", allow_module_level=True)

from test_rbd_general_colliders import _box_mesh, _stage  # noqa: E402

SMALL = 2.0
BIG = 20.0


def _two_proto_instancer(stage, drop_from=60.0):
    """One small and one big box prototype, one instance of each, far enough apart to land
    on the ground rather than on each other."""
    _box_mesh(stage, "/protos/small/box", centre=(0.0, 0.0, 0.0), size=(SMALL,) * 3)
    _box_mesh(stage, "/protos/big/box", centre=(0.0, 0.0, 0.0), size=(BIG,) * 3)

    inst = UsdGeom.PointInstancer.Define(stage, "/scatter")
    inst.GetPrototypesRel().SetTargets([Sdf.Path("/protos/small"), Sdf.Path("/protos/big")])
    inst.GetPositionsAttr().Set([Gf.Vec3f(-60.0, 0.0, drop_from), Gf.Vec3f(60.0, 0.0, drop_from)])
    inst.GetProtoIndicesAttr().Set([0, 1])
    inst.GetScalesAttr().Set([Gf.Vec3f(1.0, 1.0, 1.0)] * 2)
    inst.GetOrientationsAttr().Set([Gf.Quath(1, 0, 0, 0)] * 2)
    return inst


def _settle(stage, inst, shape_type):
    layer = Sdf.Layer.CreateAnonymous("bake")
    stage.GetRootLayer().subLayerPaths.insert(0, layer.identifier)
    rbd_mod.simulate_mujoco_instancer(
        stage, inst.GetPrim(), layer, [], 12, 24.0, -981.0, 1.0, 20,
        [0, 0, 0], [0, 0, 0], 1, shape_type=shape_type, settle_only=True,
    )
    return [float(p[2]) for p in inst.GetPositionsAttr().Get()]


@pytest.mark.parametrize("shape_type", ["Box", "Convex Hull"])
def test_each_instance_rests_on_its_own_prototype(shape_type):
    """A box of side N centred on its origin comes to rest with its origin N/2 up. Sizing
    every body from prototype 0 puts the big one 9 units into the floor instead."""
    stage = _stage()
    inst = _two_proto_instancer(stage)

    small_z, big_z = _settle(stage, inst, shape_type)

    assert small_z == pytest.approx(SMALL / 2.0, abs=0.3), (
        f"the small instance should rest at {SMALL / 2.0}, got {small_z:.2f}"
    )
    assert big_z == pytest.approx(BIG / 2.0, abs=0.6), (
        f"the big instance should rest at {BIG / 2.0}, got {big_z:.2f} -- "
        "it was sized from the wrong prototype"
    )
