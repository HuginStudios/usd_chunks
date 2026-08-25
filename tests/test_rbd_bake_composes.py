"""A baked simulation and the Transform that placed the body must both apply.

Two of these pass and two are xfail, and the split is the useful part: after a bake, the
Transform still works -- nudging it moves the chair correctly -- but the BAKE is what gets
lost, because the Transform's older copy of xformOpOrder wins and does not name the
solver's op.


Reported live: a chair wired Transform -> RBD Solver. Run the sim, then nudge the Transform,
and the chair jumped.

The solver baked into the plain `xformOp:transform` and hard-set the whole order to just
that. It sits downstream, so its layer outranks the Transform's and that one-item list won
outright -- the Transform's own op stayed on the prim, typed and valued, and was never read.
Placing the chair stopped working the moment a simulation existed.

The bake is authored directly here rather than by running MuJoCo: what is under test is the
SHAPE of what the bake writes, not the physics.
"""
from __future__ import annotations

import importlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk_gizmo, chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkConfig, SceneSender
from usd_chunks.plugins import chunk_usd

transform_mod = importlib.import_module("scripts.transform")
rbd_mod = importlib.import_module("scripts.rbd_solver")


class _Scene(SceneSender):
    def __init__(self, stage):
        super().__init__()
        self._stage = stage

    def get_stage(self):
        return self._stage

    def get_selected_prims(self):
        return []


@pytest.fixture
def chair():
    stage = Usd.Stage.CreateInMemory()
    scene_layer = Sdf.Layer.CreateAnonymous("scene")
    stage.GetRootLayer().subLayerPaths.append(scene_layer.identifier)
    with Usd.EditContext(stage, scene_layer):
        UsdGeom.Xform.Define(stage, "/chair")

    registry = chunk_registry.ChunkRegistry()
    sender = _Scene(stage)
    registry.initialize(sender, "pytest")
    chunk_registry.ChunkLayerManager().initialize(stage)
    chunk_usd.ChunkStageSync().initialize(stage, sender)
    return stage, sender


def _mk(sender, module, name, module_name):
    node = module.main()
    node._config = ChunkConfig(module=module_name)
    node._module = module
    node.name = name
    sender.add_chunk(node)
    return node


def _local(stage):
    matrix = UsdGeom.Xformable(stage.GetPrimAtPath("/chair")).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    matrix = matrix[0] if isinstance(matrix, tuple) else matrix
    return matrix.ExtractTranslation()


def _bake(stage, solver, fall=5.0):
    """Author what _write_prim_results authors, without running the solver."""
    prim = stage.GetPrimAtPath("/chair")
    rbd_mod._write_prim_results(
        stage,
        prim,
        solver.get_edit_context(),
        Gf.Matrix4d(1.0),
        [Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(0.0, -fall, 0.0))],
        1,
        None,
        solver,
    )


def _wired_pair(stage, sender):
    move = _mk(sender, transform_mod, "Move", "transform")
    move.get_param("primpath_location").value = "/chair"
    move.get_param("tx").value = 20.0
    move.execute()

    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/chair"
    solver.add_dependency(move)
    return move, solver


@pytest.mark.xfail(
    reason="xformOpOrder has no correct home yet. Every chunk writes its own copy into its "
           "own layer, and USD keeps the STRONGEST rather than the most recent -- so the "
           "Transform's copy, written before the solver had ever baked, wins and omits the "
           "solver's op. The bake is on the prim, typed and valued, and never read. Moving "
           "the order to a layer shared by the graph fixes exactly this and breaks four "
           "other tests, because the order then stops muting with its chunk; see "
           "chunk_utils._xform_order_layer_unused for what that costs.",
    strict=True,
)
def test_the_bake_uses_the_solvers_own_op(chair) -> None:
    stage, sender = chair
    _move, solver = _wired_pair(stage, sender)

    _bake(stage, solver)

    order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/chair")).GetXformOpOrderAttr().Get())
    assert chunk_utils.xform_op_name(solver) in order
    assert "xformOp:transform" not in order, (
        "the shared op is what let the solver evict everyone else"
    )


def test_the_transforms_op_survives_the_bake(chair) -> None:
    """The heart of it: the Transform must still be in the order after a sim."""
    stage, sender = chair
    move, solver = _wired_pair(stage, sender)

    _bake(stage, solver)

    order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/chair")).GetXformOpOrderAttr().Get())
    assert chunk_utils.xform_op_name(move) in order


@pytest.mark.xfail(reason="same xformOpOrder homing problem", strict=True)
def test_the_placement_and_the_sim_compose(chair) -> None:
    stage, sender = chair
    _move, solver = _wired_pair(stage, sender)

    _bake(stage, solver, fall=5.0)

    assert _local(stage) == pytest.approx(Gf.Vec3d(20.0, -5.0, 0.0)), (
        "placed at x=20 by the Transform, dropped 5 by the sim -- both, not one"
    )


def test_nudging_the_transform_after_a_bake_moves_the_chair(chair) -> None:
    """The reported symptom. Adjusting the Transform after a sim has to move the body by
    what was asked, not throw it somewhere else."""
    stage, sender = chair
    move, solver = _wired_pair(stage, sender)
    _bake(stage, solver, fall=5.0)

    move.get_param("tx").value = 21.0

    assert _local(stage) == pytest.approx(Gf.Vec3d(21.0, -5.0, 0.0))


def test_the_solver_still_bakes_alone(chair) -> None:
    """No Transform in the chain at all -- the ordinary case must keep working."""
    stage, sender = chair
    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/chair"

    _bake(stage, solver, fall=3.0)

    assert _local(stage) == pytest.approx(Gf.Vec3d(0.0, -3.0, 0.0))
