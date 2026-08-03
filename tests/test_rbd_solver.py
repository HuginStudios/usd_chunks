from __future__ import annotations

import importlib

from pxr import Sdf, Usd, UsdGeom

from plugins.chunk_usd import (
    _EXEC_INPUT,
    _EXEC_OUTPUT,
    get_chunk_prim_path,
    initialize as _chunk_usd_init,
)
from usd_chunks import chunk_registry
from usd_chunks.chunk_core import SceneSender


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _mk(mod, scene):
    cs = mod.create() if mod.loaded else mod.load()
    for c in cs:
        scene.add_chunk(c)
    return cs[0]


def _wire(dst, src, stage):
    p = stage.GetPrimAtPath(Sdf.Path(get_chunk_prim_path(dst)))
    a = p.GetAttribute(_EXEC_INPUT) or p.CreateAttribute(_EXEC_INPUT, Sdf.ValueTypeNames.Token, custom=False)
    a.SetConnections([Sdf.Path(f"{get_chunk_prim_path(src)}.{_EXEC_OUTPUT}")])
    dst.add_dependency(src)


def test_rbd_run_does_not_reexecute_upstream_chunk() -> None:
    """Regression reported live in usdview: with Scatter -> RBD Solver wired, pressing "Run
    Simulation" crashed with USD's "Layer ... is not in the local LayerStack" -- the same
    nested-solve-cascade crash class Scatter's old exec_with_hidden_view-based fix could hit,
    because recook_for_view's own re-execution of RBD's upstream (Scatter) could reach Scatter
    reactively while already inside another chunk's dispatch. It also re-ran Scatter's
    placement (visible as a stray "[ScatterPoints] Scattered ..." print) as a side effect of
    establishing RBD's "hidden view" -- surprising, since the user only pressed RBD's button.

    RBD's _run now resolves its target prim through get_upstream_stage() instead (see
    scatter_points.py's execute() for the same fix) -- a pure read-only composition of
    already-existing layers, so pressing "Run Simulation" must not execute any other chunk at
    all (no crash risk, and no more surprise re-scatters).
    """
    import scripts.rbd_solver as rbd_mod_file

    if not rbd_mod_file._HAS_MUJOCO:
        import pytest

        pytest.skip("mujoco not installed in this environment")

    stage = Usd.Stage.CreateInMemory()

    class _StageScene(_DummyScene):
        def get_stage(self):
            return stage

    scene = _StageScene()
    reg = chunk_registry.ChunkRegistry()
    reg.initialize(scene, "pytest")
    lm = chunk_registry.ChunkLayerManager()
    lm.initialize(stage)
    _chunk_usd_init(stage, scene)

    scatter_mod = reg.get_chunk_module("scatter_points")
    rbd_mod = reg.get_chunk_module("rbd_solver")

    mesh = UsdGeom.Mesh.Define(stage, "/apple")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.GetFaceVertexCountsAttr().Set([3])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2])

    scatter = _mk(scatter_mod, scene)
    rbd = _mk(rbd_mod, scene)
    _wire(rbd, scatter, stage)

    scatter.get_param("surface_prim").value = "/apple"
    scatter.get_param("max_count").value = 2

    rbd.get_param("prim_path").value = "/ScatterInstancer"
    rbd.get_param("frame_count").value = 2  # keep the test fast

    real_scatter_module = reg.get_chunk_module("scatter_points").module
    real_rbd_module = reg.get_chunk_module("rbd_solver").module

    call_count = {"n": 0}
    orig_execute = real_scatter_module.execute

    def traced_execute(node):
        call_count["n"] += 1
        return orig_execute(node)

    real_scatter_module.execute = traced_execute
    call_count["n"] = 0  # reset after the reactive scatter runs triggered by the setup above

    # This must not raise (the reported crash) and must not touch scatter at all.
    run_btn = rbd.get_param("run_sim")
    real_rbd_module._run(run_btn, None, run_btn)

    assert call_count["n"] == 0, (
        "pressing Run Simulation must not re-execute an upstream chunk like Scatter as a "
        "side effect -- get_upstream_stage is a pure read, not a re-execution"
    )

    # Regression: the baked animation must land on the REAL stage/layer, not vanish onto a
    # throwaway scratch stage discarded when _run() returns.
    positions_attr = stage.GetPrimAtPath("/ScatterInstancer").GetAttribute("positions")
    assert positions_attr.GetNumTimeSamples() > 0, (
        "the simulation must bake real time samples onto the actual stage, not a discarded "
        "scratch stage"
    )


# ---------------------------------------------------------------------------
# _is_world_animated
# ---------------------------------------------------------------------------

def test_is_world_animated_false_for_fully_static_prim() -> None:
    import scripts.rbd_solver as rbd

    stage = Usd.Stage.CreateInMemory()
    prim = UsdGeom.Xform.Define(stage, "/parent/child").GetPrim()

    assert rbd._is_world_animated(prim) is False


def test_is_world_animated_true_for_animation_on_the_prim_itself() -> None:
    import scripts.rbd_solver as rbd

    stage = Usd.Stage.CreateInMemory()
    xform = UsdGeom.Xform.Define(stage, "/bowl")
    op = xform.AddTranslateOp()
    op.Set((0, 0, 0), Usd.TimeCode(1))
    op.Set((1, 0, 0), Usd.TimeCode(24))

    assert rbd._is_world_animated(xform.GetPrim()) is True


def test_is_world_animated_true_when_animation_lives_on_an_ancestor() -> None:
    """The actual live bug: a Transform chunk authors xformOp:transform on a wrapping
    Xform prim (e.g. /World/Bowl), while the RBD solver's collision geometry is the
    renderable mesh underneath it (e.g. /World/Bowl/Geom) -- which has no xformOps of
    its own at all. Checking only the collision prim's own xformOps (the old behavior)
    always saw this as static; walking the ancestor chain finds the parent's animated
    xformOp instead."""
    import scripts.rbd_solver as rbd

    stage = Usd.Stage.CreateInMemory()
    parent = UsdGeom.Xform.Define(stage, "/World/Bowl")
    rot_op = parent.AddRotateYOp()
    rot_op.Set(0.0, Usd.TimeCode(1))
    rot_op.Set(180.0, Usd.TimeCode(24))
    mesh = UsdGeom.Mesh.Define(stage, "/World/Bowl/Geom")

    # Confirm the bug precondition: the mesh prim itself has zero xformOps/time samples.
    assert list(UsdGeom.Xformable(mesh.GetPrim()).GetOrderedXformOps()) == []

    assert rbd._is_world_animated(mesh.GetPrim()) is True


def test_is_world_animated_true_even_when_keyframes_sit_outside_the_sim_window() -> None:
    """The actual second bug, found live: an earlier version of this check sampled
    GetLocalToWorldTransform() across the solver's own start_frame/frame_count window and
    compared -- wrong on its own terms, since a Transform's keyframes can sit entirely
    outside that window (e.g. the bowl rotates across frames 0-18, but the solver's own
    start_frame is 30). USD holds the last keyframe's value for the rest of the timeline,
    so the world transform would be genuinely static *within* the solver's own window
    despite being real animated data -- the old window-sampling check reported False
    here. Fixed by not depending on any specific window at all: does this prim's world
    transform have real time samples ANYWHERE, not "does it move during frames X-Y"."""
    import scripts.rbd_solver as rbd

    stage = Usd.Stage.CreateInMemory()
    parent = UsdGeom.Xform.Define(stage, "/World/Bowl")
    rot_op = parent.AddRotateYOp()
    rot_op.Set(0.0, Usd.TimeCode(0))
    rot_op.Set(180.0, Usd.TimeCode(18))
    mesh = UsdGeom.Mesh.Define(stage, "/World/Bowl/Geom")

    assert rbd._is_world_animated(mesh.GetPrim()) is True


# ---------------------------------------------------------------------------
# _add_colliders: mocap body initial pose
# ---------------------------------------------------------------------------

def test_add_colliders_mocap_body_initial_pose_uses_start_frame_not_default() -> None:
    """Regression test for the real live bug: an animated collider's xformOp only ever
    carries real time samples (no separate default value -- matching exactly what
    scripts/transform.py's bake() authors), and Usd.Attribute.Get(Default()) on such an
    attribute resolves to nothing at all (confirmed against the installed USD build).
    Computing the mocap body's INITIAL spec pos/quat at Default() therefore silently
    fell back to identity/wrong -- and the very first mj_forward() call (before the
    per-frame mocap update loop even starts) uses exactly that spec pose, producing a
    badly wrong initial contact (apple flying away, or landing far from the actual
    collider). col_body.quat specifically was never set at all before this fix, always
    defaulting to MuJoCo's identity regardless of the collider's real rotation."""
    import pytest
    import scripts.rbd_solver as rbd

    if not rbd._HAS_MUJOCO:
        pytest.skip("mujoco not installed in this environment")

    import mujoco as _mj

    stage = Usd.Stage.CreateInMemory()
    bowl = UsdGeom.Xform.Define(stage, "/bowl")
    translate_op = bowl.AddTranslateOp()
    rotate_op = bowl.AddRotateYOp()
    # Only time samples, no default value -- exactly what bake() produces.
    translate_op.Set((0.0, 0.0, 0.0), Usd.TimeCode(1))
    translate_op.Set((0.0, 50.0, 0.0), Usd.TimeCode(30))
    rotate_op.Set(0.0, Usd.TimeCode(1))
    rotate_op.Set(90.0, Usd.TimeCode(30))
    mesh = UsdGeom.Mesh.Define(stage, "/bowl/geom")
    mesh.GetPointsAttr().Set([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)])

    spec = _mj.MjSpec()
    rbd._add_colliders(
        spec, [mesh.GetPrim()], up_idx=1, friction=0.5, contact_tc=0.02,
        dyn_radius=1.0, hfield_res=32, start_frame=30.0,
    )

    body = spec.worldbody.first_body()
    collider_body = None
    while body is not None:
        if body.name.startswith("col_"):
            collider_body = body
            break
        body = spec.worldbody.next_body(body)

    assert collider_body is not None
    assert collider_body.mocap == 1

    # Position must reflect frame 30's real (baked) translate, not frame 0/Default().
    assert collider_body.pos[1] == pytest.approx(50.0, abs=0.5)
    # 90 degrees about Y -> quat (cos45, 0, sin45, 0). Never set at all before this fix
    # (silently stayed MuJoCo's default identity [1, 0, 0, 0]).
    assert collider_body.quat[0] == pytest.approx(0.70710678, abs=0.01)
    assert collider_body.quat[2] == pytest.approx(0.70710678, abs=0.01)


def test_add_colliders_box_size_uses_local_bound_not_world_aligned_bound() -> None:
    """Regression test for the real live bug: a wide, flat, rotated collider (e.g. a
    plate/bowl tipped ~90 degrees, matching the printed live quat (0.711, -0.703, 0, 0))
    got its box collider SIZE from a world-axis-aligned bound -- which balloons for a
    rotated flat shape, since "aligned to world axes" doesn't rotate with the object.
    Confirmed directly: a wide (60x60), thin (1 unit) disc rotated -90 degrees about X
    has a world-aligned size of (60, 60, 1) but a local (pre-rotation) size of (60, 1, 60).
    The box ended up ~30 units taller than the real geometry, matching exactly the
    reported symptom ("collides way above the actual bowl", "offset_y=30 sort of works").
    col_body.quat already correctly orients a local-frame-sized box in world space, so
    using the local bound for size (not the world-aligned one) fixes this."""
    import pytest
    import scripts.rbd_solver as rbd

    if not rbd._HAS_MUJOCO:
        pytest.skip("mujoco not installed in this environment")

    import mujoco as _mj

    stage = Usd.Stage.CreateInMemory()
    plate = UsdGeom.Xform.Define(stage, "/plate")
    rot_op = plate.AddRotateXOp()
    rot_op.Set(-90.0, Usd.TimeCode(1))
    rot_op.Set(-90.0, Usd.TimeCode(24))  # >1 sample -- must still read as animated
    mesh = UsdGeom.Mesh.Define(stage, "/plate/geom")
    # Wide (60x60), thin (1 unit) disc, matching the live-reported plate's proportions.
    mesh.GetPointsAttr().Set([
        (-30, -0.5, -30), (30, -0.5, -30), (30, 0.5, 30),
        (-30, 0.5, 30), (-30, 0.5, -30), (30, -0.5, 30),
    ])

    spec = _mj.MjSpec()
    rbd._add_colliders(
        spec, [mesh.GetPrim()], up_idx=1, friction=0.5, contact_tc=0.02,
        dyn_radius=1.0, hfield_res=32, start_frame=1.0,
    )

    body = spec.worldbody.first_body()
    collider_body = None
    while body is not None:
        if body.name.startswith("col_"):
            collider_body = body
            break
        body = spec.worldbody.next_body(body)

    assert collider_body is not None
    assert len(collider_body.geoms) == 1
    size = collider_body.geoms[0].size
    # Local frame: half-extents (30, 0.5, 30) -- thin along local Y, matching the source
    # mesh's true shape. The bug produced a world-aligned (30, 30, 0.5)-ish box instead.
    assert size[1] == pytest.approx(0.5, abs=0.1), (
        f"box collider is {size[1]:.2f} units tall on the short axis -- looks like it's "
        "still using the world-aligned bound (which balloons for a rotated flat shape) "
        "instead of the local one"
    )
    assert size[0] == pytest.approx(30.0, abs=1.0)
    assert size[2] == pytest.approx(30.0, abs=1.0)
