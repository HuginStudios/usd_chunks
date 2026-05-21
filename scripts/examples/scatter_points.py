"""Scatter Points chunk — distributes instances across a mesh surface."""

from __future__ import annotations

import bisect
import math
import random

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk

_ORIENT_MODES = ["Align to Normal", "Keep Original", "Up", "Random"]
_RAND_AXES = ["Off", "X", "Y", "Z", "All"]
_SCATTER_MODES = ["Surface", "Volume", "Stack"]


def _collect_triangles(prim):
    """Collect world-space triangles from prim and its descendants."""
    if not prim or not prim.IsValid():
        return []

    triangles = []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        face_counts = mesh.GetFaceVertexCountsAttr().Get()
        face_indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not face_counts or not face_indices:
            continue

        xform_cache = UsdGeom.XformCache()
        world_xf = xform_cache.GetLocalToWorldTransform(p)
        world_pts = []
        for pt in points:
            wp = world_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
            world_pts.append(Gf.Vec3f(wp[0], wp[1], wp[2]))

        idx = 0
        for fc in face_counts:
            if fc >= 3:
                v0 = world_pts[face_indices[idx]]
                for i in range(1, fc - 1):
                    v1 = world_pts[face_indices[idx + i]]
                    v2 = world_pts[face_indices[idx + i + 1]]
                    e1 = v1 - v0
                    e2 = v2 - v0
                    n = Gf.Vec3f(
                        e1[1] * e2[2] - e1[2] * e2[1],
                        e1[2] * e2[0] - e1[0] * e2[2],
                        e1[0] * e2[1] - e1[1] * e2[0],
                    )
                    ln = Gf.GetLength(n)
                    if ln > 1e-8:
                        n = n / ln
                    triangles.append((v0, v1, v2, n))
            idx += fc

    if not triangles:
        # Fallback: flat quad on top face of bounding box
        bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
        bbox = bbox_cache.ComputeWorldBound(prim)
        bbox_range = bbox.ComputeAlignedRange()
        if not bbox_range.IsEmpty():
            mn = bbox_range.GetMin()
            mx = bbox_range.GetMax()
            v0 = Gf.Vec3f(mn[0], mn[1], mx[2])
            v1 = Gf.Vec3f(mx[0], mn[1], mx[2])
            v2 = Gf.Vec3f(mx[0], mx[1], mx[2])
            v3 = Gf.Vec3f(mn[0], mx[1], mx[2])
            up = Gf.Vec3f(0, 0, 1)
            triangles.append((v0, v1, v2, up))
            triangles.append((v0, v2, v3, up))

    return triangles


def _scatter_on_triangles(triangles, count, rng, min_dist=0):
    """Area-weighted random point sampling with optional min-distance rejection."""
    areas = []
    for v0, v1, v2, _ in triangles:
        e1 = v1 - v0
        e2 = v2 - v0
        cross = Gf.Vec3f(
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        areas.append(0.5 * Gf.GetLength(cross))

    total_area = sum(areas)
    if total_area < 1e-10:
        return [], []

    cdf = []
    cumulative = 0.0
    for a in areas:
        cumulative += a / total_area
        cdf.append(cumulative)

    positions = []
    normals = []
    min_dist_sq = min_dist * min_dist
    max_attempts = count * 100

    # Spatial hash grid for O(1) proximity checks
    grid: dict = {}
    cell = max(min_dist, 1e-6)

    def _grid_key(pt):
        return (math.floor(pt[0] / cell), math.floor(pt[1] / cell), math.floor(pt[2] / cell))

    def _too_close(pt):
        cx, cy, cz = _grid_key(pt)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for dz in range(-1, 2):
                    for ex in grid.get((cx + dx, cy + dy, cz + dz), ()):
                        d = (pt[0]-ex[0])**2 + (pt[1]-ex[1])**2 + (pt[2]-ex[2])**2
                        if d < min_dist_sq:
                            return True
        return False

    attempts = 0
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        r = rng.random()
        tri_idx = min(bisect.bisect_left(cdf, r), len(triangles) - 1)
        v0, v1, v2, n = triangles[tri_idx]

        u = rng.random()
        v = rng.random()
        if u + v > 1.0:
            u, v = 1.0 - u, 1.0 - v
        w = 1.0 - u - v
        pt = Gf.Vec3f(
            w * v0[0] + u * v1[0] + v * v2[0],
            w * v0[1] + u * v1[1] + v * v2[1],
            w * v0[2] + u * v1[2] + v * v2[2],
        )

        if min_dist_sq > 0 and _too_close(pt):
            continue

        positions.append(pt)
        normals.append(n)
        key = _grid_key(pt)
        grid.setdefault(key, []).append(pt)

    return positions, normals


def _point_in_mesh(pt, triangles):
    """Ray-cast in +X direction; odd intersection count = inside mesh."""
    ox, oy, oz = pt[0], pt[1], pt[2]
    hits = 0
    for v0, v1, v2, _ in triangles:
        # Möller–Trumbore
        e1x = v1[0] - v0[0]; e1y = v1[1] - v0[1]; e1z = v1[2] - v0[2]
        e2x = v2[0] - v0[0]; e2y = v2[1] - v0[1]; e2z = v2[2] - v0[2]
        # ray direction = (1, 0, 0)
        # h = ray_dir × e2 = (0*e2z - 0*e2y, 0*e2x - 1*e2z, 1*e2y - 0*e2x)
        hx = -e2z; hy = 0.0; hz = e2y
        a = e1x * hx + e1y * hy + e1z * hz
        if abs(a) < 1e-10:
            continue
        f = 1.0 / a
        sx = ox - v0[0]; sy = oy - v0[1]; sz = oz - v0[2]
        u = f * (sx * hx + sy * hy + sz * hz)
        if u < 0.0 or u > 1.0:
            continue
        # q = s × e1
        qx = sy * e1z - sz * e1y
        qy = sz * e1x - sx * e1z
        qz = sx * e1y - sy * e1x
        v = f * qx  # ray_dir · q = 1*qx + 0*qy + 0*qz
        if v < 0.0 or u + v > 1.0:
            continue
        t = f * (e2x * qx + e2y * qy + e2z * qz)
        if t > 1e-10:
            hits += 1
    return (hits % 2) == 1


def _fill_in_volume(triangles, count, rng, min_dist=0):
    """Uniformly fill interior of closed mesh with random points."""
    xs = [v[0] for tri in triangles for v in tri[:3]]
    ys = [v[1] for tri in triangles for v in tri[:3]]
    zs = [v[2] for tri in triangles for v in tri[:3]]
    mn = (min(xs), min(ys), min(zs))
    mx = (max(xs), max(ys), max(zs))

    positions = []
    up = Gf.Vec3f(0, 1, 0)  # normals not meaningful for volume; caller replaces
    normals = []
    min_dist_sq = min_dist * min_dist
    max_attempts = count * 200

    grid: dict = {}
    cell = max(min_dist, 1e-6)

    def _grid_key(pt):
        return (math.floor(pt[0] / cell), math.floor(pt[1] / cell), math.floor(pt[2] / cell))

    def _too_close(pt):
        cx, cy, cz = _grid_key(pt)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for dz in range(-1, 2):
                    for ex in grid.get((cx + dx, cy + dy, cz + dz), ()):
                        d = (pt[0]-ex[0])**2 + (pt[1]-ex[1])**2 + (pt[2]-ex[2])**2
                        if d < min_dist_sq:
                            return True
        return False

    attempts = 0
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        px = rng.uniform(mn[0], mx[0])
        py = rng.uniform(mn[1], mx[1])
        pz = rng.uniform(mn[2], mx[2])
        pt = Gf.Vec3f(px, py, pz)
        if not _point_in_mesh(pt, triangles):
            continue
        if min_dist_sq > 0 and _too_close(pt):
            continue
        positions.append(pt)
        normals.append(up)
        key = _grid_key(pt)
        grid.setdefault(key, []).append(pt)

    return positions, normals


def _stack_in_container(triangles, proto_prim, count, rng, scale=1.0, up_idx=1,
                         settle_frames=300, stack_shape="Sphere"):
    """Physics fill: drop instances into a container mesh and let them settle."""
    try:
        import pybullet as p
    except ImportError:
        print("[ScatterPoints] pybullet not available for Stack mode")
        return [], []

    # Flat vertex list for PyBullet mesh shape
    flat_verts = []
    for v0, v1, v2, _ in triangles:
        flat_verts.extend([[v0[0], v0[1], v0[2]], [v1[0], v1[1], v1[2]], [v2[0], v2[1], v2[2]]])
    xs = [v[0] for v in flat_verts]
    ys = [v[1] for v in flat_verts]
    zs = [v[2] for v in flat_verts]
    mn = [min(xs), min(ys), min(zs)]
    mx = [max(xs), max(ys), max(zs)]
    indices = list(range(len(flat_verts)))

    # Estimate instance size from prototype bbox
    half_exts = [0.5 * scale, 0.5 * scale, 0.5 * scale]
    if proto_prim and proto_prim.IsValid():
        try:
            bcache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
            bbox = bcache.ComputeWorldBound(proto_prim)
            prange = bbox.ComputeAlignedRange()
            if not prange.IsEmpty():
                psize = prange.GetSize()
                half_exts = [psize[0] * 0.5 * scale, psize[1] * 0.5 * scale, psize[2] * 0.5 * scale]
        except Exception:
            pass
    flat_axes = [j for j in range(3) if j != up_idx]
    # Circumscribed sphere in horizontal plane (max, not min) — ensures nothing tunnels
    sphere_r = max(max(half_exts[flat_axes[0]], half_exts[flat_axes[1]]), 0.01)
    half_h = half_exts[up_idx]

    # CoM offset: prototype local origin sits at the mesh BASE (z=0 in Z-up asset local
    # space). PyBullet bodies are centred at their CoM. Without this correction the sphere
    # spawns with its bottom half inside the container — violent contact recovery ("going
    # crazy" on first collision). half_h is the distance from base to object centre.
    com_up = half_h

    # Normalize to unit sim space — PyBullet is calibrated for ~1-unit objects. Use
    # max(container, object) so neither is microscopic or gigantic in sim space.
    container_dims = [mx[j] - mn[j] for j in range(3)]
    container_max_dim = max(container_dims) if max(container_dims) > 1e-6 else 1.0
    obj_diameter = max(half_exts) * 2
    normalize_dim = max(container_max_dim, obj_diameter)
    K = 1.0 / normalize_dim

    ratio = obj_diameter / container_max_dim if container_max_dim > 1e-9 else 0
    if ratio > 5.0:
        print(f"[ScatterPoints] WARNING: proto ({obj_diameter:.3f}) is {ratio:.1f}x the container "
              f"({container_max_dim:.3f}). Both assets must be at the same world scale "
              f"(e.g. both 100x scaled from meters to cm).")
    elif ratio < 0.02:
        print(f"[ScatterPoints] WARNING: proto ({obj_diameter:.3f}) is tiny vs container "
              f"({container_max_dim:.3f}). Likely only one asset was scaled.")
    else:
        print(f"[ScatterPoints] Stack: container={container_max_dim:.3f}  proto={obj_diameter:.3f}  K={K:.4f}")

    def _s(v):
        return [c * K for c in v]

    flat_verts_sim = [_s(v) for v in flat_verts]
    mn_sim = [c * K for c in mn]
    mx_sim = [c * K for c in mx]
    half_exts_sim = [h * K for h in half_exts]
    sphere_r_sim = sphere_r * K
    half_h_sim = half_h * K
    com_sim = com_up * K

    # PyBullet setup — use a unique client so it doesn't clash with RBD solver threads
    cid = p.connect(p.DIRECT)
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = -9.81  # fixed unit-scale gravity — physical time doesn't matter for settling
    p.setGravity(*grav, physicsClientId=cid)
    p.setTimeStep(1.0 / 60.0, physicsClientId=cid)
    p.setPhysicsEngineParameter(numSolverIterations=30, physicsClientId=cid)

    # Container as static concave trimesh — handles bowls, cups, any concave shape
    col_mesh = p.createCollisionShape(
        p.GEOM_MESH,
        vertices=flat_verts_sim,
        indices=indices,
        flags=p.GEOM_FORCE_CONCAVE_TRIMESH,
        physicsClientId=cid,
    )
    container_body = p.createMultiBody(0, col_mesh, physicsClientId=cid)
    p.changeDynamics(container_body, -1, lateralFriction=0.8, restitution=0.1, physicsClientId=cid)

    # Instance collision shape — must be convex for dynamic bodies
    if stack_shape == "Box":
        inst_col = p.createCollisionShape(
            p.GEOM_BOX, halfExtents=half_exts_sim, physicsClientId=cid
        )
    else:
        inst_col = p.createCollisionShape(
            p.GEOM_SPHERE, radius=sphere_r_sim, physicsClientId=cid
        )

    # Spawn zone above container opening, inset so objects don't clip the rim
    drop_top_sim = mx_sim[up_idx] + half_h_sim * 2
    margin_sim = sphere_r_sim * 1.5
    spawn_lo_sim = [mn_sim[j] + margin_sim for j in range(3)]
    spawn_hi_sim = [mx_sim[j] - margin_sim for j in range(3)]

    bodies = []
    # Stagger 1.5× apple-heights between levels. 80 settle steps (~1.3s at 60 Hz) gives
    # enough time for the highest item in a batch (≤12 × 1.5 ≈ 18 heights above drop_top)
    # to reach the container before the next batch starts — avoids mid-air collisions.
    batch = max(1, count // 8)
    stagger_sim = half_h_sim * 1.5

    for i in range(count):
        pos = [0.0, 0.0, 0.0]
        for j in flat_axes:
            lo, hi = spawn_lo_sim[j], spawn_hi_sim[j]
            pos[j] = rng.uniform(lo, hi) if hi > lo else (mn_sim[j] + mx_sim[j]) * 0.5
        # com_sim lifts the spawn so the sphere centre = apple centre, not apple base.
        # Without this the bottom half of the sphere starts inside the container floor.
        pos[up_idx] = drop_top_sim + (i % batch) * stagger_sim + com_sim
        body = p.createMultiBody(
            baseMass=1.0,
            baseCollisionShapeIndex=inst_col,
            basePosition=pos,
            physicsClientId=cid,
        )
        p.changeDynamics(body, -1,
            restitution=0.1, lateralFriction=0.8,
            ccdSweptSphereRadius=sphere_r_sim * 0.5,
            physicsClientId=cid,
        )
        bodies.append(body)
        if (i + 1) % batch == 0:
            for _ in range(80):
                p.stepSimulation(physicsClientId=cid)

    # Final settle pass
    for _ in range(settle_frames):
        p.stepSimulation(physicsClientId=cid)

    positions = []
    orientations = []
    for body in bodies:
        pos, orn = p.getBasePositionAndOrientation(body, physicsClientId=cid)
        # Sphere centre back to world space, then subtract CoM offset so the instance
        # position = prototype local origin = apple base, not apple centre.
        # This way instances sit flush on the container rather than hovering by half_h.
        world = [pos[j] / K for j in range(3)]
        world[up_idx] -= com_up
        positions.append(Gf.Vec3f(float(world[0]), float(world[1]), float(world[2])))
        # PyBullet orn = (x, y, z, w); Gf.Quath = (w, ix, iy, iz)
        orientations.append(Gf.Quath(float(orn[3]), float(orn[0]), float(orn[1]), float(orn[2])))

    p.disconnect(cid)
    return positions, orientations


def _normal_to_quat(normal):
    """Convert surface normal to a quaternion rotation (Z-up → normal direction)."""
    up = Gf.Vec3f(0, 0, 1)
    n = Gf.Vec3f(normal[0], normal[1], normal[2])
    ln = Gf.GetLength(n)
    if ln < 1e-8:
        return Gf.Quath(1, 0, 0, 0)
    n = n / ln
    dot = Gf.Dot(up, n)
    if dot > 0.9999:
        return Gf.Quath(1, 0, 0, 0)
    if dot < -0.9999:
        return Gf.Quath(0, 1, 0, 0)
    axis = Gf.Cross(up, n)
    axis_len = Gf.GetLength(axis)
    if axis_len < 1e-8:
        return Gf.Quath(1, 0, 0, 0)
    axis = axis / axis_len
    angle = math.acos(max(-1.0, min(1.0, dot)))
    half = angle * 0.5
    s = math.sin(half)
    return Gf.Quath(math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def _quat_mul(a, b):
    """Multiply two Quath quaternions."""
    aw, ax, ay, az = a.GetReal(), *a.GetImaginary()
    bw, bx, by, bz = b.GetReal(), *b.GetImaginary()
    return Gf.Quath(
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _compute_orientation(mode, normal):
    if mode == "Align to Normal":
        return _normal_to_quat(normal)
    return Gf.Quath(1, 0, 0, 0)


def _apply_random_rotation(base_quat, axis, amount_deg, rng):
    if axis == "Off":
        return base_quat
    amount_rad = math.radians(amount_deg)
    if axis == "All":
        ax = Gf.Vec3f(rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))
        ln = Gf.GetLength(ax)
        if ln < 1e-8:
            return base_quat
        ax = ax / ln
        angle = rng.uniform(-amount_rad, amount_rad)
    else:
        ax_map = {"X": Gf.Vec3f(1, 0, 0), "Y": Gf.Vec3f(0, 1, 0), "Z": Gf.Vec3f(0, 0, 1)}
        ax = ax_map[axis]
        angle = rng.uniform(-amount_rad, amount_rad)
    half = angle * 0.5
    s = math.sin(half)
    rand_q = Gf.Quath(math.cos(half), ax[0] * s, ax[1] * s, ax[2] * s)
    return _quat_mul(rand_q, base_quat)


def _on_mode_changed(owner, notice, sender):
    """Show/hide the Stack group when scatter mode changes."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    stack_grp = chunk_obj.get_param("Stack")
    if not stack_grp:
        return
    if owner.value == "Stack":
        stack_grp.unset_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        stack_grp.set_state(chunk.ChunkFlags.State.HIDDEN)


def execute(owner, notice, sender):
    """Scatter instances on a mesh surface — triggered by the Scatter button."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    surface_prims = _chunk.get_param("surface_prim").get_prims()
    surface_prim = surface_prims[0] if surface_prims else None

    proto_prims = _chunk.get_param("proto_prim").get_prims()
    proto_prim = proto_prims[0] if proto_prims else None

    inst_path = _chunk.get_param("inst_path").value or "/ScatterInstancer"
    count = int(_chunk.get_param("count").value or 100)
    seed = int(_chunk.get_param("seed").value or 0)
    scale = float(_chunk.get_param("scale").value or 1.0)
    min_dist = float(_chunk.get_param("min_distance").value or 0)
    scatter_mode = _chunk.get_param("scatter_mode").value or "Surface"
    orient_mode = _chunk.get_param("orient_mode").value or "Align to Normal"
    rand_axis = _chunk.get_param("rand_axis").value or "Off"
    rand_amount = float(_chunk.get_param("rand_amount").value or 360)
    settle_frames = int(_chunk.get_param("settle_frames").value or 300)
    stack_shape = _chunk.get_param("stack_shape").value or "Sphere"

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Erase stale positions/orientations time samples (and defaults) from every
    # other layer that has opinions on this instancer — typically a solver bake.
    # Without this, re-scattering with a different count leaves the solver's old
    # N-item arrays winning over scatter's new M-item arrays, which breaks the
    # PointInstancer (mismatched proxyIndices vs positions → 1 visible instance).
    _inst_sdf = Sdf.Path(inst_path)
    for _lyr in stage.GetLayerStack():
        if _lyr.identifier == layer.identifier:
            continue
        for _prop in ("positions", "orientations"):
            _pp = _inst_sdf.AppendProperty(_prop)
            for _t in list(_lyr.ListTimeSamplesForPath(_pp)):
                _lyr.EraseTimeSample(_pp, _t)
            _spec = _lyr.GetAttributeAtPath(_pp)
            if _spec:
                _spec.ClearDefaultValue()

    triangles = _collect_triangles(surface_prim)
    rng = random.Random(seed)

    up_axis = UsdGeom.GetStageUpAxis(stage)
    up_idx = 1 if up_axis.upper() == "Y" else 2
    world_up = Gf.Vec3f(0, 1, 0) if up_idx == 1 else Gf.Vec3f(0, 0, 1)
    normals = []
    stack_orientations = None

    if scatter_mode == "Volume":
        if triangles:
            positions, normals = _fill_in_volume(triangles, count, rng, min_dist)
            normals = [world_up] * len(positions)
        else:
            positions = []
    elif scatter_mode == "Stack":
        if triangles:
            positions, stack_orientations = _stack_in_container(
                triangles, proto_prim, count, rng, scale, up_idx, settle_frames, stack_shape
            )
        else:
            positions = []
    else:
        # Surface mode — only upward-facing triangles
        surf_tris = [t for t in triangles if Gf.Dot(t[3], world_up) > 0] if triangles else []
        if not surf_tris:
            sp = 2.0
            n = int(math.sqrt(count))
            off = (n - 1) * sp / 2
            positions = []
            for x in range(n):
                for y in range(n):
                    jx = rng.uniform(-sp * 0.3, sp * 0.3)
                    jy = rng.uniform(-sp * 0.3, sp * 0.3)
                    positions.append(Gf.Vec3f(x * sp - off + jx, y * sp - off + jy, 0))
            normals = [Gf.Vec3f(0, 0, 1)] * len(positions)
        else:
            positions, normals = _scatter_on_triangles(surf_tris, count, rng, min_dist)

    # Read the proto prim's world-space scale BEFORE we write anything.
    # We clear xformOpOrder on the prototype ref so instances sit at the scatter
    # points rather than inheriting a position offset from the source prim.
    # But that also strips any scale applied by chunks like OffsetTransform.
    # Extracting the world scale here and baking it into instance scales fixes that.
    proto_scale = Gf.Vec3f(1.0, 1.0, 1.0)
    if proto_prim and proto_prim.IsValid():
        _xc = UsdGeom.XformCache(Usd.TimeCode.Default())
        _wm = _xc.GetLocalToWorldTransform(proto_prim)
        _r0, _r1, _r2 = _wm.GetRow3(0), _wm.GetRow3(1), _wm.GetRow3(2)
        proto_scale = Gf.Vec3f(
            (_r0[0] ** 2 + _r1[0] ** 2 + _r2[0] ** 2) ** 0.5,
            (_r0[1] ** 2 + _r1[1] ** 2 + _r2[1] ** 2) ** 0.5,
            (_r0[2] ** 2 + _r1[2] ** 2 + _r2[2] ** 2) ** 0.5,
        )

    with Usd.EditContext(stage, layer):
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)

        proto_scope = f"{inst_path}/Prototypes"
        stage.DefinePrim(proto_scope, "Scope")
        proto_path = f"{proto_scope}/proto_0"
        if proto_prim and proto_prim.IsValid():
            ref_prim = stage.DefinePrim(proto_path)
            ref_prim.GetReferences().AddInternalReference(proto_prim.GetPath())
            ref_prim.SetInstanceable(False)
            UsdGeom.Xformable(ref_prim).GetXformOpOrderAttr().Set([])
        else:
            cube = UsdGeom.Cube.Define(stage, proto_path)
            cube.GetSizeAttr().Set(1.0)

        instancer.GetPrototypesRel().SetTargets([Sdf.Path(proto_path)])

        if positions:
            instancer.GetPositionsAttr().Set(Vt.Vec3fArray(positions))
            instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0] * len(positions)))
            instancer.GetScalesAttr().Set(
                Vt.Vec3fArray([Gf.Vec3f(
                    scale * proto_scale[0],
                    scale * proto_scale[1],
                    scale * proto_scale[2],
                )] * len(positions))
            )

            if stack_orientations is not None:
                instancer.GetOrientationsAttr().Set(Vt.QuathArray(stack_orientations))
            else:
                rng2 = random.Random(seed + 7)
                orientations = [
                    _apply_random_rotation(
                        _compute_orientation(orient_mode, nrm), rand_axis, rand_amount, rng2
                    )
                    for nrm in normals
                ]
                instancer.GetOrientationsAttr().Set(Vt.QuathArray(orientations))

    print(f"[ScatterPoints] Scattered {len(positions)} instances at {inst_path}")


def main():
    chunk_api = chunk.Chunk("Scatter Points")

    surface_prim = chunk.LineEdit(
        "surface_prim",
        "Surface Mesh",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Mesh to scatter instances on. In Stack mode this becomes the container (e.g. a bowl) that instances are dropped into.",
    )
    chunk_api.add_param(surface_prim)

    proto_prim = chunk.LineEdit(
        "proto_prim",
        "Prototype Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Prim to instantiate at each scatter point. Leave empty to use a default cube.",
    )
    chunk_api.add_param(proto_prim)

    chunk_api.add_param(
        chunk.LineEdit(
            "inst_path",
            "Instancer Path",
            default_value="/ScatterInstancer",
            tooltip="USD path where the PointInstancer prim will be created.",
        )
    )

    scatter_grp = chunk.Group("Scatter")
    scatter_mode_param = chunk.Menu(
        "scatter_mode",
        "Mode",
        menu_items=_SCATTER_MODES,
        default_value="Surface",
        tooltip="Surface: place instances on upward-facing faces.\nVolume: fill the interior of a closed mesh.\nStack: physics simulation — drop instances into a container and let them settle.",
    )
    scatter_mode_param.add_callback(_on_mode_changed)
    scatter_grp.add_param(scatter_mode_param)
    scatter_grp.add_param(
        chunk.Spinbox(
            "count",
            "Point Count",
            default_value=100,
            is_int=True,
            tooltip="Number of instances to place.",
        )
    )
    scatter_grp.add_param(
        chunk.Spinbox(
            "seed",
            "Seed",
            default_value=0,
            is_int=True,
            tooltip="Random seed. Change this to get a different scatter pattern with the same settings.",
        )
    )
    scatter_grp.add_param(
        chunk.Spinbox(
            "scale",
            "Instance Scale",
            default_value=1.0,
            tooltip="Uniform scale applied to every instance.",
        )
    )
    scatter_grp.add_param(
        chunk.Spinbox(
            "min_distance",
            "Min Distance",
            default_value=0.0,
            tooltip="Minimum distance between instances in scene units. 0 means no limit. Increase to prevent overlap and clumping.",
        )
    )
    chunk_api.add_param(scatter_grp)

    orient_grp = chunk.Group("Orientation")
    orient_grp.add_param(
        chunk.Menu(
            "orient_mode",
            "Orientation Mode",
            menu_items=_ORIENT_MODES,
            default_value="Align to Normal",
            tooltip="Align to Normal: tilt instances to follow the surface.\nKeep Original: use the prototype's own orientation.\nUp: always point toward world up.\nRandom: fully randomized orientation.",
        )
    )
    orient_grp.add_param(
        chunk.Menu(
            "rand_axis",
            "Random Rotation Axis",
            menu_items=_RAND_AXES,
            default_value="Off",
            tooltip="Apply a random rotation around this axis after the base orientation is set. Off disables randomization.",
        )
    )
    orient_grp.add_param(
        chunk.Spinbox(
            "rand_amount",
            "Random Rotation (deg)",
            default_value=360.0,
            tooltip="Maximum random rotation in degrees. Each instance gets a random angle between -this and +this value.",
        )
    )
    chunk_api.add_param(orient_grp)

    stack_grp = chunk.Group("Stack")
    stack_grp.set_state(chunk.ChunkFlags.State.HIDDEN)
    stack_grp.add_param(
        chunk.Spinbox(
            "settle_frames",
            "Settle Frames",
            default_value=300,
            is_int=True,
            tooltip="Physics frames to simulate after all instances have been dropped. More frames = more time to settle into the container. Used only in Stack mode.",
        )
    )
    stack_grp.add_param(
        chunk.Menu(
            "stack_shape",
            "Collision Shape",
            menu_items=["Sphere", "Box"],
            default_value="Sphere",
            tooltip="Collision shape used for each instance during Stack simulation.\nSphere: fast, good for round objects like apples or oranges.\nBox: better for rectangular items like books or blocks.",
        )
    )
    chunk_api.add_param(stack_grp)

    scatter_btn = chunk.Button(
        "scatter",
        "Scatter",
        tooltip="Run the scatter. In Stack mode this triggers a physics simulation — may take a few seconds.",
    )
    scatter_btn.add_callback(execute)
    chunk_api.add_param(scatter_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Create")
