"""MuJoCo RBD Solver — rigid body simulation via MuJoCo.

Requires: pxr (OpenUSD), usd_chunks, mujoco, numpy.
Optional: scipy — smoother/denser heightfield colliders; falls back gracefully if absent.
"""

from __future__ import annotations

import math
from collections.abc import Generator

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_registry
from plugins import chunk_usd

try:
    import mujoco as _mj
    import numpy as _np

    _HAS_MUJOCO = True
except ImportError:
    _HAS_MUJOCO = False
    _np = None


def _build_heightfield(cprim: Usd.Prim, up_idx: int, resolution: int = 64, min_cell: float = 0.0) -> dict | None:
    """Sample the top surface of *cprim*'s meshes into a MuJoCo heightfield.

    A bowl/plate/terrain inner surface is single-valued in height, so a
    heightfield gives stable, smooth *concave* collision — unlike a mesh geom,
    which MuJoCo only ever collides as its convex hull (this build has no
    generic mesh-SDF collider).

    *min_cell* caps the grid resolution so a cell never gets much smaller than
    the dynamic body: a sphere resting in a sub-cell-sized concave pit at the
    bowl bottom produces a singular/over-constrained contact that explodes
    (empirically once cell size drops below ~0.1x the body radius). Pass the
    body radius * ~0.15 so the bottom stays gentle.

    Returns a dict for spec.add_hfield() / a static body, or None if the prim
    can't be represented as a heightfield (degenerate / flat / no mesh).

    Limitation: only the upper surface is captured (no overhangs) -- fine for a bowl/plate/
    terrain (single-valued height), wrong for a cup with a lip curling back over itself.
    Falls back to a box collider when None.
    """
    if _np is None:
        return None

    xc = UsdGeom.XformCache()
    pts_world = []
    for p in Usd.PrimRange(cprim):
        mesh = UsdGeom.Mesh(p)
        if not mesh:
            continue
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        wxf = xc.GetLocalToWorldTransform(p)
        for pt in points:
            w = wxf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
            pts_world.append((w[0], w[1], w[2]))

    if len(pts_world) < 16:
        return None

    V = _np.asarray(pts_world, dtype=_np.float64)
    lat = [j for j in range(3) if j != up_idx]
    a0, a1 = lat[0], lat[1]
    min0, max0 = float(V[:, a0].min()), float(V[:, a0].max())
    min1, max1 = float(V[:, a1].min()), float(V[:, a1].max())
    if (max0 - min0) < 1e-6 or (max1 - min1) < 1e-6:
        return None

    # Resolution: clamp to a hard cap, then to a cell-size floor for stability.
    N = max(8, min(int(resolution), 72))
    if min_cell > 1e-6:
        span = min(max0 - min0, max1 - min1)
        N = max(8, min(N, int(span / min_cell) + 1))
    col = _np.clip(((V[:, a0] - min0) / (max0 - min0) * (N - 1)).round().astype(int), 0, N - 1)
    # Flipped (max1 - ...), not (... - min1): MuJoCo's hfield row 0 sits at local -Y, row N-1
    # at local +Y, but the -90-about-X quat below (needed to point local +Z, the height axis,
    # along world up) sends local +Y to world -Z, not +Z -- a real rotation can't avoid that
    # (X->X, Y->Z, Z->Y has determinant -1, a mirror, not a rotation). Flipping which end of
    # the sampled range row 0 starts from cancels that inversion so the baked collider actually
    # matches the source mesh's orientation instead of its Z-mirror.
    row = _np.clip(((max1 - V[:, a1]) / (max1 - min1) * (N - 1)).round().astype(int), 0, N - 1)

    H = _np.full((N, N), -_np.inf)
    up = V[:, up_idx]
    # Top surface = highest sampled point per cell.
    _np.maximum.at(H, (row, col), up)

    filled = _np.isfinite(H)
    if not filled.any():
        return None
    if not filled.all():
        # Fill empty cells from the nearest sampled cell so the grid is dense.
        try:
            from scipy.ndimage import distance_transform_edt

            idx = distance_transform_edt(~filled, return_distances=False, return_indices=True)
            H = H[tuple(idx)]
        except Exception:
            H[~filled] = float(H[filled].min())

    # Light smoothing removes binning staircase so the body rolls cleanly.
    try:
        from scipy.ndimage import gaussian_filter

        H = gaussian_filter(H, sigma=0.6, mode="nearest")
    except Exception:
        pass

    h0 = float(H.min())
    elev = float(H.max() - h0)
    if elev < 1e-6:
        return None
    heights = ((H - h0) / elev).astype(_np.float32).flatten()

    pos = [0.0, 0.0, 0.0]
    pos[a0] = (min0 + max0) / 2.0
    pos[a1] = (min1 + max1) / 2.0
    pos[up_idx] = h0

    # Orient the hfield so its local +Z (height axis) points along world up.
    if up_idx == 1:  # Y-up: -90° about X
        a = math.pi / 4
        quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
    else:  # Z-up: identity (X-up falls back to box before reaching here)
        quat = [1.0, 0.0, 0.0, 0.0]

    size = [(max0 - min0) / 2.0, (max1 - min1) / 2.0, elev, 1.0]
    return {"nrow": N, "ncol": N, "size": size, "heights": heights, "pos": pos, "quat": quat}


def _add_colliders(
    spec: _mj.MjSpec,
    collision_prims: list[Usd.Prim] | None,
    up_idx: int,
    friction: float,
    contact_tc: float,
    dyn_radius: float,
    hfield_res: int,
    floor_drop: float = 0.0,
) -> list[tuple[str, Usd.Prim]]:
    """Add static/kinematic collision bodies to *spec*.

    No collision prims → infinite ground plane. Otherwise each prim becomes a
    concave heightfield collider (+ flat backstop) when possible, else a
    bbox-fitted box (animated prims fall back to a mocap-driven box).

    *dyn_radius* sizes the heightfield cell-size cap to the dynamic body.
    Returns mocap_col_names: list of (body_name, prim) needing per-frame updates.
    """
    mocap_col_names = []

    if not collision_prims:
        # Infinite ground plane, oriented to the scene's up axis.
        plane_body = spec.worldbody.add_body()
        plane_body.name = "ground"
        if floor_drop:
            ppos = [0.0, 0.0, 0.0]
            ppos[up_idx] = -floor_drop
            plane_body.pos = ppos
        if up_idx == 1:  # Y-up: rotate plane so its normal points +Y
            a = math.pi / 4  # half-angle of -90° rotation around X
            plane_body.quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
        pg = plane_body.add_geom()
        pg.type = _mj.mjtGeom.mjGEOM_PLANE
        pg.size = [0.0, 0.0, 0.01]
        pg.friction = [friction, 0.005, 0.0001]
        pg.condim = 3
        return mocap_col_names

    # Catch-all ground plane at the lowest point of the colliders — the "table"
    # the bowl sits on. An infinite plane is a rock-solid (non-singular) contact,
    # so it reliably catches anything the concave heightfield bottom ejects AND
    # any body that rolls/spills off the rim, instead of letting it fall forever.
    bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    ground_up = None
    for cp in collision_prims:
        cr = bc.ComputeWorldBound(cp).ComputeAlignedRange()
        if not cr.IsEmpty():
            b = float(cr.GetMin()[up_idx])
            ground_up = b if ground_up is None else min(ground_up, b)
    if ground_up is not None:
        gp_body = spec.worldbody.add_body()
        gp_body.name = "catch_ground"
        gpos = [0.0, 0.0, 0.0]
        gpos[up_idx] = ground_up - floor_drop
        gp_body.pos = gpos
        if up_idx == 1:  # Y-up: orient plane normal to +Y
            a = math.pi / 4
            gp_body.quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
        gpg = gp_body.add_geom()
        gpg.type = _mj.mjtGeom.mjGEOM_PLANE
        gpg.size = [0.0, 0.0, 0.01]
        gpg.friction = [friction, 0.005, 0.0001]
        gpg.solref = [contact_tc, 1.0]
        gpg.condim = 3

    for i, cp in enumerate(collision_prims):
        xfc = UsdGeom.XformCache()
        world_xf = xfc.GetLocalToWorldTransform(cp)
        col_t = world_xf.ExtractTranslation()

        is_anim = any(op.GetNumTimeSamples() > 1 for op in UsdGeom.Xformable(cp).GetOrderedXformOps())

        # Concave collision via heightfield (static, non-X-up prims only).
        # MuJoCo collides mesh geoms as their convex hull, so a bowl would be
        # a solid dome with no interior — the apple just rolls off. A
        # heightfield captures the actual concave dip and is numerically
        # stable (a flat high-vertex mesh hull explodes).
        hf = None
        if not is_anim and up_idx != 0:
            # Keep hfield cells from getting much smaller than the body, or
            # the contact at the bowl bottom goes singular and explodes.
            hf = _build_heightfield(cp, up_idx, hfield_res, min_cell=0.15 * dyn_radius)

        if hf is not None:
            hname = f"hf_{i}"
            mh = spec.add_hfield()
            mh.name = hname
            mh.nrow = hf["nrow"]
            mh.ncol = hf["ncol"]
            mh.size = hf["size"]
            mh.userdata = hf["heights"]

            col_body = spec.worldbody.add_body()
            col_body.name = f"col_{i}"
            col_body.pos = hf["pos"]
            col_body.quat = hf["quat"]
            bg = col_body.add_geom()
            bg.type = _mj.mjtGeom.mjGEOM_HFIELD
            bg.hfieldname = hname
            bg.friction = [friction, 0.005, 0.0001]
            bg.solref = [contact_tc, 1.0]
            bg.solimp = [0.95, 0.99, 0.001, 0.5, 2]
            # condim=3 (normal + sliding friction). condim=6 adds torsional/
            # rolling constraints that over-constrain the singular contact at
            # a bowl's concave bottom vertex and explode. The catch-all ground
            # plane above backstops anything the concave bottom ejects.
            bg.condim = 3
            continue

        # Fallback: bbox-fitted box (animated prims, or non-heightfieldable).
        bname = f"col_{i}"
        col_body = spec.worldbody.add_body()
        col_body.name = bname
        col_body.pos = [float(col_t[0]), float(col_t[1]), float(col_t[2])]
        if is_anim:
            # Mocap body: MuJoCo updates its pose each frame via data.mocap_pos/quat.
            col_body.mocap = True
            mocap_col_names.append((bname, cp))

        fb = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        cr = fb.ComputeWorldBound(cp).ComputeAlignedRange()
        if not cr.IsEmpty():
            csz = cr.GetMax() - cr.GetMin()
            half = [csz[0] / 2, csz[1] / 2, csz[2] / 2]
            # A flat collider (grid/plane) has a zero extent on one axis, which MuJoCo
            # rejects ("size N must be positive in geom"). Give any degenerate axis a
            # small thickness so it becomes a thin slab the bodies can rest on.
            min_half = max(1e-3, 1e-3 * max(half))
            half = [max(h, min_half) for h in half]
            bg = col_body.add_geom()
            bg.type = _mj.mjtGeom.mjGEOM_BOX
            bg.size = half
            bg.friction = [friction, 0.005, 0.0001]
            bg.solref = [contact_tc, 1.0]
            bg.condim = 3

    return mocap_col_names


def _sim_prim_gen_mj(
    prim: Usd.Prim,
    collision_prims: list[Usd.Prim] | None,
    frame_count: int,
    gravity: float,
    friction: float,
    restitution: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    dt: float,
    otx: float,
    oty: float,
    otz: float,
    orx: float,
    ory: float,
    orz: float,
    up_idx: int,
    shape_type: str,
    collision_margin: float,
    solver_iters: int,
    translate: Gf.Vec3d,
    rot: Gf.Rotation,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    hull_max_verts: int = 0,
    hfield_res: int = 96,
    floor_drop: float = 0.0,
) -> Generator[int, None, list[Gf.Matrix4d] | None]:
    """MuJoCo rigid body simulation — yields frame index, returns all_mats.

    Uses MjSpec to build the scene programmatically from USD mesh data (no file I/O).
    Concave static colliders use heightfields (see _build_heightfield); this
    build of MuJoCo has no generic mesh-SDF collider.
    MuJoCo quaternions are [w,x,y,z] matching USD — no reordering needed.
    """
    # --- bbox / body centre (same as PyBullet version) ---
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    rng = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
    half_extents = [0.5, 0.5, 0.5]
    z_offset = 0.0
    if not rng.IsEmpty():
        sz = rng.GetMax() - rng.GetMin()
        half_extents = [sz[0] / 2, sz[1] / 2, sz[2] / 2]
        z_offset = (rng.GetMin()[up_idx] + rng.GetMax()[up_idx]) / 2 - translate[up_idx]

    # --- apply the initial rotation offset up front, so the hull, the body
    # quaternion and the geom offset are all built in the same final frame ---
    if orx != 0.0 or ory != 0.0 or orz != 0.0:
        rot = (
            rot
            * Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
            * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
            * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz)
        )

    # The body frame is placed at the prim's transform origin (not the bbox
    # centre), so the body rotates about exactly the same point as the USD
    # xform and the collision shape stays locked to the visual mesh while
    # tumbling. The geometry centre sits z_offset above the origin along world
    # up; express that offset in the body's local frame to place the geom.
    _rmat = Gf.Matrix4d(1.0)
    _rmat.SetRotate(rot.GetQuat())
    _up_world = [0.0, 0.0, 0.0]
    _up_world[up_idx] = z_offset
    _c_local = _rmat.GetInverse().TransformDir(Gf.Vec3d(*_up_world))
    geom_offset = [float(_c_local[0]), float(_c_local[1]), float(_c_local[2])]

    # --- collect convex hull vertices if needed ---
    mesh_pts = None
    if shape_type == "Convex Hull":
        # Hull verts in the body-local frame, relative to the PRIM ORIGIN
        # (so they already include the geom offset; geom pos stays 0).
        _body_mat = Gf.Matrix4d(1.0)
        _body_mat.SetRotate(rot.GetQuat())
        _body_mat.SetTranslateOnly(Gf.Vec3d(translate[0], translate[1], translate[2]))
        _body_inv = _body_mat.GetInverse()

        _xc = UsdGeom.XformCache()
        hull_verts = []
        for _child in Usd.PrimRange(prim):
            _mesh = UsdGeom.Mesh(_child)
            _pts = _mesh.GetPointsAttr().Get()
            if not _pts:
                continue
            _cxf = _xc.GetLocalToWorldTransform(_child)
            for _pt in _pts:
                _wp = _cxf.Transform(Gf.Vec3d(_pt[0], _pt[1], _pt[2]))
                # Transform world point into body-local frame
                _lp = _body_inv.Transform(_wp)
                hull_verts.append([float(_lp[0]), float(_lp[1]), float(_lp[2])])
        if hull_verts:
            if hull_max_verts > 0 and len(hull_verts) > hull_max_verts:
                step = max(1, len(hull_verts) // hull_max_verts)
                hull_verts = hull_verts[::step][:hull_max_verts]
            mesh_pts = _np.array(hull_verts, dtype=_np.float32)
        else:
            print("[RBDSolver/MuJoCo] Convex Hull: no mesh data, falling back to Box")

    # --- build MjSpec scene ---
    spec = _mj.MjSpec()
    sub_dt = dt / substeps  # per-step timestep
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.noslip_iterations = 3
    # implicitfast integrates contacts/damping more stably than the default Euler.
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST

    # Contact stiffness timeconst: must be >= 2*timestep for stability. A stiff
    # 0.01 floor keeps impact penetration tiny (~0.1cm vs ~1.4cm at 0.04), so
    # fast impacts at -981 don't visibly clip. Stability under pile-ups comes
    # from the catch-all ground plane (see _add_colliders), not from softening.
    contact_tc = max(2.0 * sub_dt, 0.01)

    # --- static / kinematic collision bodies ---
    dyn_radius = max(half_extents) + max(collision_margin, 0.0)
    mocap_col_names = _add_colliders(
        spec, collision_prims, up_idx, friction, contact_tc, dyn_radius, hfield_res, floor_drop
    )

    # --- initial orientation (offset already applied above) ---
    gq = rot.GetQuat()
    gi = gq.GetImaginary()
    # MuJoCo quaternion convention matches USD: [w, x, y, z]
    init_quat_mj = [gq.GetReal(), float(gi[0]), float(gi[1]), float(gi[2])]

    # --- initial position: body origin == prim transform origin (no z_offset;
    # the geometry offset is carried by the geom, see geom_offset) ---
    init_pos = [translate[0] + otx, translate[1] + oty, translate[2] + otz]

    lat = [j for j in range(3) if j != up_idx]

    # --- dynamic body ---
    dyn_body = spec.worldbody.add_body()
    dyn_body.name = "dynamic"
    dyn_body.pos = list(init_pos)
    dyn_body.quat = init_quat_mj

    free_jnt = dyn_body.add_joint()
    free_jnt.type = _mj.mjtJoint.mjJNT_FREE
    free_jnt.name = "free_joint"

    dyn_geom = dyn_body.add_geom()
    dyn_geom.friction = [friction, 0.005, 0.0001]
    dyn_geom.mass = mass
    # Map restitution (0-1) to solref: lower dampratio = bouncier.
    # solref[0] = timeconst (kept >= 2*timestep for stability), solref[1] = dampratio
    dyn_geom.solref = [contact_tc, 1.0 - max(0.0, min(1.0, restitution)) * 0.9]
    # solimp: [dmin, dmax, width, midpoint, power]
    dyn_geom.solimp = [0.95, 0.99, 0.001, 0.5, 2]
    # condim=3 (normal + sliding friction); condim=6's torsional/rolling
    # constraints destabilise the singular contact at a bowl's concave bottom.
    dyn_geom.condim = 3

    if shape_type == "Sphere":
        sphere_r = max(half_extents[lat[0]], half_extents[lat[1]])
        radius = max(sphere_r + collision_margin, 0.001)
        dyn_geom.type = _mj.mjtGeom.mjGEOM_SPHERE
        dyn_geom.size = [radius, 0.0, 0.0]
        dyn_geom.pos = geom_offset  # centre on geometry, not the prim origin

    elif shape_type == "Convex Hull" and mesh_pts is not None:
        hull_ma = spec.add_mesh()
        hull_ma.name = "hull_dyn"
        hull_ma.uservert = mesh_pts.flatten()
        # MuJoCo builds a convex hull internally from the vertex cloud —
        # maxhullvert caps the output vertex count for the collision hull.
        hull_ma.maxhullvert = max(hull_max_verts, 64) if hull_max_verts > 0 else 1024
        if collision_margin != 0.0:
            max_ext = max(half_extents) if max(half_extents) > 1e-6 else 1.0
            ms = (max_ext + collision_margin) / max_ext
            hull_ma.scale = [ms, ms, ms]
        dyn_geom.type = _mj.mjtGeom.mjGEOM_MESH
        dyn_geom.meshname = "hull_dyn"

    else:
        adj = [max(h + collision_margin, 0.001) for h in half_extents]
        dyn_geom.type = _mj.mjtGeom.mjGEOM_BOX
        dyn_geom.size = adj
        dyn_geom.pos = geom_offset  # centre on geometry, not the prim origin

    # --- compile ---
    try:
        model = spec.compile()
    except Exception as _ce:
        print(f"[RBDSolver/MuJoCo] Scene compile failed: {_ce}")
        return

    data = _mj.MjData(model)

    # Apply linear/angular damping on the freejoint DOFs directly on the compiled model.
    # DOFs 0-2 are translational, 3-5 are rotational.
    jnt_id = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, "free_joint")
    if jnt_id >= 0:
        dof0 = model.jnt_dofadr[jnt_id]
        model.dof_damping[dof0 : dof0 + 3] = lin_damping
        model.dof_damping[dof0 + 3 : dof0 + 6] = ang_damping
        # Set initial velocity
        data.qvel[dof0 : dof0 + 3] = init_vel[:3]
        data.qvel[dof0 + 3 : dof0 + 6] = ang_vel[:3]

    bid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, "dynamic")

    # Resolve mocap body ids for kinematic collision updates
    mocap_ids = []
    for bname, cprim in mocap_col_names:
        mbid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, bname)
        mid = int(model.body_mocapid[mbid])
        mocap_ids.append((mid, cprim))

    print(f"[RBDSolver/MuJoCo] Compiled — {model.nbody} bodies, {model.ngeom} geoms, shape={shape_type}")

    # Initialize forward kinematics so xpos/xquat are valid on frame 0
    _mj.mj_forward(model, data)

    # --- Debug: write collision geoms to USD session layer for visualization ---
    # --- simulation loop ---
    all_mats = []
    for frame in range(frame_count):
        # Update kinematic (mocap) collision bodies from USD time samples
        for mid, cprim in mocap_ids:
            tc = Usd.TimeCode(start_frame + frame)
            kc = UsdGeom.XformCache(tc)
            kxf = kc.GetLocalToWorldTransform(cprim)
            kt = kxf.ExtractTranslation()
            kq = kxf.RemoveScaleShear().ExtractRotation().GetQuat()  # strip scale, see note above
            ki = kq.GetImaginary()
            data.mocap_pos[mid] = [float(kt[0]), float(kt[1]), float(kt[2])]
            data.mocap_quat[mid] = [kq.GetReal(), float(ki[0]), float(ki[1]), float(ki[2])]

        # Sample state before stepping (frame N shows where the body was at the start of step N)
        pos = data.xpos[bid]
        qmj = data.xquat[bid]  # [w, x, y, z] — same convention as USD, no reordering needed

        world_mat = Gf.Matrix4d()
        world_mat.SetRotate(Gf.Quatd(float(qmj[0]), float(qmj[1]), float(qmj[2]), float(qmj[3])))
        # Body origin coincides with the prim origin, so xpos maps directly to
        # the prim transform — the geometry offset is baked into the geom/hull.
        world_mat.SetTranslateOnly(Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2])))
        all_mats.append(world_mat)

        for _ in range(substeps):
            _mj.mj_step(model, data)

        # Update debug collision geoms for this frame
        yield frame

    return all_mats


def _write_prim_results(
    stage: Usd.Stage,
    prim: Usd.Prim,
    layer: Sdf.Layer,
    parent_world_inv: Gf.Matrix4d,
    all_mats: list[Gf.Matrix4d],
    start_frame: int,
    scale_xyz: tuple[float, float, float] | None = None,
) -> None:
    """Write single-prim simulation results to USD (main-thread only)."""
    if scale_xyz and any(abs(s - 1.0) > 1e-6 for s in scale_xyz):
        sx, sy, sz = scale_xyz
        # Build a scale matrix to left-multiply each sim RT frame (row-vector convention:
        # scale_mat * mat scales rows 0,1,2 by sx,sy,sz while preserving translation row 3).
        scale_mat = Gf.Matrix4d()
        scale_mat.SetScale(Gf.Vec3d(sx, sy, sz))

        def _local(mat):
            return scale_mat * mat * parent_world_inv
    else:

        def _local(mat):
            return mat * parent_world_inv

    with Usd.EditContext(stage, layer):
        order_attr = prim.GetAttribute("xformOpOrder")
        if not order_attr:
            order_attr = prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray)
        order_attr.Set(["xformOp:transform"])
        xf_attr = prim.GetAttribute("xformOp:transform")
        if not xf_attr:
            xf_attr = prim.CreateAttribute("xformOp:transform", Sdf.ValueTypeNames.Matrix4d)
        xf_attr.Set(_local(all_mats[0]))
        for frame_idx, mat in enumerate(all_mats):
            xf_attr.Set(_local(mat), Usd.TimeCode(start_frame + frame_idx))
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + len(all_mats) - 1)
    print(f"[RBDSolver] Baked {len(all_mats)} frames on {prim.GetPath()}")


def simulate_mujoco(
    stage: Usd.Stage,
    prim: Usd.Prim,
    layer: Sdf.Layer,
    frame_count: int,
    fps: float,
    gravity: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    parent_world_inv: Gf.Matrix4d,
    otx: float,
    oty: float,
    otz: float,
    orx: float,
    ory: float,
    orz: float,
    collision_prims: list[Usd.Prim] | None = None,
    friction: float = 0.5,
    restitution: float = 0.1,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    scale_xyz: tuple[float, float, float] | None = None,
    shape_type: str = "Convex Hull",
    collision_margin: float = 0.0,
    solver_iters: int = 30,
    hfield_res: int = 96,
    floor_drop: float = 0.0,
) -> list[Gf.Matrix4d] | None:
    """Run MuJoCo simulation."""
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    translate = world_xf.ExtractTranslation()
    # Strip scale/shear first: ExtractRotation() on a scaled matrix returns
    # garbage (e.g. a 100x-scaled -90deg matrix decodes as a 360deg identity),
    # which would bake the wrong orientation onto the prim and desync the
    # collision shape from the visual mesh.
    rot = world_xf.RemoveScaleShear().ExtractRotation()

    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    dt = 1.0 / fps

    # Run the working MuJoCo simulation generator
    gen = _sim_prim_gen_mj(
        prim,
        collision_prims,
        frame_count,
        gravity,
        friction,
        restitution,
        mass,
        substeps,
        init_vel,
        ang_vel,
        start_frame,
        dt,
        otx,
        oty,
        otz,
        orx,
        ory,
        orz,
        up_idx,
        shape_type,
        collision_margin,
        solver_iters,
        translate,
        rot,
        lin_damping,
        ang_damping,
        hfield_res=hfield_res,
        floor_drop=floor_drop,
    )

    # Exhaust the generator to get all_mats
    all_mats = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        all_mats = e.value

    if not all_mats:
        print("[MuJoCoSolver] Simulation produced no results")
        return

    # Write results using the proven writer
    _write_prim_results(stage, prim, layer, parent_world_inv, all_mats, start_frame, scale_xyz)
    print(f"[MuJoCoSolver] Baked {frame_count} frames on {prim.GetPath()}")
    return all_mats


def _proto_hull_verts(stage: Usd.Stage, proto_targets, cap: int = 200) -> _np.ndarray | None:
    """Prototype mesh vertices for a shared convex-hull collider (extracted once).

    Points are gathered in the prototype's world-bound space — the same space the
    caller measures proto extent/centre in — so per-instance scaling stays
    consistent with the sphere/box path. Subsampled to *cap* points to keep the
    per-asset hull build cheap. Returns None if the prototype has no mesh data.
    """
    if _np is None or not proto_targets:
        return None
    proto_prim = stage.GetPrimAtPath(proto_targets[0])
    if not proto_prim:
        return None
    xc = UsdGeom.XformCache()
    verts = []
    for c in Usd.PrimRange(proto_prim):
        mesh = UsdGeom.Mesh(c)
        pts = mesh.GetPointsAttr().Get()
        if not pts:
            continue
        cxf = xc.GetLocalToWorldTransform(c)
        for p in pts:
            w = cxf.Transform(Gf.Vec3d(p[0], p[1], p[2]))
            verts.append([float(w[0]), float(w[1]), float(w[2])])
    if len(verts) < 4:
        return None
    arr = _np.asarray(verts, dtype=_np.float32)
    if len(arr) > cap:
        arr = arr[:: max(1, len(arr) // cap)][:cap]
    return arr


def simulate_mujoco_instancer(
    stage: Usd.Stage,
    prim: Usd.Prim,
    layer: Sdf.Layer,
    collision_prims: list[Usd.Prim] | None,
    frame_count: int,
    fps: float,
    gravity: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    otx: float,
    oty: float,
    otz: float,
    orx: float,
    ory: float,
    orz: float,
    friction: float = 0.5,
    restitution: float = 0.1,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    shape_type: str = "Sphere",
    collision_margin: float = 0.0,
    solver_iters: int = 30,
    hfield_res: int = 96,
    floor_drop: float = 0.0,
) -> None:
    """Run MuJoCo multi-body simulation for a UsdGeom.PointInstancer using MjSpec.

    Each instance gets a free joint and collides with the other instances and the
    same collider stack as the single-prim path (concave heightfield + backstop
    for a bowl, else a ground plane). *shape_type* picks the per-instance collider:
    "Sphere" (fast, most stable in dense piles), "Box", or "Convex Hull" (accurate,
    heavier — the shared prototype hull is built once and reused per distinct
    scale). A sphere is rotationally symmetric, so the rotation offset and each
    instance's own orientation only influence the physics for Box / Convex Hull.
    """
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    instancer = UsdGeom.PointInstancer(prim)
    positions_attr = instancer.GetPositionsAttr()
    orientations_attr = instancer.GetOrientationsAttr()
    positions = positions_attr.Get()
    orientations = orientations_attr.Get()
    if not positions:
        print("[MuJoCoSolver] No positions on PointInstancer")
        return

    num_instances = len(positions)
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    world_inv = world_xf.GetInverse()
    # Instancer orientations are in instancer-local space; convert to/from world
    # with the instancer's own rotation (scale stripped — see note in simulate_mujoco).
    inst_rot = world_xf.RemoveScaleShear().ExtractRotation()
    inst_rot_inv = inst_rot.GetInverse()

    # Prototype's UNSCALED bound (extents + centre). The scatter tool clears the
    # prototype's transform and bakes the real scale into per-instance scales, so
    # the collider must multiply the proto extent by each instance's scale —
    # otherwise the spheres are ~100x too small and the instances clip straight
    # through everything.
    proto_targets = instancer.GetPrototypesRel().GetTargets()
    proto_ext = Gf.Vec3d(1.0, 1.0, 1.0)
    proto_ctr = Gf.Vec3d(0.0, 0.0, 0.0)
    if proto_targets:
        proto_prim = stage.GetPrimAtPath(proto_targets[0])
        if proto_prim:
            bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
            # Local, NOT world: the instancer's world transform (e.g. a Transform node feeding it)
            # is already applied to the body positions via world_xf. Using ComputeWorldBound here
            # would double-count it in the collider centre/extent — offsetting and rescaling the
            # collision geometry away from the visible instance. Identical to the old result when
            # the instancer is untransformed.
            rng = bbox_cache.ComputeLocalBound(proto_prim).ComputeAlignedRange()
            if not rng.IsEmpty():
                proto_ext = rng.GetMax() - rng.GetMin()
                proto_ctr = (rng.GetMin() + rng.GetMax()) * 0.5

    scales = instancer.GetScalesAttr().Get()

    def _instance_scale(idx: int) -> tuple[float, float, float]:
        if scales and idx < len(scales):
            s = scales[idx]
            return float(s[0]), float(s[1]), float(s[2])
        return 1.0, 1.0, 1.0

    def _instance_radius(idx: int) -> float:
        sx, sy, sz = _instance_scale(idx)
        return (
            max(
                float(proto_ext[0]) * sx,
                float(proto_ext[1]) * sy,
                float(proto_ext[2]) * sz,
            )
            / 2.0
        )

    # Representative radius (largest instance) for the heightfield cell-size cap.
    max_radius = max([0.5] + [_instance_radius(i) for i in range(num_instances)]) + max(collision_margin, 0.0)

    dt = 1.0 / fps

    # --- Build MjSpec ---
    spec = _mj.MjSpec()
    # Many piled bodies generate many contacts/constraints; the default solver
    # arena (~16 MB) overflows. Scale the allocation with the instance count.
    spec.memory = max(64 * 1024 * 1024, num_instances * 2 * 1024 * 1024)
    sub_dt = dt / substeps
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.noslip_iterations = 3
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST
    contact_tc = max(2.0 * sub_dt, 0.01)

    # Same colliders as the single-prim path (bowl heightfield + backstop, etc.)
    mocap_col_names = _add_colliders(
        spec, collision_prims, up_idx, friction, contact_tc, max_radius, hfield_res, floor_drop
    )

    dyn_dampratio = 1.0 - max(0.0, min(1.0, restitution)) * 0.9

    # Rotation offset applied to every instance's initial orientation (mirrors the
    # single-prim path). Only meaningful physically for Box / Convex Hull colliders.
    off_rot = None
    if orx != 0.0 or ory != 0.0 or orz != 0.0:
        off_rot = (
            Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
            * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
            * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz)
        )

    # Convex hull: extract the shared prototype geometry once, then build one mesh
    # asset per distinct instance scale (MuJoCo computes the hull per asset).
    hull_base = None
    if shape_type == "Convex Hull":
        hull_base = _proto_hull_verts(stage, proto_targets)
        if hull_base is None:
            print("[MuJoCoSolver] Convex Hull: no prototype mesh data, falling back to Sphere")

    hull_mesh_cache: dict[tuple[float, float, float], str] = {}

    def _hull_mesh_for_scale(sx: float, sy: float, sz: float) -> str:
        key = (round(sx, 5), round(sy, 5), round(sz, 5))
        name = hull_mesh_cache.get(key)
        if name is not None:
            return name
        name = f"hull{len(hull_mesh_cache)}"
        ma = spec.add_mesh()
        ma.name = name
        ma.uservert = hull_base.flatten()
        msx, msy, msz = sx, sy, sz
        if collision_margin != 0.0:
            # Grow the hull outward by ~collision_margin via a uniform scale bump
            # (same approximation the single-prim path uses).
            max_ext = max(float(proto_ext[0]) * sx, float(proto_ext[1]) * sy, float(proto_ext[2]) * sz) / 2.0
            if max_ext > 1e-6:
                mf = (max_ext + collision_margin) / max_ext
                msx, msy, msz = sx * mf, sy * mf, sz * mf
        ma.scale = [msx, msy, msz]
        ma.maxhullvert = 64
        hull_mesh_cache[key] = name
        return name

    # Per-instance bodies
    for i in range(num_instances):
        lp = positions[i]
        pos_world = world_xf.Transform(Gf.Vec3d(float(lp[0]), float(lp[1]), float(lp[2])))
        px = float(pos_world[0]) + otx
        py = float(pos_world[1]) + oty
        pz = float(pos_world[2]) + otz

        if orientations and i < len(orientations):
            q = orientations[i]
            qi = q.GetImaginary()
            local_q = Gf.Quatd(float(q.GetReal()), float(qi[0]), float(qi[1]), float(qi[2]))
        else:
            local_q = Gf.Quatd(1.0, 0.0, 0.0, 0.0)
        # to world: instancer rotation * instance-local orientation * offset
        world_rot = inst_rot * Gf.Rotation(local_q)
        if off_rot is not None:
            world_rot = world_rot * off_rot
        wq = world_rot.GetQuat()
        wqi = wq.GetImaginary()

        sx, sy, sz = _instance_scale(i)
        radius_i = max(_instance_radius(i) + collision_margin, 1e-4)
        # Centre the sphere on the prototype geometry (scaled, in body-local), so
        # the collider tracks the visual instance instead of its origin.
        gpos = [
            float(proto_ctr[0]) * sx,
            float(proto_ctr[1]) * sy,
            float(proto_ctr[2]) * sz,
        ]

        body = spec.worldbody.add_body()
        body.name = f"b{i}"
        body.pos = [px, py, pz]
        body.quat = [wq.GetReal(), float(wqi[0]), float(wqi[1]), float(wqi[2])]
        jnt = body.add_joint()
        jnt.type = _mj.mjtJoint.mjJNT_FREE
        g = body.add_geom()
        if hull_base is not None:
            # Shared prototype hull, scaled per instance. The verts already carry the
            # geometry offset, so the geom stays at the body origin (no gpos).
            g.type = _mj.mjtGeom.mjGEOM_MESH
            g.meshname = _hull_mesh_for_scale(sx, sy, sz)
        elif shape_type == "Box":
            g.type = _mj.mjtGeom.mjGEOM_BOX
            g.size = [
                max(float(proto_ext[0]) * sx / 2.0 + collision_margin, 1e-4),
                max(float(proto_ext[1]) * sy / 2.0 + collision_margin, 1e-4),
                max(float(proto_ext[2]) * sz / 2.0 + collision_margin, 1e-4),
            ]
            g.pos = gpos
        else:
            g.type = _mj.mjtGeom.mjGEOM_SPHERE
            g.size = [radius_i, 0.0, 0.0]
            g.pos = gpos
        g.mass = mass
        g.friction = [friction, 0.005, 0.0001]
        g.solref = [contact_tc, dyn_dampratio]
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3

    # Compile
    try:
        model = spec.compile()
    except Exception as e:
        print(f"[MuJoCoSolver] Instancer compile failed: {e}")
        return

    data = _mj.MjData(model)

    # Resolve body ids by name (collider bodies precede the instances now) and
    # apply damping + initial velocity on each instance's own DOFs.
    bids = []
    for i in range(num_instances):
        bid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, f"b{i}")
        bids.append(bid)
        dof0 = int(model.jnt_dofadr[int(model.body_jntadr[bid])])
        model.dof_damping[dof0 : dof0 + 3] = lin_damping
        model.dof_damping[dof0 + 3 : dof0 + 6] = ang_damping
        if init_vel:
            data.qvel[dof0 : dof0 + 3] = init_vel[:3]
        if ang_vel:
            data.qvel[dof0 + 3 : dof0 + 6] = ang_vel[:3]

    # Resolve mocap collider ids for per-frame kinematic updates
    mocap_ids = []
    for bname, cprim in mocap_col_names:
        mbid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, bname)
        mocap_ids.append((int(model.body_mocapid[mbid]), cprim))

    _mj.mj_forward(model, data)

    # Simulate
    all_positions = []
    all_orientations = []
    for frame in range(frame_count):
        # Update kinematic (mocap) collision bodies from USD time samples
        for mid, cprim in mocap_ids:
            tc = Usd.TimeCode(start_frame + frame)
            kc = UsdGeom.XformCache(tc)
            kxf = kc.GetLocalToWorldTransform(cprim)
            kt = kxf.ExtractTranslation()
            kq = kxf.RemoveScaleShear().ExtractRotation().GetQuat()
            ki = kq.GetImaginary()
            data.mocap_pos[mid] = [float(kt[0]), float(kt[1]), float(kt[2])]
            data.mocap_quat[mid] = [kq.GetReal(), float(ki[0]), float(ki[1]), float(ki[2])]

        frame_pos = Vt.Vec3fArray(num_instances)
        frame_ori = Vt.QuathArray(num_instances)
        for i in range(num_instances):
            bid = bids[i]
            pos = data.xpos[bid]
            qmj = data.xquat[bid]

            world_pos = Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2]))
            local_pos = world_inv.Transform(world_pos)
            frame_pos[i] = Gf.Vec3f(float(local_pos[0]), float(local_pos[1]), float(local_pos[2]))
            # world orientation back to instancer-local
            wq = Gf.Quatd(float(qmj[0]), float(qmj[1]), float(qmj[2]), float(qmj[3]))
            lq = (inst_rot_inv * Gf.Rotation(wq)).GetQuat()
            li = lq.GetImaginary()
            frame_ori[i] = Gf.Quath(float(lq.GetReal()), float(li[0]), float(li[1]), float(li[2]))

        all_positions.append(frame_pos)
        all_orientations.append(frame_ori)

        for _ in range(substeps):
            _mj.mj_step(model, data)

    # Write time samples
    with Usd.EditContext(stage, layer):
        for frame_idx in range(frame_count):
            tc = Usd.TimeCode(start_frame + frame_idx)
            positions_attr.Set(all_positions[frame_idx], tc)
            orientations_attr.Set(all_orientations[frame_idx], tc)

    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)

    print(f"[MuJoCoSolver] Baked {frame_count} frames on PointInstancer {prim.GetPath()} ({num_instances} bodies)")


# ---------------------------------------------------------------------------
# Chunk UI
# ---------------------------------------------------------------------------


def _parse_vec3(s: str) -> list[float]:
    parts = (s or "").split()
    if len(parts) >= 3:
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    return [0.0, 0.0, 0.0]


def _parse_collision_prims(value: str, stage: Usd.Stage | None) -> list[Usd.Prim]:
    """Parse a newline-or-comma-separated path list into valid Usd.Prim objects."""
    result = []
    if not stage:
        return result
    for path in (value or "").replace(",", "\n").split("\n"):
        path = path.strip()
        if not path:
            continue
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            result.append(prim)
    return result


def _clear_chunk_bake(owner) -> None:
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)


def _promote_bake_layer(layer: Sdf.Layer | None) -> None:
    """Make the bake layer the strongest chunk layer for its target prim.

    Chunk layers are ordered so the earlier-added sibling wins. When the solver
    animates a prim that another chunk authored (e.g. a PointInstancer created by
    ScatterPoints), that source chunk's static values would otherwise override the
    baked time samples. Move the bake layer — and its parent graph layer — to the
    front of their containers so the simulation wins.
    """
    lreg = chunk.get_layer_manager()
    if not lreg or not layer:
        return
    try:
        root = lreg.get_root_layer()
        for parent, sub in lreg.iter_chunk_layers():
            if sub.identifier != layer.identifier:
                continue
            # Strongest sibling within its parent graph layer.
            if sub.identifier in parent.subLayerPaths and parent.subLayerPaths[0] != sub.identifier:
                parent.subLayerPaths.remove(sub.identifier)
                parent.subLayerPaths.insert(0, sub.identifier)
            # Strongest graph layer under the chunks root.
            if parent.identifier in root.subLayerPaths and root.subLayerPaths[0] != parent.identifier:
                root.subLayerPaths.remove(parent.identifier)
                root.subLayerPaths.insert(0, parent.identifier)
            break
    except Exception as e:
        print(f"[MuJoCoSolver] Could not promote bake layer: {e}")


def reset(owner, notice, sender) -> None:
    _clear_chunk_bake(owner)
    print("[MuJoCoSolver] Reset — bake cleared.")


def add_collision(owner, notice, sender) -> None:
    """Append the viewport-selected prim(s) to the collision list (comma-joined
    so get_prims() picks them all up)."""
    param = owner.get_chunk().get_param("collision_path")
    scene = chunk_registry.ChunkRegistry().scene
    if not scene:
        return
    selected = scene.get_selected_prims()
    if not selected:
        print("[MuJoCoSolver] No prim selected in viewport")
        return
    existing = [p.strip() for p in (param.value or "").replace(",", "\n").split("\n") if p.strip()]
    added = 0
    for prim in selected:
        path = str(prim.GetPath())
        if path and path != "/" and path not in existing:
            existing.append(path)
            added += 1
    param.value = "\n".join(existing)
    print(f"[MuJoCoSolver] Added {added} collision prim(s); {len(existing)} total")


def clear_collision(owner, notice, sender) -> None:
    owner.get_chunk().get_param("collision_path").value = ""
    print("[MuJoCoSolver] Cleared collision prims")


def _run(owner, notice, sender) -> None:
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prim_paths = _chunk.get_param("prim_path").resolve_paths()
    if not prim_paths:
        print("[MuJoCoSolver] No target prim selected")
        return

    # A typed param's .value already returns the correct type and falls back to its
    # default_value when unset, so no cast or `or <fallback>` is needed. (The `or`
    # form was also a latent bug: it clobbered a legitimate 0, e.g. zero bounce.)
    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)

    collision_path_value = _chunk.get_param("collision_path", is_s).value

    frame_count = _chunk.get_param("frame_count", is_i).value
    fps = _chunk.get_param("fps", is_f).value
    gravity = _chunk.get_param("gravity", is_f).value
    substeps = max(1, _chunk.get_param("substeps", is_i).value)
    start_frame = _chunk.get_param("start_frame", is_i).value
    friction = _chunk.get_param("friction", is_f).value
    restitution = _chunk.get_param("restitution", is_f).value
    mass = _chunk.get_param("mass", is_f).value
    lin_damping = _chunk.get_param("lin_damping", is_f).value
    ang_damping = _chunk.get_param("ang_damping", is_f).value
    shape_type = _chunk.get_param("shape_type", is_s).value
    otx = _chunk.get_param("off_tx", is_f).value
    oty = _chunk.get_param("off_ty", is_f).value
    otz = _chunk.get_param("off_tz", is_f).value
    orx = _chunk.get_param("off_rx", is_f).value
    ory = _chunk.get_param("off_ry", is_f).value
    orz = _chunk.get_param("off_rz", is_f).value
    init_vel = _parse_vec3(_chunk.get_param("initial_velocity", is_s).value)
    ang_vel = _parse_vec3(_chunk.get_param("angular_velocity", is_s).value)
    floor_drop = _chunk.get_param("floor_drop", is_f).value

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # The target prim comes from the REAL stage, not a scratch one: simulate_mujoco[_instancer]
    # write baked results back through the same instancer/attribute objects they read `prim`
    # from (positions_attr.Set(...) inside a `with Usd.EditContext(stage, layer)`), and
    # Usd.EditContext only governs the stage object it's given -- it has no effect on attributes
    # bound to a *different* Usd.Stage. A scratch-stage-bound prim here silently bakes the whole
    # simulation onto the throwaway stage instead of `layer`, discarded the moment this function
    # returns (found the hard way: "Run Simulation" printed a successful bake, but the layer
    # stayed empty). Collision prims are read-only -- never written back through -- so they're
    # still safely scoped through get_upstream_stage: a downstream chunk's edit to a collision
    # prim can't leak into the simulation, without risking the same silent-write-loss bug.
    prim = stage.GetPrimAtPath(prim_paths[0])
    if not prim.IsValid() or prim.GetPath() == Sdf.Path("/"):
        return
    scratch_stage = chunk_usd.get_upstream_stage(stage, owner.get_scene(), _chunk)
    collision_prims = _parse_collision_prims(collision_path_value, scratch_stage)

    if UsdGeom.PointInstancer(prim).GetPositionsAttr().HasValue():
        simulate_mujoco_instancer(
            stage,
            prim,
            layer,
            collision_prims,
            frame_count,
            fps,
            gravity,
            mass,
            substeps,
            init_vel,
            ang_vel,
            start_frame,
            otx,
            oty,
            otz,
            orx,
            ory,
            orz,
            friction=friction,
            restitution=restitution,
            lin_damping=lin_damping,
            ang_damping=ang_damping,
            shape_type=shape_type,
            collision_margin=_chunk.get_param("collision_margin", is_f).value,
            solver_iters=30,
            hfield_res=_chunk.get_param("hfield_res", is_i).value,
            floor_drop=floor_drop,
        )
    else:
        xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        parent = prim.GetParent()
        parent_world_inv = Gf.Matrix4d(1.0)
        if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
            parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()

        # Extract local scale so we can preserve it in the baked transform
        local_xf = xform_cache.GetLocalTransformation(prim)[0]
        _r0 = local_xf.GetRow3(0)
        _r1 = local_xf.GetRow3(1)
        _r2 = local_xf.GetRow3(2)
        scale_xyz = (
            (_r0[0] ** 2 + _r0[1] ** 2 + _r0[2] ** 2) ** 0.5,
            (_r1[0] ** 2 + _r1[1] ** 2 + _r1[2] ** 2) ** 0.5,
            (_r2[0] ** 2 + _r2[1] ** 2 + _r2[2] ** 2) ** 0.5,
        )

        simulate_mujoco(
            stage,
            prim,
            layer,
            frame_count,
            fps,
            gravity,
            mass,
            substeps,
            init_vel,
            ang_vel,
            start_frame,
            parent_world_inv,
            otx,
            oty,
            otz,
            orx,
            ory,
            orz,
            collision_prims=collision_prims,
            friction=friction,
            restitution=restitution,
            lin_damping=lin_damping,
            ang_damping=ang_damping,
            scale_xyz=scale_xyz,
            shape_type=shape_type,
            collision_margin=_chunk.get_param("collision_margin", is_f).value,
            solver_iters=30,
            hfield_res=_chunk.get_param("hfield_res", is_i).value,
            floor_drop=floor_drop,
        )

    # Ensure the baked animation overrides any chunk (e.g. ScatterPoints) that authored
    # the target prim, otherwise its static values win and playback shows nothing.
    _promote_bake_layer(layer)

    try:
        scene = chunk_registry.ChunkRegistry().scene
        if scene:
            scene.refresh_timeline(start_frame, start_frame + frame_count - 1)
    except Exception:
        pass


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("RBD Solver")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The prim to simulate — a single mesh, or a PointInstancer to simulate every instance.",
    )
    chunk_api.add_param(prim_path)

    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    col_grp = chunk.Group("Collision", flags=_GF)
    col_grp.add_param(
        chunk.TextEdit(
            "collision_path",
            "Collider Prims",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Static collision geometry — one prim path per line. Use 'Add "
            "Selected' to append, or edit/delete lines directly to remove.",
        )
    )
    add_col_btn = chunk.Button(
        "add_collision",
        "Add Selected",
        tooltip="Append the prim(s) selected in the usdview hierarchy to the list.",
    )
    add_col_btn.add_callback(add_collision)
    col_grp.add_param(add_col_btn)

    clear_col_btn = chunk.Button(
        "clear_collision",
        "Clear",
        tooltip="Remove all collider prims.",
    )
    clear_col_btn.add_callback(clear_collision)
    col_grp.add_param(clear_col_btn)
    col_grp.add_param(
        chunk.Spinbox(
            "floor_drop",
            "Floor Drop",
            default_value=0.0,
            tooltip="Lower the catch-all ground plane by this much below the collider's "
            "lowest point, so bodies that roll off the rim keep falling further.",
        )
    )
    chunk_api.add_param(col_grp)

    sim_grp = chunk.Group("Simulation", flags=_GF)
    sim_grp.add_param(
        chunk.Spinbox(
            "frame_count",
            "Frame Count",
            default_value=120,
            is_int=True,
            tooltip="Number of frames to simulate and bake.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "fps", "FPS", default_value=24.0, tooltip="Frames per second — sets the simulation timestep (1/fps)."
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "gravity",
            "Gravity",
            default_value=-981.0,
            tooltip="Gravity along the scene's up axis, in scene units/s². Default -981 assumes centimetres.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "substeps",
            "Substeps",
            default_value=8,
            is_int=True,
            tooltip="Physics steps per frame. Higher = more stable/accurate but slower.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "start_frame", "Start Frame", default_value=1, is_int=True, tooltip="First frame of the baked animation."
        )
    )
    chunk_api.add_param(sim_grp)

    physics_grp = chunk.Group("Physics", flags=_GF)
    # Sphere is the most stable shape against the heightfield collider; Convex
    # Hull is more accurate on flat ground but can be unstable on heightfields.
    physics_grp.add_param(
        chunk.Menu(
            "shape_type",
            "Collision Shape",
            menu_items=["Sphere", "Box", "Convex Hull"],
            default_value="Sphere",
            tooltip="Collision shape for the dynamic body: Sphere (fastest, most stable), Box, "
            "or Convex Hull (most accurate, can be unstable on concave colliders).",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "collision_margin",
            "Collision Margin",
            default_value=0.0,
            tooltip="Extra padding grown around every collider, in scene units. Helps avoid clipping.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "hfield_res",
            "Collider Resolution",
            default_value=64,
            is_int=True,
            tooltip="Grid resolution of the heightfield built from concave colliders (bowls/terrain). "
            "Higher = finer surface but heavier.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox("friction", "Friction", default_value=0.5, tooltip="Surface friction (0 = frictionless).")
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "restitution", "Bounce", default_value=0.1, tooltip="Bounciness: 0 = no bounce, 1 = fully elastic."
        )
    )
    physics_grp.add_param(chunk.Spinbox("mass", "Mass", default_value=1.0, tooltip="Mass of the dynamic body."))
    physics_grp.add_param(
        chunk.Spinbox(
            "lin_damping",
            "Linear Damping",
            default_value=0.0,
            tooltip="Linear drag slowing the body's motion (0 = none).",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "ang_damping", "Angular Damping", default_value=0.05, tooltip="Rotational drag slowing the body's spin."
        )
    )
    chunk_api.add_param(physics_grp)

    _ot_tip = "Shift the body's start position along {} before simulating, in scene units."
    _or_tip = "Pre-rotate the body around {} before simulating, in degrees."
    offset_grp = chunk.Group("Offsets", flags=_GF)
    offset_grp.add_param(chunk.Spinbox("off_tx", "Offset X", default_value=0.0, tooltip=_ot_tip.format("X")))
    offset_grp.add_param(chunk.Spinbox("off_ty", "Offset Y", default_value=0.0, tooltip=_ot_tip.format("Y")))
    offset_grp.add_param(chunk.Spinbox("off_tz", "Offset Z", default_value=0.0, tooltip=_ot_tip.format("Z")))
    offset_grp.add_param(chunk.Spinbox("off_rx", "Rotate X", default_value=0.0, tooltip=_or_tip.format("X")))
    offset_grp.add_param(chunk.Spinbox("off_ry", "Rotate Y", default_value=0.0, tooltip=_or_tip.format("Y")))
    offset_grp.add_param(chunk.Spinbox("off_rz", "Rotate Z", default_value=0.0, tooltip=_or_tip.format("Z")))
    chunk_api.add_param(offset_grp)

    vel_grp = chunk.Group("Velocity", flags=_GF)
    vel_grp.add_param(
        chunk.LineEdit(
            "initial_velocity",
            "Linear Velocity",
            default_value="0 0 0",
            tooltip="Starting linear velocity as 'x y z', in scene units/s.",
        )
    )
    vel_grp.add_param(
        chunk.LineEdit(
            "angular_velocity",
            "Angular Velocity",
            default_value="0 0 0",
            tooltip="Starting angular velocity as 'x y z', in radians/s.",
        )
    )
    chunk_api.add_param(vel_grp)

    run_btn = chunk.Button("run_sim", "Run Simulation", tooltip="Simulate and bake the animation onto the target prim.")
    run_btn.add_callback(_run)
    chunk_api.add_param(run_btn)

    reset_btn = chunk.Button("reset_sim", "Reset", tooltip="Clear the baked animation from this chunk's layer.")
    reset_btn.add_callback(reset)
    chunk_api.add_param(reset_btn)

    return chunk_api


def config() -> chunk.DefaulConfig:
    return chunk.DefaulConfig(
        category="Simulation",
        description=(
            "Bakes a MuJoCo rigid-body simulation onto the target prim — or every instance\n"
            "of a PointInstancer — as keyframed animation over the frame range. Add colliders\n"
            "for objects to rest on or in (a heightfield is built for concave shapes like\n"
            "bowls). Requires the 'mujoco' package."
        ),
    )
