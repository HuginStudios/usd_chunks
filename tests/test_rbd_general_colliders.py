"""RBD Solver against arbitrary geometry: bodies whose points sit far from their prim
origin, and concave colliders a body has to get *inside*.

Both cases came from the same live report: a crayon translated over a cup on the kitchen
set's dining table. Pressing Run Simulation launched the crayon across the scene the moment
it touched the cup ("Nan, Inf or huge value in QACC"), and a scattered set of crayons sank
into the cup's wall and stuck there.

Two independent defects, one per section below. The geometry here is synthetic so the tests
don't need the kitchen set, but it reproduces the property that mattered: a prim whose points
are authored hundreds of units away from its transform origin (normal for a prop referenced
out of a production asset, which keeps its geometry in a shared model space).
"""

from __future__ import annotations

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

import scripts.rbd_solver as rbd_mod

if not rbd_mod._HAS_MUJOCO:
    pytest.skip("mujoco not installed", allow_module_level=True)

# After the guard: a machine without mujoco has no reason to have numpy either,
# and importing it above would error at collection rather than skip.
import numpy as np  # noqa: E402


# ---------------------------------------------------------------------------
# synthetic geometry
# ---------------------------------------------------------------------------


def _box_mesh(stage, path, centre, size):
    """A closed box mesh whose POINTS are at *centre* — the prim transform stays identity,
    so the geometry sits wherever centre says, exactly like model-space asset geometry."""
    cx, cy, cz = centre
    hx, hy, hz = (s * 0.5 for s in size)
    pts = [
        (cx - hx, cy - hy, cz - hz), (cx + hx, cy - hy, cz - hz),
        (cx + hx, cy + hy, cz - hz), (cx - hx, cy + hy, cz - hz),
        (cx - hx, cy - hy, cz + hz), (cx + hx, cy - hy, cz + hz),
        (cx + hx, cy + hy, cz + hz), (cx - hx, cy + hy, cz + hz),
    ]
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(pts)
    mesh.GetFaceVertexCountsAttr().Set([4] * 6)
    mesh.GetFaceVertexIndicesAttr().Set(
        [0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4, 1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7]
    )
    return mesh


def _cup_mesh(stage, path, centre, inner=8.0, height=12.0, wall=0.6):
    """An open box "cup": a floor and four thin upright walls, points at *centre*.

    Thin-walled and concave — the two properties a heightfield cannot describe (one height
    per cell) and a single convex mesh geom cannot either (it fills the interior in).
    """
    cx, cy, cz = centre
    h = inner * 0.5
    quads = []

    def _quad(a, b, c, d):
        quads.append((a, b, c, d))

    # floor
    _quad((cx - h, cy - h, cz), (cx + h, cy - h, cz), (cx + h, cy + h, cz), (cx - h, cy + h, cz))
    top = cz + height
    # four walls, each a thin slab drawn as a single quad (the collider extrudes it)
    _quad((cx - h, cy - h, cz), (cx + h, cy - h, cz), (cx + h, cy - h, top), (cx - h, cy - h, top))
    _quad((cx + h, cy + h, cz), (cx - h, cy + h, cz), (cx - h, cy + h, top), (cx + h, cy + h, top))
    _quad((cx + h, cy - h, cz), (cx + h, cy + h, cz), (cx + h, cy + h, top), (cx + h, cy - h, top))
    _quad((cx - h, cy + h, cz), (cx - h, cy - h, cz), (cx - h, cy - h, top), (cx - h, cy + h, top))

    pts = []
    counts = []
    indices = []
    for q in quads:
        base = len(pts)
        pts.extend(q)
        counts.append(4)
        indices.extend([base, base + 1, base + 2, base + 3])
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.GetPointsAttr().Set(pts)
    mesh.GetFaceVertexCountsAttr().Set(counts)
    mesh.GetFaceVertexIndicesAttr().Set(indices)
    return mesh


def _stage():
    """A stage whose authored content sits in a sublayer, so a bake layer can outrank it.

    The real chunk flow ends with _promote_bake_layer(), which moves the solver's layer above
    whatever authored the prim; a test driving simulate_mujoco() directly has to arrange the
    same strength order itself, or the prim's own xformOpOrder wins and the bake reads as if
    nothing moved.
    """
    root = Sdf.Layer.CreateAnonymous("root")
    base = Sdf.Layer.CreateAnonymous("base")
    root.subLayerPaths = [base.identifier]
    stage = Usd.Stage.Open(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)  # metadata: root layer only
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)  # centimetres, like a real set
    stage.SetEditTarget(Usd.EditTarget(base))
    return stage


def _world_bounds(stage, path, tc=Usd.TimeCode.Default()):
    bc = UsdGeom.BBoxCache(tc, [UsdGeom.Tokens.default_])
    r = bc.ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()
    return np.array([*r.GetMin()]), np.array([*r.GetMax()])


def _drop(
    stage,
    prim_path,
    collider_paths,
    frames=45,
    substeps=40,
    shape_type="Convex Hull",
    collider_mode="Auto",
    start_frame=1,
):
    """Run the solver's own entry point and return the per-frame geometry centre."""
    layer = Sdf.Layer.CreateAnonymous("bake")
    stage.GetRootLayer().subLayerPaths.insert(0, layer.identifier)  # strongest sublayer
    prim = stage.GetPrimAtPath(prim_path)
    parent = prim.GetParent()
    parent_world_inv = Gf.Matrix4d(1.0)
    if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
        parent_world_inv = UsdGeom.XformCache().GetLocalToWorldTransform(parent).GetInverse()

    rbd_mod.simulate_mujoco(
        stage,
        prim,
        layer,
        frames,
        24.0,
        -981.0,
        1.0,
        substeps,
        [0, 0, 0],
        [0, 0, 0],
        start_frame,
        parent_world_inv,
        # The solver's own translate/rotate offsets are gone -- a second Transform chunk
        # is the offset now. They were all zero here, so nothing this test asserts changes.
        collision_prims=[stage.GetPrimAtPath(p) for p in collider_paths],
        shape_type=shape_type,
        collider_mode=collider_mode,
    )

    centres = []
    for f in range(frames):
        mn, mx = _world_bounds(stage, prim_path, Usd.TimeCode(start_frame + f))
        centres.append((mn + mx) * 0.5)
    return np.array(centres)


# ---------------------------------------------------------------------------
# 1. a body whose points are far from its prim origin
# ---------------------------------------------------------------------------


def test_a_body_authored_far_from_its_origin_does_not_launch() -> None:
    """The reported explosion. The crayon's points sat 230 units from its prim origin, and
    the solver put the MuJoCo body frame on that origin — so the free joint carried its mass
    at the end of a 230-unit lever, and the first contact impulse became an enormous angular
    one. Speeds went from 80 to 1000 units/s within 4 ms of touching, then to 1e6 and NaN.

    The body frame now goes on the geometry, so contact behaves the same wherever the prim's
    origin happens to be.
    """
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(400.0, -200.0, 60.0))
    # a crayon: thin, long, and 450 units from its own prim origin
    _box_mesh(stage, "/crayon", centre=(400.0, -200.0, 90.0), size=(1.2, 1.2, 9.0))

    centres = _drop(stage, "/crayon", ["/cup"])

    speeds = np.linalg.norm(np.diff(centres, axis=0), axis=1) * 24.0
    assert np.all(np.isfinite(centres)), "the sim must not produce NaN/Inf"
    # Free fall from 30 units up reaches ~240 units/s; anything past a few hundred is the
    # blow-up, which used to reach 1e5-1e6.
    assert speeds.max() < 600.0, f"body was launched: peak speed {speeds.max():.0f} units/s"
    assert np.linalg.norm(centres[-1] - centres[-2]) < 0.5, "it should come to rest"


def test_it_settles_inside_the_cup_it_was_dropped_into() -> None:
    """Not just stable — correct. The crayon has to end up in the cup, not on top of it and
    not through it.
    """
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(400.0, -200.0, 60.0), inner=8.0, height=12.0)
    _box_mesh(stage, "/crayon", centre=(400.0, -200.0, 90.0), size=(1.2, 1.2, 9.0))

    cup_min, cup_max = _world_bounds(stage, "/cup")
    centres = _drop(stage, "/crayon", ["/cup"])
    end = centres[-1]

    assert cup_min[2] - 1.0 < end[2] < cup_max[2], (
        f"should rest inside the cup (z {cup_min[2]:.1f}..{cup_max[2]:.1f}), ended at z={end[2]:.2f}"
    )
    assert np.all(end[:2] > cup_min[:2] - 1.0) and np.all(end[:2] < cup_max[:2] + 1.0), (
        f"should stay within the cup laterally, ended at {end}"
    )


def test_the_prim_keeps_its_own_transform_structure() -> None:
    """The bake still writes the prim's transform, not the body frame's: frame 0 has to leave
    the geometry exactly where it started, whatever the offset between the two.
    """
    stage = _stage()
    _box_mesh(stage, "/ground", centre=(400.0, -200.0, 0.0), size=(200.0, 200.0, 2.0))
    _box_mesh(stage, "/prop", centre=(400.0, -200.0, 40.0), size=(4.0, 4.0, 4.0))

    before_min, before_max = _world_bounds(stage, "/prop")
    centres = _drop(stage, "/prop", ["/ground"], frames=30)

    assert np.allclose(centres[0], (before_min + before_max) * 0.5, atol=1e-6), (
        "frame 0 must reproduce the prim's authored pose"
    )
    # and it should land on the ground slab, not fall past it
    assert centres[-1][2] == pytest.approx(1.0 + 2.0, abs=0.6), f"should rest on the slab, got {centres[-1]}"


def test_geometry_centred_on_its_origin_still_behaves() -> None:
    """The case that already worked (an apple authored about its own origin) must be
    unaffected: the new composition collapses to the old identity when the two coincide.
    """
    stage = _stage()
    _box_mesh(stage, "/table", centre=(0.0, 0.0, 0.0), size=(100.0, 100.0, 4.0))
    apple = UsdGeom.Sphere.Define(stage, "/apple")
    apple.GetRadiusAttr().Set(3.0)
    UsdGeom.Xformable(apple).AddTranslateOp().Set(Gf.Vec3d(0, 0, 30))

    centres = _drop(stage, "/apple", ["/table"], frames=40, shape_type="Sphere")
    assert centres[-1][2] == pytest.approx(2.0 + 3.0, abs=0.6), f"apple should rest on the table, got {centres[-1]}"
    assert np.linalg.norm(centres[-1][:2]) < 1.0, "it should not drift sideways"


# ---------------------------------------------------------------------------
# 2. concave, thin-walled colliders
# ---------------------------------------------------------------------------


def _compiled_model(fn):
    """Run *fn* and return the MuJoCo model the solver compiled inside it.

    The solver builds and compiles its spec internally; there is no seam to hand the model
    back through, so the test borrows MjData's constructor -- the one thing every path calls
    with the finished model.
    """
    captured = {}
    real = rbd_mod._mj.MjData

    def spy(model, *a, **k):
        captured.setdefault("model", model)
        return real(model, *a, **k)

    rbd_mod._mj.MjData = spy
    try:
        fn()
    finally:
        rbd_mod._mj.MjData = real
    return captured.get("model")


def test_the_body_frame_carries_its_own_mass() -> None:
    """The defect behind the launch, stated directly: the dynamic body's centre of mass has
    to coincide with the frame its free joint acts at.

    With the frame on the prim's transform origin and the geometry 450 units away, MuJoCo put
    the body's COM 450 units from its joint (body_ipos), so the rotational inertia about that
    joint was m*d^2 -- ~200,000x the crayon's own -- and the mass matrix was ill-conditioned
    enough that one contact impulse threw the body across the scene.
    """
    stage = _stage()
    _box_mesh(stage, "/ground", centre=(400.0, -200.0, 0.0), size=(100.0, 100.0, 2.0))
    _box_mesh(stage, "/crayon", centre=(400.0, -200.0, 60.0), size=(1.2, 1.2, 9.0))

    layer = Sdf.Layer.CreateAnonymous("bake")
    stage.GetRootLayer().subLayerPaths.insert(0, layer.identifier)
    prim = stage.GetPrimAtPath("/crayon")

    model = _compiled_model(
        lambda: rbd_mod.simulate_mujoco(
            stage, prim, layer, 2, 24.0, -981.0, 1.0, 4, [0, 0, 0], [0, 0, 0], 1,
            Gf.Matrix4d(1.0),
            collision_prims=[stage.GetPrimAtPath("/ground")],
        )
    )
    assert model is not None
    bid = rbd_mod._mj.mj_name2id(model, rbd_mod._mj.mjtObj.mjOBJ_BODY, "dynamic")

    com_lever = float(np.linalg.norm(model.body_ipos[bid]))
    assert com_lever < 0.5, f"the body's mass must sit at its joint, not {com_lever:.1f} units away"

    mn, mx = _world_bounds(stage, "/crayon")
    centre = (mn + mx) * 0.5
    assert np.allclose(model.body_pos[bid], centre, atol=1e-3), (
        f"body frame should be on the geometry {centre}, got {model.body_pos[bid]}"
    )


def test_scattered_instances_do_not_explode_or_sink_into_the_walls() -> None:
    """The instancer path has the same defect and the same fix.

    A PointInstancer positions the prototype's *origin*, so a prototype whose points are
    authored away from that origin (a scatter prototype referencing a prop out of an asset)
    gave every instance the same long lever arm between its free joint and its mass. Live,
    that showed up as scattered crayons sinking into a cup's wall and sticking there.
    """
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(400.0, -200.0, 60.0), inner=10.0, height=12.0)
    # prototype authored 450 units from its own origin, like a referenced prop
    _box_mesh(stage, "/proto/crayon", centre=(400.0, -200.0, 0.0), size=(1.2, 1.2, 8.0))

    inst = UsdGeom.PointInstancer.Define(stage, "/scatter")
    inst.GetPrototypesRel().SetTargets([Sdf.Path("/proto")])
    # A 3x2 lateral grid inside the cup, all at the same height: laterally clear of each
    # other, so the test starts from a legal state (overlapping instances at frame 0 would
    # blow any solver up, and that is not what is under test here).
    grid = [(px, py) for px in (-3.0, 0.0, 3.0) for py in (-2.5, 2.5)]
    n = len(grid)
    inst.GetPositionsAttr().Set([Gf.Vec3f(px, py, 80.0) for px, py in grid])
    inst.GetProtoIndicesAttr().Set([0] * n)
    inst.GetOrientationsAttr().Set([Gf.Quath(1, 0, 0, 0)] * n)

    layer = Sdf.Layer.CreateAnonymous("bake")
    stage.GetRootLayer().subLayerPaths.insert(0, layer.identifier)

    frames = 60
    model = _compiled_model(
        lambda: rbd_mod.simulate_mujoco_instancer(
            stage, stage.GetPrimAtPath("/scatter"), layer,
            [stage.GetPrimAtPath("/cup")],
            frames, 24.0, -981.0, 1.0, 40, [0, 0, 0], [0, 0, 0], 1,
            shape_type="Convex Hull",
        )
    )
    assert model is not None
    for i in range(n):
        bid = rbd_mod._mj.mj_name2id(model, rbd_mod._mj.mjtObj.mjOBJ_BODY, f"b{i}")
        lever = float(np.linalg.norm(model.body_ipos[bid]))
        assert lever < 0.5, f"instance {i} carries its mass {lever:.1f} units from its joint"

    cup_min, cup_max = _world_bounds(stage, "/cup")

    def _geom_centres(frame):
        """Where each instance's geometry actually is, at *frame*.

        A PointInstancer position is the PROTOTYPE ORIGIN, and this prototype's points sit
        450 units from that origin, so the raw positions swing hundreds of units the moment
        an instance tumbles -- correct, but useless to assert on. Compose the real instance
        transforms and carry the prototype's own centre through them instead.
        """
        tc = Usd.TimeCode(frame)
        mats = inst.ComputeInstanceTransformsAtTime(tc, tc)
        proto_centre = Gf.Vec3d(400.0, -200.0, 0.0)  # _box_mesh centre, in prototype space
        return np.array([[*m.Transform(proto_centre)] for m in mats], dtype=float)

    geom = _geom_centres(frames)
    assert np.all(np.isfinite(geom)), "no instance may go non-finite"
    # Every instance ends up in the cup: on its floor, not through it and not launched out.
    assert np.all(geom[:, 2] > cup_min[2] - 1.0), f"instances fell through the cup floor: {geom}"
    assert np.all(geom[:, 2] < cup_max[2] + 6.0), f"instances were launched upward: {geom}"
    for axis in (0, 1):
        assert np.all(geom[:, axis] > cup_min[axis] - 2.0) and np.all(geom[:, axis] < cup_max[axis] + 2.0), (
            f"instances left the cup laterally on axis {axis}: {geom}"
        )
    # and the pile has come to rest
    move = np.abs(_geom_centres(frames - 2) - geom).max()
    assert move < 1.0, f"the pile should have settled, still moving {move:.2f}/frame"


def test_mesh_collider_is_the_default_for_a_prop() -> None:
    """Auto picks the exact triangle-soup collider while the collider fits the budget."""
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(0.0, 0.0, 0.0))
    spec = rbd_mod._mj.MjSpec()
    rbd_mod._add_colliders(
        spec, [stage.GetPrimAtPath("/cup")], up_idx=2, friction=0.5, contact_tc=0.01,
        dyn_radius=1.0, hfield_res=64,
    )
    model = spec.compile()
    kinds = {rbd_mod._mj.mjtGeom(model.geom_type[g]).name for g in range(model.ngeom)}
    assert "mjGEOM_MESH" in kinds, f"expected a mesh soup collider, got {kinds}"
    assert "mjGEOM_HFIELD" not in kinds
    # one prism per triangle, plus the catch-all ground plane
    assert model.ngeom == 11, model.ngeom


def test_a_huge_collider_falls_back_to_a_heightfield() -> None:
    """Terrain-scale geometry keeps the cheap 2.5D collider: one geom per triangle is the
    right trade for a prop, not for 200k triangles."""
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(0.0, 0.0, 0.0))
    spec = rbd_mod._mj.MjSpec()
    rbd_mod._add_colliders(
        spec, [stage.GetPrimAtPath("/cup")], up_idx=2, friction=0.5, contact_tc=0.01,
        dyn_radius=1.0, hfield_res=64, tri_budget=2,  # the cup's 10 triangles exceed it
    )
    model = spec.compile()
    kinds = {rbd_mod._mj.mjtGeom(model.geom_type[g]).name for g in range(model.ngeom)}
    assert "mjGEOM_HFIELD" in kinds, f"expected the heightfield fallback, got {kinds}"


def test_box_collider_is_centred_on_the_geometry_not_the_prim_origin() -> None:
    """The same offset bug on the collider side: the cup's box collider used to be built at
    the prim's transform origin, so it collided with empty space 250 units from the mesh
    while bodies fell straight through what you could see.
    """
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(400.0, -200.0, 60.0))
    mn, mx = _world_bounds(stage, "/cup")
    expected = (mn + mx) * 0.5

    spec = rbd_mod._mj.MjSpec()
    rbd_mod._add_colliders(
        spec, [stage.GetPrimAtPath("/cup")], up_idx=2, friction=0.5, contact_tc=0.01,
        dyn_radius=1.0, hfield_res=64, collider_mode="Box",
    )
    model = spec.compile()
    bid = rbd_mod._mj.mj_name2id(model, rbd_mod._mj.mjtObj.mjOBJ_BODY, "col_0")
    assert bid > 0
    assert np.allclose(model.body_pos[bid], expected, atol=1e-3), (
        f"collider body should sit on the geometry {expected}, got {model.body_pos[bid]}"
    )


def test_a_box_collider_cannot_be_entered_but_a_mesh_collider_can() -> None:
    """Why the Mesh collider exists, stated as behaviour: with the cup approximated by its
    bounding box the body rests on the lid; with the real surface it drops inside.
    """
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(400.0, -200.0, 60.0), inner=8.0, height=12.0)
    _box_mesh(stage, "/crayon", centre=(400.0, -200.0, 90.0), size=(1.2, 1.2, 9.0))
    cup_min, cup_max = _world_bounds(stage, "/cup")

    as_box = _drop(stage, "/crayon", ["/cup"], collider_mode="Box")
    assert as_box[-1][2] > cup_max[2] - 1.0, "a box collider is solid: the body stays on top"

    stage2 = _stage()
    _cup_mesh(stage2, "/cup", centre=(400.0, -200.0, 60.0), inner=8.0, height=12.0)
    _box_mesh(stage2, "/crayon", centre=(400.0, -200.0, 90.0), size=(1.2, 1.2, 9.0))
    as_mesh = _drop(stage2, "/crayon", ["/cup"], collider_mode="Mesh")
    assert as_mesh[-1][2] < cup_max[2], "the real surface lets the body into the cup"


def test_thin_walls_are_not_tunnelled_through() -> None:
    """The second live symptom: instances sank into the cup's wall and stuck half way. A
    heightfield turns a wall into a cliff with nothing behind it; the prism collider gives
    every triangle real thickness, so a body sliding into a wall is stopped by it.
    """
    stage = _stage()
    _cup_mesh(stage, "/cup", centre=(400.0, -200.0, 60.0), inner=8.0, height=12.0)
    # start it off-centre so it slides into a wall on the way down
    _box_mesh(stage, "/crayon", centre=(403.5, -197.0, 85.0), size=(1.2, 1.2, 8.0))
    cup_min, cup_max = _world_bounds(stage, "/cup")

    centres = _drop(stage, "/crayon", ["/cup"], frames=60)
    end_min, end_max = _world_bounds(stage, "/crayon", Usd.TimeCode(60))

    assert np.all(end_max <= cup_max + 1.0) and np.all(end_min >= cup_min - 1.0), (
        f"the body must stay inside the cup, not end up embedded in a wall: {end_min}..{end_max}"
    )
    assert np.linalg.norm(centres[-1] - centres[-2]) < 0.5, "and it must come to rest"
