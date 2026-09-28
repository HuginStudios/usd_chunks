"""A baked simulation and the Transform that placed the body must both apply.


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

from usd_chunks import chunk_registry, chunk_utils
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender
from usd_chunks import chunk_stage

transform_mod = importlib.import_module("chunks.transforms.transform")
rbd_mod = importlib.import_module("chunks.dynamics.rbd_solver")


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
    chunk_stage.ChunkStageSync().initialize(stage, sender)
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


def _bake(stage, solver, ends_at):
    """Author what _write_prim_results authors, without running the solver.

    `ends_at` is where the simulation says the body finishes, in parent space -- absolute,
    which is what the real solver hands the writer. It must be consistent with where the
    body actually STARTS: a sim on a chair placed at x=20 ends near x=20, not near the
    origin. An earlier draft ignored that and fed a result of (0, -5, 0) for a chair at
    x=20, which is not a simulation of that chair at all, and made a correct bake look wrong.
    """
    rbd_mod._write_prim_results(
        stage,
        stage.GetPrimAtPath("/chair"),
        solver.get_edit_context(),
        Gf.Matrix4d(1.0),
        [Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*ends_at))],
        1,
        None,
        solver,
    )


def _downstream_wins(upstream, downstream):
    """Rank the downstream chunk's layer above the upstream one's, as a live graph does.

    Recook reorders each graph layer's subLayerPaths so the chunk nearest the view wins, so
    downstream is always the stronger of the two. Nothing establishes that in a short test
    without the Qt view machinery -- plain creation order decides instead, and the chunk
    created FIRST ends up stronger, which is the opposite of a real session. Forcing it here
    is reproducing ordinary state, not staging a special case.
    """
    manager = chunk_registry.ChunkLayerManager()
    up_layer = manager.get_layer(upstream)
    down_layer = manager.get_layer(downstream)
    graph = next(
        g for g in manager.graph_layers.values() if up_layer.identifier in g.subLayerPaths
    )
    graph.subLayerPaths = [down_layer.identifier] + [
        p for p in graph.subLayerPaths if p != down_layer.identifier
    ]


def _wired_pair(stage, sender):
    move = _mk(sender, transform_mod, "Move", "transform")
    move.get_param("primpath_location").value = "/chair"
    move.get_param("tx").value = 20.0
    move.execute()

    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/chair"
    solver.add_dependency(move)
    _downstream_wins(move, solver)
    return move, solver


def test_the_bake_uses_the_solvers_own_op(chair) -> None:
    stage, sender = chair
    _move, solver = _wired_pair(stage, sender)

    _bake(stage, solver, ends_at=(20.0, -5.0, 0.0))

    order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/chair")).GetXformOpOrderAttr().Get())
    assert chunk_utils.xform_op_name(solver) in order
    assert "xformOp:transform" not in order, (
        "the shared op is what let the solver evict everyone else"
    )


def test_the_transforms_op_survives_the_bake(chair) -> None:
    """The heart of it: the Transform must still be in the order after a sim."""
    stage, sender = chair
    move, solver = _wired_pair(stage, sender)

    _bake(stage, solver, ends_at=(20.0, -5.0, 0.0))

    order = list(UsdGeom.Xformable(stage.GetPrimAtPath("/chair")).GetXformOpOrderAttr().Get())
    assert chunk_utils.xform_op_name(move) in order


def test_the_placement_and_the_sim_compose(chair) -> None:
    stage, sender = chair
    _move, solver = _wired_pair(stage, sender)

    _bake(stage, solver, ends_at=(20.0, -5.0, 0.0))

    assert _local(stage) == pytest.approx(Gf.Vec3d(20.0, -5.0, 0.0)), (
        "placed at x=20 by the Transform, dropped 5 by the sim -- both, not one"
    )


def test_nudging_the_transform_after_a_bake_moves_the_chair(chair) -> None:
    """The reported symptom. Adjusting the Transform after a sim has to move the body by
    what was asked, not throw it somewhere else."""
    stage, sender = chair
    move, solver = _wired_pair(stage, sender)
    _bake(stage, solver, ends_at=(20.0, -5.0, 0.0))

    move.get_param("tx").value = 21.0

    assert _local(stage) == pytest.approx(Gf.Vec3d(21.0, -5.0, 0.0))


def test_the_solver_still_bakes_alone(chair) -> None:
    """No Transform in the chain at all -- the ordinary case must keep working."""
    stage, sender = chair
    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/chair"

    _bake(stage, solver, ends_at=(0.0, -3.0, 0.0))

    assert _local(stage) == pytest.approx(Gf.Vec3d(0.0, -3.0, 0.0))


# ---------------------------------------------------------------------------
# the bake is a share, not an absolute placement
# ---------------------------------------------------------------------------

def test_running_the_sim_does_not_jump_the_body(chair) -> None:
    """Reported live: with a Transform upstream, pressing Run Simulation threw the chair
    away.

    _local() gives where the body should end up in its parent's space, which was the whole
    answer while the bake replaced the op stack. Composing on top of the upstream ops, that
    absolute value applies the Transform's placement a second time -- 20 becomes 40.
    """
    stage, sender = chair
    _move, solver = _wired_pair(stage, sender)   # Transform places the chair at x=20

    # A sim that changes nothing: the body ends where it started. Whatever the bake writes,
    # the chair must not move.
    rbd_mod._write_prim_results(
        stage,
        stage.GetPrimAtPath("/chair"),
        solver.get_edit_context(),
        Gf.Matrix4d(1.0),
        [Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(20.0, 0.0, 0.0))],
        1,
        None,
        solver,
    )

    assert _local(stage) == pytest.approx(Gf.Vec3d(20.0, 0.0, 0.0)), (
        "a no-op simulation moved the body, so the placement was applied twice"
    )


def test_the_baked_share_excludes_the_upstream_placement(chair) -> None:
    """What the solver actually stores: the difference it is responsible for."""
    stage, sender = chair
    _move, solver = _wired_pair(stage, sender)

    _bake(stage, solver, ends_at=(20.0, -5.0, 0.0))   # sim result: (0, -5, 0) in parent space

    own = stage.GetPrimAtPath("/chair").GetAttribute(chunk_utils.xform_op_name(solver)).Get()
    assert Gf.Matrix4d(own).ExtractTranslation() == pytest.approx(Gf.Vec3d(0.0, -5.0, 0.0)), (
        "the share is the sim result minus the placement the Transform already contributes: "
        "the chair fell 5, and the 20 it was placed at is not the solver's to re-apply"
    )


def test_a_solver_with_nothing_upstream_bakes_the_result_directly(chair) -> None:
    """No upstream to divide out, so the share IS the result -- the ordinary case."""
    stage, sender = chair
    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/chair"

    _bake(stage, solver, ends_at=(0.0, -3.0, 0.0))

    own = stage.GetPrimAtPath("/chair").GetAttribute(chunk_utils.xform_op_name(solver)).Get()
    assert Gf.Matrix4d(own).ExtractTranslation() == pytest.approx(Gf.Vec3d(0.0, -3.0, 0.0))


# ---------------------------------------------------------------------------
# scale survives the world -> local conversion
# ---------------------------------------------------------------------------

def _scaled_parent(stage, scale=100.0):
    """A mesh under a scaled parent, which is what every real asset looks like.

    The apple's 100 lives on /food_apple_01_4k, not on its mesh -- so the mesh's LOCAL scale
    is 1 and its WORLD scale is 100. A prim at the root has the same value for both, which
    is why this went unnoticed: simulating the parent worked and simulating its mesh did not.
    """
    with Usd.EditContext(stage, stage.GetRootLayer().subLayerPaths and
                         Sdf.Layer.Find(stage.GetRootLayer().subLayerPaths[0])):
        parent = UsdGeom.Xform.Define(stage, "/asset")
        UsdGeom.Xformable(parent).AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))
        UsdGeom.Mesh.Define(stage, "/asset/mesh")
    return stage.GetPrimAtPath("/asset/mesh")


def _world_scale(stage, path):
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    return cache.GetLocalToWorldTransform(stage.GetPrimAtPath(path)).GetRow3(0).GetLength()


def _bake_as_run_does(stage, solver, prim, ends_at):
    """Mirror what _run does before calling the writer.

    The scale comes from rbd_solver._bake_scale rather than being recomputed here. An
    earlier version of this helper worked it out itself, which meant the tests passed
    against the buggy derivation too -- they exercised the writer and never the line that
    was actually wrong.
    """
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    parent_inv = cache.GetLocalToWorldTransform(prim.GetParent()).GetInverse()
    scale_xyz = rbd_mod._bake_scale(prim, cache)
    rbd_mod._write_prim_results(
        stage, prim, solver.get_edit_context(), parent_inv,
        [Gf.Matrix4d(1.0).SetTranslate(Gf.Vec3d(*ends_at))], 1, scale_xyz, solver,
    )


def test_a_mesh_under_a_scaled_parent_keeps_its_size(chair) -> None:
    """Reported live: running a sim on the apple MESH shrank it, while running it on the
    parent was fine.

    The bake converts a world-space result to local with parent_world_inv, which divides out
    every ancestor's scale -- and the sim result is rotation and translation only, so nothing
    puts it back. The compensation read the prim's LOCAL scale, which is 1 for a mesh under a
    scaled parent, so it compensated for nothing: the baked op came out at 0.01, exactly the
    1/100 the parent contributes.
    """
    stage, sender = chair
    prim = _scaled_parent(stage, scale=100.0)
    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/asset/mesh"
    assert _world_scale(stage, "/asset/mesh") == pytest.approx(100.0)

    _bake_as_run_does(stage, solver, prim, ends_at=(0.0, -1.0, 0.0))

    assert _world_scale(stage, "/asset/mesh") == pytest.approx(100.0), (
        "the apple shrank: the parent's scale was divided out and never restored"
    )


def test_the_parent_itself_still_works(chair) -> None:
    """The case that always worked, because a prim at the root has the same local and world
    scale -- kept so the fix cannot regress the thing that hid the bug."""
    stage, sender = chair
    _scaled_parent(stage, scale=100.0)
    prim = stage.GetPrimAtPath("/asset")
    solver = _mk(sender, rbd_mod, "Solver", "rbd_solver")
    solver.get_param("prim_path").value = "/asset"

    _bake_as_run_does(stage, solver, prim, ends_at=(0.0, -1.0, 0.0))

    assert _world_scale(stage, "/asset") == pytest.approx(100.0)


def test_the_bake_scale_is_the_world_scale(chair) -> None:
    """Directly on the derivation, since going through the writer cannot distinguish the two
    -- the writer is handed a number and has no way to know it came from the wrong place."""
    stage, _sender = chair
    prim = _scaled_parent(stage, scale=100.0)
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())

    local = cache.GetLocalTransformation(prim)
    local = local[0] if isinstance(local, tuple) else local
    assert local.GetRow3(0).GetLength() == pytest.approx(1.0), "the mesh's own scale is 1"

    assert rbd_mod._bake_scale(prim, cache)[0] == pytest.approx(100.0), (
        "the bake needs the scale the parent contributes, which the local transform lacks"
    )
