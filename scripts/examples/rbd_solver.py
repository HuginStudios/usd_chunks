"""RBD Solver chunk — rigid body physics simulation via PyBullet."""

from __future__ import annotations

import os as _os
import shutil as _shutil
import tempfile as _tempfile
import traceback as _tb

from pxr import Gf, Sdf, Usd, UsdGeom

from usd_chunks import chunk

try:
    import numpy as _np
    import pybullet as _pb
    import pybullet_data as _pb_data

    _HAS_PYBULLET = True
except ImportError:
    _HAS_PYBULLET = False
    _np = None

try:
    import mujoco as _mj

    _HAS_MUJOCO = True
    if _np is None:
        import numpy as _np
except ImportError:
    _HAS_MUJOCO = False
    _mj = None

_SHAPE_TYPES = ["Box", "Sphere", "Convex Hull"]

# Prevent double-launch; keep progress dialogs alive during simulation.
_active_sims: dict = {}
_active_dialogs: dict = {}


def _parse_collision_prims(value, stage):
    """Parse newline-or-comma-separated prim paths into a list of valid Usd.Prim objects."""
    result = []
    for path in (value or "").replace(",", "\n").split("\n"):
        path = path.strip()
        if not path:
            continue
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            result.append(prim)
    return result


def add_collision(owner, notice, sender):
    """Append currently selected viewport prim(s) to the collision list."""
    from usd_chunks import chunk_registry

    _chunk = owner.get_chunk()
    scene = chunk_registry.ChunkRegistry().scene
    if not scene:
        return
    selected = scene.get_selected_prims()
    if not selected:
        print("[RBDSolver] No prim selected in viewport")
        return
    existing = _chunk.get_param("collision_path").value or ""
    existing_paths = [
        p.strip() for p in existing.replace(",", "\n").split("\n") if p.strip()
    ]
    added = 0
    for prim in selected:
        path = str(prim.GetPath())
        if path and path != "/" and path not in existing_paths:
            existing_paths.append(path)
            added += 1
    _chunk.get_param("collision_path").value = "\n".join(existing_paths)
    print(
        f"[RBDSolver] Added {added} prim(s). Collision list: {len(existing_paths)} total"
    )


def clear_collision(owner, notice, sender):
    """Clear all collision prims."""
    owner.get_chunk().get_param("collision_path").value = ""
    print("[RBDSolver] Cleared collision prims")


_SETTINGS_PARAMS = [
    "frame_count",
    "fps",
    "gravity",
    "substeps",
    "solver_iters",
    "start_frame",
    "friction",
    "restitution",
    "mass",
    "lin_damping",
    "ang_damping",
    "shape_type",
    "collision_margin",
    "hull_max_verts",
    "off_tx",
    "off_ty",
    "off_tz",
    "off_rx",
    "off_ry",
    "off_rz",
    "initial_velocity",
    "angular_velocity",
]


_APPLE_PRESET = {
    "frame_count": 120,
    "fps": 24.0,
    "gravity": -981.0,
    "substeps": 40,
    "solver_iters": 30,
    "start_frame": 1,
    "friction": 0.6,
    "restitution": 0.05,
    "mass": 1.0,
    "lin_damping": 0.0,
    "ang_damping": 0.05,
    "shape_type": "Convex Hull",
    "collision_margin": 1.0,
    "off_tx": 0.0,
    "off_ty": 30.0,
    "off_tz": 0.0,
    "off_rx": 0.0,
    "off_ry": 0.0,
    "off_rz": 0.0,
    "initial_velocity": "0 0 0",
    "angular_velocity": "0 0 0",
}


def preset_apple(owner, notice, sender):
    """Apply the 'Apple in Bowl' preset — good starting values for a round fruit in a concave bowl."""
    _chunk = owner.get_chunk()
    for _name, _val in _APPLE_PRESET.items():
        _p = _chunk.get_param(_name)
        if _p:
            _p.value = _val
    print("[RBDSolver] Loaded preset: apple")


def save_settings(owner, notice, sender):
    """Serialize all simulation parameters to a JSON file via a native save dialog."""
    import json

    try:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            None, "Save RBD Settings", _os.path.expanduser("~"), "JSON Files (*.json)"
        )
    except Exception:
        path = ""
    if not path:
        return
    _chunk = owner.get_chunk()
    data = {
        n: _chunk.get_param(n).value for n in _SETTINGS_PARAMS if _chunk.get_param(n)
    }
    with open(path, "w") as _f:
        json.dump(data, _f, indent=2)
    print(f"[RBDSolver] Settings saved to {path}")


def load_settings(owner, notice, sender):
    """Restore simulation parameters from a previously saved JSON file via a native open dialog."""
    import json

    try:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            None, "Load RBD Settings", _os.path.expanduser("~"), "JSON Files (*.json)"
        )
    except Exception:
        path = ""
    if not path or not _os.path.exists(path):
        return
    _chunk = owner.get_chunk()
    with open(path) as _f:
        data = json.load(_f)
    for name, value in data.items():
        _p = _chunk.get_param(name)
        if _p:
            _p.value = value
    print(f"[RBDSolver] Settings loaded from {path}")


def _clear_chunk_bake(owner):
    """Remove all time-sampled animation written by this chunk to its edit layer."""
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)


def reset(owner, notice, sender):
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return
    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        return
    prim = prims[0]
    if not prim.IsValid() or prim.GetPath() == Sdf.Path("/"):
        return
    _clear_chunk_bake(owner)
    print(f"[RBDSolver] Reset {prim.GetPath()}")


def _parse_vec3(s: str) -> list[float]:
    parts = (s or "").split()
    if len(parts) >= 3:
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    return [0.0, 0.0, 0.0]


def _extract_mesh_data(prim):
    """Return (vertices, indices) from a UsdGeom.Mesh, or (None, None)."""
    mesh = UsdGeom.Mesh(prim)
    pts = mesh.GetPointsAttr().Get()
    if pts is None or len(pts) == 0:
        return None, None
    vertices = _np.array([[v[0], v[1], v[2]] for v in pts])
    face_idx_attr = mesh.GetFaceVertexIndicesAttr()
    raw = face_idx_attr.Get() if face_idx_attr else None
    indices = list(raw) if raw is not None else None
    return vertices, indices


def _load_static_mesh(p, vertices, indices, friction, restitution):
    """Load a static concave mesh as Bullet BVH trimesh via URDF (useFixedBase=True).
    BVH is exact to the mesh surface — the standard for static concave level geometry."""
    tmpdir = None
    try:
        tmpdir = _tempfile.mkdtemp(prefix="rbd_bvh_")
        obj_path = _os.path.join(tmpdir, "mesh.obj")
        urdf_path = _os.path.join(tmpdir, "mesh.urdf")

        with open(obj_path, "w") as _f:
            for _v in vertices:
                _f.write(f"v {_v[0]:.6f} {_v[1]:.6f} {_v[2]:.6f}\n")
            # Write each triangle twice with reversed winding — BVH trimesh is single-sided
            # (only collides from the normal-facing direction). Double-winding makes it
            # two-sided so the apple collides correctly from inside the bowl at any angle.
            for _ti in range(0, len(indices), 3):
                _i0, _i1, _i2 = (
                    indices[_ti] + 1,
                    indices[_ti + 1] + 1,
                    indices[_ti + 2] + 1,
                )
                _f.write(f"f {_i0} {_i1} {_i2}\n")
                _f.write(f"f {_i0} {_i2} {_i1}\n")

        # Use forward slashes so PyBullet's URDF parser handles Windows paths correctly.
        obj_uri = obj_path.replace("\\", "/")
        with open(urdf_path, "w") as _f:
            _f.write(
                '<?xml version="1.0"?>\n<robot name="col">\n'
                '  <link name="base">\n    <collision>\n'
                f'      <geometry><mesh filename="{obj_uri}"/></geometry>\n'
                "    </collision>\n  </link>\n</robot>\n"
            )

        body = p.loadURDF(urdf_path, [0.0, 0.0, 0.0], useFixedBase=True)
        p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
        print("[RBDSolver] Bowl: BVH trimesh loaded")
        return body
    except Exception as _e:
        print(f"[RBDSolver] BVH load failed ({_e}), trying VHACD")
        return None
    finally:
        if tmpdir:
            _shutil.rmtree(tmpdir, ignore_errors=True)


def _try_vhacd_static(p, vertices, indices, friction, restitution):
    """
    Decompose a concave mesh into convex pieces via VHACD and create one static
    PyBullet body per piece. The union of all pieces gives reliable concave collision
    using standard GJK convex-convex detection (far more robust than GImpact).
    Returns the first body ID, or None if VHACD is unavailable or fails.
    """
    tmpdir = None
    try:
        tmpdir = _tempfile.mkdtemp(prefix="rbd_vhacd_")
        obj_in = _os.path.join(tmpdir, "in.obj")
        obj_out = _os.path.join(tmpdir, "out.obj")
        log_out = _os.path.join(tmpdir, "log.txt")

        with open(obj_in, "w") as _f:
            for _v in vertices:
                _f.write(f"v {_v[0]:.6f} {_v[1]:.6f} {_v[2]:.6f}\n")
            for _ti in range(0, len(indices), 3):
                _f.write(
                    f"f {indices[_ti] + 1} {indices[_ti + 1] + 1} {indices[_ti + 2] + 1}\n"
                )

        p.vhacd(obj_in, obj_out, log_out)

        if not _os.path.exists(obj_out) or _os.path.getsize(obj_out) == 0:
            return None

        # V-HACD resets vertex numbering per 'o' group — collect per-group vertex lists.
        pieces, cur = [], []
        with open(obj_out) as _f:
            for _line in _f:
                _tok = _line.split()
                if not _tok:
                    continue
                if _tok[0] == "o":
                    if cur:
                        pieces.append(cur)
                    cur = []
                elif _tok[0] == "v":
                    cur.append([float(_tok[1]), float(_tok[2]), float(_tok[3])])
            if cur:
                pieces.append(cur)

        if not pieces:
            return None

        first_body = None
        for _pverts in pieces:
            if len(_pverts) < 4:
                continue
            _sh = p.createCollisionShape(p.GEOM_MESH, vertices=_pverts)
            _bd = p.createMultiBody(
                baseMass=0, baseCollisionShapeIndex=_sh, basePosition=[0, 0, 0]
            )
            p.changeDynamics(_bd, -1, lateralFriction=friction, restitution=restitution)
            if first_body is None:
                first_body = _bd

        if first_body is not None:
            print(f"[RBDSolver] VHACD: {len(pieces)} convex pieces")
        return first_body
    except Exception as _e:
        print(
            f"[RBDSolver] VHACD unavailable ({type(_e).__name__}: {_e}), using GImpact fallback"
        )
        return None
    finally:
        if tmpdir:
            _shutil.rmtree(tmpdir, ignore_errors=True)


def _add_collision_body(p, stage, collision_prim, friction, restitution, up_idx=2):
    """Add a static collision body to PyBullet. Returns (body_id, is_animated_kinematic)."""
    plane_normal = [0, 0, 0]
    plane_normal[up_idx] = 1

    if collision_prim is None or not collision_prim.IsValid():
        ground = p.createCollisionShape(p.GEOM_PLANE, planeNormal=plane_normal)
        body = p.createMultiBody(
            baseMass=0, baseCollisionShapeIndex=ground, basePosition=[0, 0, 0]
        )
        p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
        return body, False

    xform_cache = UsdGeom.XformCache()
    world_xf = xform_cache.GetLocalToWorldTransform(collision_prim)
    translate = world_xf.ExtractTranslation()
    body_pos = [translate[0], translate[1], translate[2]]

    is_animated = any(
        op.GetNumTimeSamples() > 1
        for op in UsdGeom.Xformable(collision_prim).GetOrderedXformOps()
    )

    col_shape = None
    lateral_axes = [i for i in range(3) if i != up_idx]

    sphere = UsdGeom.Sphere(collision_prim)
    if sphere.GetRadiusAttr().HasValue():
        radius = sphere.GetRadiusAttr().Get()
        scale = world_xf.GetRow3(0).GetLength()
        col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=radius * scale)

    if col_shape is None:
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]
        )
        coll_rng = bbox_cache.ComputeWorldBound(collision_prim).ComputeAlignedRange()
        if not coll_rng.IsEmpty():
            coll_sz = coll_rng.GetMax() - coll_rng.GetMin()
            max_lateral = max(coll_sz[i] for i in lateral_axes)
            if max_lateral > 0 and coll_sz[up_idx] / max_lateral < 0.1:
                # Flat surface → finite GEOM_BOX (objects fall off edges, reliable for large meshes).
                mn = coll_rng.GetMin()
                mx = coll_rng.GetMax()
                dims = [mx[i] - mn[i] for i in range(3)]
                col_shape = p.createCollisionShape(
                    p.GEOM_BOX,
                    halfExtents=[max(dims[i] / 2, 0.5) for i in range(3)],
                )
                body_pos = [(mn[i] + mx[i]) / 2 for i in range(3)]

    if col_shape is None:
        for child in Usd.PrimRange(collision_prim):
            mesh = UsdGeom.Mesh(child)
            points = mesh.GetPointsAttr().Get()
            if not points:
                continue
            face_counts = mesh.GetFaceVertexCountsAttr().Get()
            face_indices = mesh.GetFaceVertexIndicesAttr().Get()
            if not face_counts or not face_indices:
                continue
            child_xf = xform_cache.GetLocalToWorldTransform(child)
            vertices = [
                list(child_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))) for pt in points
            ]
            indices = []
            idx = 0
            for fc in face_counts:
                if fc >= 3:
                    for i in range(1, fc - 1):
                        indices.extend(
                            [
                                face_indices[idx],
                                face_indices[idx + i],
                                face_indices[idx + i + 1],
                            ]
                        )
                idx += fc
            if vertices and indices:
                # BVH trimesh (via URDF) is Bullet's exact static concave collision.
                # VHACD is the fallback when BVH loading fails.
                bvh_body = _load_static_mesh(
                    p, vertices, indices, friction, restitution
                )
                if bvh_body is not None:
                    return bvh_body, is_animated
                vhacd_body = _try_vhacd_static(
                    p, vertices, indices, friction, restitution
                )
                if vhacd_body is not None:
                    return vhacd_body, is_animated
                # Final fallback: GImpact concave trimesh + reversed-winding body.
                col_shape = p.createCollisionShape(
                    p.GEOM_MESH,
                    vertices=vertices,
                    indices=indices,
                    meshScale=[1, 1, 1],
                    flags=p.GEOM_FORCE_CONCAVE_TRIMESH,
                )
                body_pos = [0, 0, 0]
                rev_indices = []
                for _ti in range(0, len(indices), 3):
                    rev_indices.extend(
                        [indices[_ti], indices[_ti + 2], indices[_ti + 1]]
                    )
                rev_shape = p.createCollisionShape(
                    p.GEOM_MESH,
                    vertices=vertices,
                    indices=rev_indices,
                    meshScale=[1, 1, 1],
                    flags=p.GEOM_FORCE_CONCAVE_TRIMESH,
                )
                rev_body = p.createMultiBody(
                    baseMass=0,
                    baseCollisionShapeIndex=rev_shape,
                    basePosition=[0, 0, 0],
                )
                p.changeDynamics(
                    rev_body, -1, lateralFriction=friction, restitution=restitution
                )
                break

    if col_shape is None:
        bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
        rng = bbox_cache.ComputeWorldBound(collision_prim).ComputeAlignedRange()
        if not rng.IsEmpty():
            mn, mx = rng.GetMin(), rng.GetMax()
            dims = [mx[i] - mn[i] for i in range(3)]
            col_shape = p.createCollisionShape(
                p.GEOM_BOX,
                halfExtents=[max(dims[i] / 2, 0.5) for i in range(3)],
            )
            body_pos = [(mn[i] + mx[i]) / 2 for i in range(3)]

    if col_shape is None:
        print("[RBDSolver] Could not create collision shape, using ground plane")
        col_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=plane_normal)
        body_pos = [0, 0, 0]

    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=col_shape, basePosition=body_pos
    )
    p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
    return body, is_animated


# ---------------------------------------------------------------------------
# Pure simulation functions — no USD writes, safe to call from a thread.
# ---------------------------------------------------------------------------


def _sim_instancer_gen(
    stage,
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
    up_idx=2,
    shape_type="Box",
    collision_margin=0.0,
    solver_iters=10,
    lin_damping=0.2,
    ang_damping=0.2,
):
    """Simulate a PointInstancer; yields frame index per step, returns (all_positions, all_orientations)."""
    instancer = UsdGeom.PointInstancer(prim)
    positions = instancer.GetPositionsAttr().Get()
    orientations = instancer.GetOrientationsAttr().Get()

    if not positions or len(positions) == 0:
        print("[RBDSolver] PointInstancer has no positions")
        return None

    n = len(positions)

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    inst_world_xf = xform_cache.GetLocalToWorldTransform(prim)
    inst_world_inv = inst_world_xf.GetInverse()

    # Derive collision shape size and CoM offset from the first prototype's bbox.
    # Instancer positions sit at the prototype's local origin; PyBullet bodies sit
    # at their centre of mass. com_offset bridges the two.
    half_extents = [0.5, 0.5, 0.5]
    com_offset = 0.0
    proto_mesh_pts = None
    proto_targets = instancer.GetPrototypesRel().GetTargets()
    if proto_targets:
        proto_prim = stage.GetPrimAtPath(proto_targets[0])
        if proto_prim.IsValid():
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]
            )
            proto_rng = bbox_cache.ComputeWorldBound(proto_prim).ComputeAlignedRange()
            if not proto_rng.IsEmpty():
                sz = proto_rng.GetMax() - proto_rng.GetMin()
                half_extents = [
                    max(sz[0] / 2, 0.1),
                    max(sz[1] / 2, 0.1),
                    max(sz[2] / 2, 0.1),
                ]
                com_offset = (
                    proto_rng.GetMax()[up_idx] + proto_rng.GetMin()[up_idx]
                ) / 2.0
            if shape_type == "Convex Hull":
                # Transform vertices world→proto-local and centre on CoM.
                # Raw local-mesh verts are wrong when parent prims carry scale/rotation.
                _hxc = UsdGeom.XformCache()
                _proto_inv = _hxc.GetLocalToWorldTransform(proto_prim).GetInverse()
                _hull_verts = []
                for _child in Usd.PrimRange(proto_prim):
                    _mesh = UsdGeom.Mesh(_child)
                    _pts = _mesh.GetPointsAttr().Get()
                    if not _pts:
                        continue
                    _cxf = _hxc.GetLocalToWorldTransform(_child)
                    for _pt in _pts:
                        _wp = _cxf.Transform(Gf.Vec3d(_pt[0], _pt[1], _pt[2]))
                        _lp = _proto_inv.Transform(_wp)
                        v = [float(_lp[0]), float(_lp[1]), float(_lp[2])]
                        v[up_idx] -= com_offset
                        _hull_verts.append(v)
                if _hull_verts:
                    proto_mesh_pts = _np.array(_hull_verts)

    p = _pb
    p.connect(p.DIRECT)
    p.setAdditionalSearchPath(_pb_data.getDataPath())
    gravity_vec = [0, 0, 0]
    gravity_vec[up_idx] = gravity
    p.setGravity(*gravity_vec)
    p.setTimeStep(dt / substeps)
    p.setPhysicsEngineParameter(numSolverIterations=solver_iters)

    animated_cols = []
    if not collision_prims:
        _add_collision_body(p, stage, None, friction, restitution, up_idx)
    else:
        for cp in collision_prims:
            cbody, is_anim = _add_collision_body(
                p, stage, cp, friction, restitution, up_idx
            )
            if is_anim:
                animated_cols.append((cbody, cp))

    rot_offset_pb = None
    if orx != 0.0 or ory != 0.0 or orz != 0.0:
        r = (
            Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
            * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
            * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz)
        )
        rq = r.GetQuat()
        ri = rq.GetImaginary()
        rot_offset_pb = [float(ri[0]), float(ri[1]), float(ri[2]), float(rq.GetReal())]

    if shape_type == "Sphere":
        radius = max(max(half_extents) + collision_margin, 0.001)
        col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=radius)
    elif shape_type == "Convex Hull" and proto_mesh_pts is not None:
        max_ext = max(half_extents) if max(half_extents) > 1e-6 else 1.0
        ms = max((max_ext + collision_margin) / max_ext, 0.001)
        col_shape = p.createCollisionShape(
            p.GEOM_MESH, meshScale=[ms, ms, ms], vertices=proto_mesh_pts
        )
    else:
        adj = [max(h + collision_margin, 0.001) for h in half_extents]
        col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=adj)

    bodies = []
    for i in range(n):
        lpos = positions[i]
        wpos = inst_world_xf.Transform(
            Gf.Vec3d(float(lpos[0]), float(lpos[1]), float(lpos[2]))
        )

        if orientations and i < len(orientations):
            q = orientations[i]
            imag = q.GetImaginary()
            pb_orn = [
                float(imag[0]),
                float(imag[1]),
                float(imag[2]),
                float(q.GetReal()),
            ]
        else:
            pb_orn = [0.0, 0.0, 0.0, 1.0]

        if rot_offset_pb is not None:
            _, pb_orn = p.multiplyTransforms(
                [0, 0, 0], pb_orn, [0, 0, 0], rot_offset_pb
            )

        # Rotate local CoM offset by body orientation so the box centre is correct
        # regardless of how the instance is initially tilted.
        local_com = [0.0, 0.0, 0.0]
        local_com[up_idx] = com_offset
        world_com, _ = p.multiplyTransforms([0, 0, 0], pb_orn, local_com, [0, 0, 0, 1])
        pb_pos = [
            wpos[0] + otx + world_com[0],
            wpos[1] + oty + world_com[1],
            wpos[2] + otz + world_com[2],
        ]

        body = p.createMultiBody(
            baseMass=mass,
            baseCollisionShapeIndex=col_shape,
            basePosition=pb_pos,
            baseOrientation=list(pb_orn),
        )
        p.changeDynamics(
            body,
            -1,
            lateralFriction=friction,
            restitution=restitution,
            linearDamping=lin_damping,
            angularDamping=ang_damping,
        )
        p.resetBaseVelocity(body, linearVelocity=init_vel, angularVelocity=ang_vel)
        bodies.append(body)

    all_positions = []
    all_orientations = []

    try:
        for _frame in range(frame_count):
            for cbody, cprim in animated_cols:
                tc = Usd.TimeCode(start_frame + _frame)
                kc = UsdGeom.XformCache(tc)
                kxf = kc.GetLocalToWorldTransform(cprim)
                kt = kxf.ExtractTranslation()
                kq = kxf.ExtractRotation().GetQuat()
                ki = kq.GetImaginary()
                p.resetBasePositionAndOrientation(
                    cbody,
                    [kt[0], kt[1], kt[2]],
                    [ki[0], ki[1], ki[2], kq.GetReal()],
                )

            frame_pos = []
            frame_orn = []
            for body in bodies:
                pos, orn = p.getBasePositionAndOrientation(body)
                local_neg_com = [0.0, 0.0, 0.0]
                local_neg_com[up_idx] = -com_offset
                world_neg_com, _ = p.multiplyTransforms(
                    [0, 0, 0], orn, local_neg_com, [0, 0, 0, 1]
                )
                corrected = [pos[j] + world_neg_com[j] for j in range(3)]
                local_p = inst_world_inv.Transform(Gf.Vec3d(*corrected))
                frame_pos.append(
                    Gf.Vec3f(float(local_p[0]), float(local_p[1]), float(local_p[2]))
                )
                frame_orn.append(
                    Gf.Quath(float(orn[3]), float(orn[0]), float(orn[1]), float(orn[2]))
                )

            all_positions.append(frame_pos)
            all_orientations.append(frame_orn)

            for _ in range(substeps):
                p.stepSimulation()

            yield _frame
    finally:
        p.disconnect()
    return all_positions, all_orientations


def _write_instancer_results(
    stage, prim, layer, all_positions, all_orientations, start_frame
):
    """Write PointInstancer simulation results to USD (main-thread only)."""
    frame_count = len(all_positions)
    instancer = UsdGeom.PointInstancer(prim)
    with Usd.EditContext(stage, layer):
        pos_attr = instancer.GetPositionsAttr()
        orn_attr = instancer.GetOrientationsAttr()
        pos_attr.Set(all_positions[0])
        orn_attr.Set(all_orientations[0])
        for frame_idx in range(frame_count):
            tc = Usd.TimeCode(start_frame + frame_idx)
            pos_attr.Set(all_positions[frame_idx], tc)
            orn_attr.Set(all_orientations[frame_idx], tc)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)
    print(
        f"[RBDSolver] Baked {frame_count} frames on {len(all_positions[0])} instances at {prim.GetPath()}"
    )


def _sim_prim_gen(
    stage,
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
    lin_damping=0.2,
    ang_damping=0.2,
    hull_max_verts=0,
):
    """Simulate a single prim; yields frame index per step, returns all_mats (list of Matrix4d)."""
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    rng = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
    half_extents = [0.5, 0.5, 0.5]
    z_offset = 0.0
    if not rng.IsEmpty():
        sz = rng.GetMax() - rng.GetMin()
        half_extents = [sz[0] / 2, sz[1] / 2, sz[2] / 2]
        z_offset = (rng.GetMin()[up_idx] + rng.GetMax()[up_idx]) / 2 - translate[up_idx]

    # Body CoM in world space: prim world origin shifted to bbox centre on the up axis.
    body_ctr = list(translate)
    body_ctr[up_idx] += z_offset

    mesh_pts = None
    if shape_type == "Convex Hull":
        # Walk the full prim hierarchy to collect world-space vertices — works for
        # both direct meshes and deep reference hierarchies (e.g. referenced .usdc).
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
                hull_verts.append(
                    [
                        float(_wp[0]) - body_ctr[0],
                        float(_wp[1]) - body_ctr[1],
                        float(_wp[2]) - body_ctr[2],
                    ]
                )
        if hull_verts:
            if hull_max_verts > 0 and len(hull_verts) > hull_max_verts:
                step = max(1, len(hull_verts) // hull_max_verts)
                hull_verts = hull_verts[::step][:hull_max_verts]
            mesh_pts = _np.array(hull_verts)
        else:
            print(
                "[RBDSolver] Convex Hull: no mesh data found in hierarchy, falling back to Box"
            )

    p = _pb
    p.connect(p.DIRECT)
    p.setAdditionalSearchPath(_pb_data.getDataPath())
    gravity_vec = [0, 0, 0]
    gravity_vec[up_idx] = gravity
    p.setGravity(*gravity_vec)
    p.setTimeStep(dt / substeps)
    p.setPhysicsEngineParameter(numSolverIterations=solver_iters)

    animated_cols = []
    if not collision_prims:
        _add_collision_body(p, stage, None, friction, restitution, up_idx)
    else:
        for cp in collision_prims:
            cbody, is_anim = _add_collision_body(
                p, stage, cp, friction, restitution, up_idx
            )
            if is_anim:
                animated_cols.append((cbody, cp))

    if orx != 0.0 or ory != 0.0 or orz != 0.0:
        rot = (
            rot
            * Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
            * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
            * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz)
        )
    quat = rot.GetQuat()
    imag = quat.GetImaginary()
    init_orn = [imag[0], imag[1], imag[2], quat.GetReal()]
    init_pos = [translate[0] + otx, translate[1] + oty, translate[2] + otz]
    init_pos[up_idx] += z_offset

    lat = [j for j in range(3) if j != up_idx]
    if shape_type == "Sphere":
        # Use lateral (non-up) extents — avoids oversized sphere when stem/height exceeds width.
        sphere_r = max(half_extents[lat[0]], half_extents[lat[1]])
        radius = max(sphere_r + collision_margin, 0.001)
        col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=radius)
        # Recompute z_offset so the sphere bottom aligns with bbox bottom, not bbox center.
        # When the apple is taller than it is wide (stem), bbox_center is 1-2cm above the
        # sphere radius, causing the apple to sit/float too high and roll out of shallow bowls.
        if not rng.IsEmpty():
            z_offset = rng.GetMin()[up_idx] + radius - translate[up_idx]
            init_pos[up_idx] = translate[up_idx] + oty + z_offset
    elif shape_type == "Convex Hull" and mesh_pts is not None:
        max_ext = max(half_extents) if max(half_extents) > 1e-6 else 1.0
        ms = (max_ext + collision_margin) / max_ext if collision_margin != 0.0 else 1.0
        col_shape = p.createCollisionShape(
            p.GEOM_MESH,
            vertices=mesh_pts,
            meshScale=[ms, ms, ms],
        )
    else:
        adj = [max(h + collision_margin, 0.001) for h in half_extents]
        col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=adj)

    body = p.createMultiBody(
        baseMass=mass,
        baseCollisionShapeIndex=col_shape,
        basePosition=init_pos,
        baseOrientation=init_orn,
    )
    p.changeDynamics(
        body,
        -1,
        lateralFriction=friction,
        restitution=restitution,
        linearDamping=lin_damping,
        angularDamping=ang_damping,
    )
    # CCD prevents fast-moving bodies tunneling through thin surfaces (e.g. bowl rim).
    lat_r = max(half_extents[lat[0]], half_extents[lat[1]])
    p.changeDynamics(body, -1, ccdSweptSphereRadius=lat_r * 0.5)
    p.resetBaseVelocity(body, linearVelocity=init_vel, angularVelocity=ang_vel)

    all_mats = []
    try:
        for frame in range(frame_count):
            for cbody, cprim in animated_cols:
                tc = Usd.TimeCode(start_frame + frame)
                kine_cache = UsdGeom.XformCache(tc)
                kine_xf = kine_cache.GetLocalToWorldTransform(cprim)
                kine_t = kine_xf.ExtractTranslation()
                kine_q = kine_xf.ExtractRotation().GetQuat()
                kine_imag = kine_q.GetImaginary()
                p.resetBasePositionAndOrientation(
                    cbody,
                    [kine_t[0], kine_t[1], kine_t[2]],
                    [kine_imag[0], kine_imag[1], kine_imag[2], kine_q.GetReal()],
                )

            pos, orn = p.getBasePositionAndOrientation(body)
            world_mat = Gf.Matrix4d()
            world_mat.SetRotate(Gf.Quatd(orn[3], orn[0], orn[1], orn[2]))
            corrected = list(pos)
            corrected[up_idx] -= z_offset
            world_mat.SetTranslateOnly(Gf.Vec3d(*corrected))
            all_mats.append(world_mat)

            for _ in range(substeps):
                p.stepSimulation()

            yield frame
    finally:
        p.disconnect()
    return all_mats


def _sim_prim_gen_mj(
    stage,
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
    lin_damping=0.0,
    ang_damping=0.05,
    hull_max_verts=0,
):
    """MuJoCo rigid body simulation — yields frame index, returns all_mats.

    Uses MjSpec to build the scene programmatically from USD mesh data (no file I/O).
    Static mesh geoms with needsdf=True give exact concave collision without VHACD.
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

    body_ctr = list(translate)
    body_ctr[up_idx] += z_offset

    # --- collect convex hull vertices if needed ---
    mesh_pts = None
    if shape_type == "Convex Hull":
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
                hull_verts.append(
                    [
                        float(_wp[0]) - body_ctr[0],
                        float(_wp[1]) - body_ctr[1],
                        float(_wp[2]) - body_ctr[2],
                    ]
                )
        if hull_verts:
            if hull_max_verts > 0 and len(hull_verts) > hull_max_verts:
                step = max(1, len(hull_verts) // hull_max_verts)
                hull_verts = hull_verts[::step][:hull_max_verts]
            mesh_pts = _np.array(hull_verts, dtype=_np.float32)
        else:
            print("[RBDSolver/MuJoCo] Convex Hull: no mesh data, falling back to Box")

    # --- build MjSpec scene ---
    spec = _mj.MjSpec()
    spec.option.timestep = dt / substeps
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters

    # --- static / kinematic collision bodies ---
    mocap_col_names = []  # (body_name, cprim) for bodies needing per-frame kinematic updates

    if not collision_prims:
        # Infinite ground plane, oriented to the scene's up axis.
        plane_body = spec.worldbody.add_body()
        plane_body.name = "ground"
        if up_idx == 1:  # Y-up: rotate plane so its normal points +Y
            import math as _math
            a = _math.pi / 4  # half-angle of -90° rotation around X
            plane_body.quat = [_math.cos(a), -_math.sin(a), 0.0, 0.0]
        pg = plane_body.add_geom()
        pg.type = _mj.mjtGeom.mjGEOM_PLANE
        pg.size = [0.0, 0.0, 0.01]
        pg.friction = [friction, 0.005, 0.0001]
    else:
        for i, cp in enumerate(collision_prims):
            xfc = UsdGeom.XformCache()
            world_xf = xfc.GetLocalToWorldTransform(cp)
            col_t = world_xf.ExtractTranslation()

            is_anim = any(
                op.GetNumTimeSamples() > 1
                for op in UsdGeom.Xformable(cp).GetOrderedXformOps()
            )

            bname = f"col_{i}"
            col_body = spec.worldbody.add_body()
            col_body.name = bname
            col_body.pos = [float(col_t[0]), float(col_t[1]), float(col_t[2])]
            if is_anim:
                # Mocap body: MuJoCo updates its pose each frame via data.mocap_pos/quat.
                col_body.mocap = True
                mocap_col_names.append((bname, cp))

            vertices, indices = _extract_mesh_data(cp)
            if vertices is not None and indices is not None:
                ma = spec.add_mesh()
                ma.name = f"mesh_{bname}"
                # needsdf=True → MuJoCo precomputes a signed-distance field for the mesh,
                # giving exact concave collision on static bodies without any decomposition.
                ma.needsdf = True
                ma.uservert = vertices.astype(_np.float32).flatten()
                ma.userface = _np.array(indices, dtype=_np.int32).flatten()
                bg = col_body.add_geom()
                bg.type = _mj.mjtGeom.mjGEOM_MESH
                bg.meshname = ma.name
                bg.friction = [friction, 0.005, 0.0001]
            else:
                # Fallback: bbox-fitted box for collision prims without direct mesh data.
                fb = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
                cr = fb.ComputeWorldBound(cp).ComputeAlignedRange()
                if not cr.IsEmpty():
                    csz = cr.GetMax() - cr.GetMin()
                    bg = col_body.add_geom()
                    bg.type = _mj.mjtGeom.mjGEOM_BOX
                    bg.size = [csz[0] / 2, csz[1] / 2, csz[2] / 2]
                    bg.friction = [friction, 0.005, 0.0001]

    # --- initial rotation ---
    if orx != 0.0 or ory != 0.0 or orz != 0.0:
        rot = (
            rot
            * Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
            * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
            * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz)
        )
    gq = rot.GetQuat()
    gi = gq.GetImaginary()
    # MuJoCo quaternion convention matches USD: [w, x, y, z]
    init_quat_mj = [gq.GetReal(), float(gi[0]), float(gi[1]), float(gi[2])]

    # --- initial position ---
    init_pos = [translate[0] + otx, translate[1] + oty, translate[2] + otz]
    init_pos[up_idx] += z_offset

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

    if shape_type == "Sphere":
        sphere_r = max(half_extents[lat[0]], half_extents[lat[1]])
        radius = max(sphere_r + collision_margin, 0.001)
        if not rng.IsEmpty():
            z_offset = rng.GetMin()[up_idx] + radius - translate[up_idx]
            init_pos[up_idx] = translate[up_idx] + oty + z_offset
            dyn_body.pos = list(init_pos)
        dyn_geom.type = _mj.mjtGeom.mjGEOM_SPHERE
        dyn_geom.size = [radius, 0.0, 0.0]

    elif shape_type == "Convex Hull" and mesh_pts is not None:
        hull_ma = spec.add_mesh()
        hull_ma.name = "hull_dyn"
        hull_ma.uservert = mesh_pts.flatten()
        # MuJoCo builds a convex hull internally from the vertex cloud —
        # maxhullvert caps the output vertex count for the collision hull.
        hull_ma.maxhullvert = max(hull_max_verts, 64) if hull_max_verts > 0 else 256
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

    print(
        f"[RBDSolver/MuJoCo] Compiled — {model.nbody} bodies, "
        f"{model.ngeom} geoms, shape={shape_type}"
    )

    # --- simulation loop ---
    all_mats = []
    for frame in range(frame_count):
        # Update kinematic (mocap) collision bodies from USD time samples
        for mid, cprim in mocap_ids:
            tc = Usd.TimeCode(start_frame + frame)
            kc = UsdGeom.XformCache(tc)
            kxf = kc.GetLocalToWorldTransform(cprim)
            kt = kxf.ExtractTranslation()
            kq = kxf.ExtractRotation().GetQuat()
            ki = kq.GetImaginary()
            data.mocap_pos[mid] = [float(kt[0]), float(kt[1]), float(kt[2])]
            data.mocap_quat[mid] = [kq.GetReal(), float(ki[0]), float(ki[1]), float(ki[2])]

        # Sample state before stepping (frame N shows where the body was at the start of step N)
        pos = data.xpos[bid]
        qmj = data.xquat[bid]  # [w, x, y, z] — same convention as USD, no reordering needed

        world_mat = Gf.Matrix4d()
        world_mat.SetRotate(
            Gf.Quatd(float(qmj[0]), float(qmj[1]), float(qmj[2]), float(qmj[3]))
        )
        corrected = list(pos)
        corrected[up_idx] -= z_offset
        world_mat.SetTranslateOnly(Gf.Vec3d(*corrected))
        all_mats.append(world_mat)

        for _ in range(substeps):
            _mj.mj_step(model, data)

        yield frame

    return all_mats


def _write_prim_results(
    stage, prim, layer, parent_world_inv, all_mats, start_frame, scale_xyz=None
):
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
            order_attr = prim.CreateAttribute(
                "xformOpOrder", Sdf.ValueTypeNames.TokenArray
            )
        order_attr.Set(["xformOp:transform"])
        xf_attr = prim.GetAttribute("xformOp:transform")
        if not xf_attr:
            xf_attr = prim.CreateAttribute(
                "xformOp:transform", Sdf.ValueTypeNames.Matrix4d
            )
        xf_attr.Set(_local(all_mats[0]))
        for frame_idx, mat in enumerate(all_mats):
            xf_attr.Set(_local(mat), Usd.TimeCode(start_frame + frame_idx))
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + len(all_mats) - 1)
    print(f"[RBDSolver] Baked {len(all_mats)} frames on {prim.GetPath()}")


# ---------------------------------------------------------------------------
# Threading / progress machinery
# ---------------------------------------------------------------------------


def _launch_sim(chunk_id, frame_count, gen_fn, write_fn, on_complete=None):
    """Drive gen_fn() frame-by-frame with a QTimer on the main thread — no threads, no GIL contention."""
    if _active_sims.get(chunk_id):
        print("[RBDSolver] Simulation already running")
        return

    gen = gen_fn()
    _active_sims[chunk_id] = gen

    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError:
        # Headless fallback — run synchronously.
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value
        if result is not None:
            write_fn(result)
        if on_complete:
            on_complete()
        _active_sims.pop(chunk_id, None)
        return

    dlg = QtWidgets.QProgressDialog(
        f"Simulating 0 / {frame_count} frames...", "Cancel", 0, frame_count
    )
    dlg.setWindowTitle("RBD Solver")
    dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    dlg.setMinimumDuration(0)
    dlg.setValue(0)
    dlg.show()
    _active_dialogs[chunk_id] = dlg

    timer = QtCore.QTimer(dlg)
    _FRAMES_PER_TICK = 4  # keeps each tick well under 16 ms for typical sims

    def tick():
        if dlg.wasCanceled():
            gen.close()  # fires GeneratorExit → finally block disconnects PyBullet
            timer.stop()
            dlg.close()
            _active_sims.pop(chunk_id, None)
            _active_dialogs.pop(chunk_id, None)
            return

        last_frame = 0
        result = None
        done = False

        for _ in range(_FRAMES_PER_TICK):
            try:
                last_frame = next(gen)
            except StopIteration as e:
                result = e.value
                done = True
                break
            except Exception:
                print(f"[RBDSolver] Simulation error:\n{_tb.format_exc()}")
                done = True
                break

        dlg.setLabelText(f"Simulating {last_frame + 1} / {frame_count} frames...")
        dlg.setValue(last_frame + 1)

        if done:
            timer.stop()
            dlg.close()
            _active_sims.pop(chunk_id, None)
            _active_dialogs.pop(chunk_id, None)
            if result is not None:
                write_fn(result)
            if on_complete:
                on_complete()

    timer.timeout.connect(tick)
    timer.start(16)


# ---------------------------------------------------------------------------
# Chunk entry point
# ---------------------------------------------------------------------------


def execute(owner, notice, sender):
    """Run RBD simulation — triggered by the Run Simulation button."""
    if not _HAS_MUJOCO and not _HAS_PYBULLET:
        print(
            "[RBDSolver] No physics backend found.\n"
            "  Install MuJoCo (recommended): pip install mujoco\n"
            "  Install PyBullet (fallback):  pip install pybullet"
        )
        return

    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    chunk_id = id(_chunk)
    if _active_sims.get(chunk_id):
        print("[RBDSolver] Simulation already running")
        return

    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        print("[RBDSolver] No target prim selected")
        return
    prim = prims[0]
    if not prim.IsValid() or prim.GetPath() == Sdf.Path("/"):
        print("[RBDSolver] Invalid prim selected (root or pseudo-root)")
        return

    collision_prims = _parse_collision_prims(
        _chunk.get_param("collision_path").value, stage
    )

    frame_count = int(_chunk.get_param("frame_count").value or 120)
    fps = float(_chunk.get_param("fps").value or 24)
    mpu = UsdGeom.GetStageMetersPerUnit(stage)
    _gravity_default = round(-9.81 / mpu) if mpu and mpu > 0 else -981
    _grav_param = _chunk.get_param("gravity")
    gravity = float(_grav_param.value or _gravity_default)
    if not _grav_param.value:
        _grav_param.value = (
            gravity  # write back so the user can see the auto-detected value
        )
    friction = float(
        v if (v := _chunk.get_param("friction").value) is not None else 0.6
    )
    restitution = float(
        v if (v := _chunk.get_param("restitution").value) is not None else 0.3
    )
    mass = float(v if (v := _chunk.get_param("mass").value) is not None else 1.0)
    lin_damping = float(
        v if (v := _chunk.get_param("lin_damping").value) is not None else 0.2
    )
    ang_damping = float(
        v if (v := _chunk.get_param("ang_damping").value) is not None else 0.2
    )
    shape_type = _chunk.get_param("shape_type").value or "Box"
    col_margin = float(_chunk.get_param("collision_margin").value or 0.0)
    hull_max_verts = int(_chunk.get_param("hull_max_verts").value or 0)
    solver_iters = int(_chunk.get_param("solver_iters").value or 10)
    start_frame = int(_chunk.get_param("start_frame").value or 1)
    substeps = max(1, int(_chunk.get_param("substeps").value or 10))
    init_vel = _parse_vec3(_chunk.get_param("initial_velocity").value or "0 0 0")
    ang_vel = _parse_vec3(_chunk.get_param("angular_velocity").value or "0 0 0")
    otx = float(_chunk.get_param("off_tx").value or 0)
    oty = float(_chunk.get_param("off_ty").value or 0)
    otz = float(_chunk.get_param("off_tz").value or 0)
    orx = float(_chunk.get_param("off_rx").value or 0)
    ory = float(_chunk.get_param("off_ry").value or 0)
    orz = float(_chunk.get_param("off_rz").value or 0)
    dt = 1.0 / fps

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    def _refresh():
        try:
            from scripts.node_graph.graph_host import GraphHost

            GraphHost.current().refresh_timeline(
                start_frame, start_frame + frame_count - 1
            )
        except Exception:
            pass

    if UsdGeom.PointInstancer(prim):
        if not _HAS_PYBULLET:
            print("[RBDSolver] PointInstancer simulation requires pybullet. Run: pip install pybullet")
            return
        _launch_sim(
            chunk_id,
            frame_count,
            lambda: _sim_instancer_gen(
                stage,
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
                col_margin,
                solver_iters,
                lin_damping,
                ang_damping,
            ),
            lambda result: _write_instancer_results(
                stage, prim, layer, result[0], result[1], start_frame
            ),
            _refresh,
        )
        return

    # --- Single-prim path ---
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    translate = world_xf.ExtractTranslation()
    rot = world_xf.ExtractRotation()

    parent = prim.GetParent()
    parent_world_inv = Gf.Matrix4d(1.0)
    if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
        parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()

    # Extract scale from the prim's local-to-parent transform (row lengths of 3×3 submatrix).
    local_xf = world_xf * parent_world_inv
    _r0 = local_xf.GetRow3(0)
    _r1 = local_xf.GetRow3(1)
    _r2 = local_xf.GetRow3(2)
    _sx = (_r0[0] ** 2 + _r0[1] ** 2 + _r0[2] ** 2) ** 0.5
    _sy = (_r1[0] ** 2 + _r1[1] ** 2 + _r1[2] ** 2) ** 0.5
    _sz = (_r2[0] ** 2 + _r2[1] ** 2 + _r2[2] ** 2) ** 0.5
    scale_xyz = (_sx, _sy, _sz)

    _sim_args = (
        stage, prim, collision_prims, frame_count, gravity,
        friction, restitution, mass, substeps, init_vel, ang_vel,
        start_frame, dt, otx, oty, otz, orx, ory, orz,
        up_idx, shape_type, col_margin, solver_iters, translate, rot,
        lin_damping, ang_damping, hull_max_verts,
    )

    if _HAS_MUJOCO:
        print("[RBDSolver] Using MuJoCo backend")

        def _gen_fn():
            return _sim_prim_gen_mj(*_sim_args)
    else:
        print("[RBDSolver] Using PyBullet backend")

        def _gen_fn():
            return _sim_prim_gen(*_sim_args)

    _launch_sim(
        chunk_id,
        frame_count,
        _gen_fn,
        lambda result: _write_prim_results(
            stage, prim, layer, parent_world_inv, result, start_frame, scale_xyz
        ),
        _refresh,
    )


def main():
    """Build the RBD Solver chunk UI — params, groups, and button callbacks."""
    chunk_api = chunk.Chunk("RBD Solver")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED
        | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Prim to simulate. Can be a single mesh prim or a PointInstancer — each instance will be simulated independently.",
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
            "Prims",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Static collision geometry — these prims stay fixed and the target prim bounces off them.",
        )
    )
    add_col_btn = chunk.Button(
        "add_collision",
        "Add Selected Prim",
        tooltip="Add the prim currently selected in the usdview hierarchy to the collision list.",
    )
    add_col_btn.add_callback(add_collision)
    col_grp.add_param(add_col_btn)
    clear_col_btn = chunk.Button(
        "clear_collision",
        "Clear",
        tooltip="Remove all prims from the collision list.",
    )
    clear_col_btn.add_callback(clear_collision)
    col_grp.add_param(clear_col_btn)
    chunk_api.add_param(col_grp)

    sim_grp = chunk.Group("Simulation", flags=_GF)
    sim_grp.add_param(
        chunk.Spinbox(
            "frame_count",
            "Frame Count",
            default_value=120,
            is_int=True,
            tooltip="Number of frames to bake. Animation is written from Start Frame to Start Frame + Frame Count.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "fps",
            "FPS",
            default_value=24.0,
            tooltip="Frames per second. Should match your stage's timeline FPS.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "gravity",
            "Gravity",
            default_value=-981.0,
            tooltip="Gravity in scene units/s² along the up axis. -981 = 9.81 m/s² for cm stages, -9.81 for meter stages. Set to 0 to auto-detect from stage metersPerUnit.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "substeps",
            "Substeps",
            default_value=10,
            is_int=True,
            tooltip="Physics steps computed per frame. More substeps = more accurate simulation at the cost of speed. 10 is good for simple falling; use 20-40 for tight stacking or fast-moving objects.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "solver_iters",
            "Solver Iterations",
            default_value=10,
            is_int=True,
            tooltip="Bullet constraint solver iterations per substep. Lower is faster but less stable. 10 works for most falling objects; increase to 30+ if objects tunnel through each other or stacking is wobbly.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "start_frame",
            "Start Frame",
            default_value=1,
            is_int=True,
            tooltip="Timeline frame at which the baked animation starts.",
        )
    )
    chunk_api.add_param(sim_grp)

    physics_grp = chunk.Group("Physics", flags=_GF)
    physics_grp.add_param(
        chunk.Spinbox(
            "friction",
            "Friction",
            default_value=0.6,
            tooltip="How much surfaces resist sliding against each other. 0 = frictionless ice, 1 = high grip. Typical dry surfaces: 0.4–0.8. Fruits/wood: ~0.6.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "restitution",
            "Bounce",
            default_value=0.3,
            tooltip="Bounciness (coefficient of restitution). 0 = no bounce, 1 = perfectly elastic. Values above 1 add energy and are unstable.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "mass",
            "Mass",
            default_value=1.0,
            tooltip="Mass of each dynamic object. Only matters relative to other dynamic objects — for objects falling under gravity alone, mass has no effect on trajectory.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "lin_damping",
            "Linear Damping",
            default_value=0.2,
            tooltip="Velocity damping (simulated drag). Helps objects settle without jittering after landing. 0 = no damping, 1 = heavily overdamped.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "ang_damping",
            "Angular Damping",
            default_value=0.2,
            tooltip="Rotational damping. Helps objects stop spinning after landing. 0 = no damping, 1 = heavily overdamped.",
        )
    )
    chunk_api.add_param(physics_grp)

    shape_grp = chunk.Group("Shape", flags=_GF)
    shape_grp.add_param(
        chunk.Menu(
            "shape_type",
            "Collision Shape",
            menu_items=_SHAPE_TYPES,
            default_value="Box",
            tooltip="Collision shape used to approximate the prim for physics.\nBox: fastest, axis-aligned bounding box.\nSphere: good for round objects like balls.\nConvex Hull: wraps the actual mesh shape — most accurate but slowest.",
        )
    )
    shape_grp.add_param(
        chunk.Spinbox(
            "collision_margin",
            "Collision Margin",
            default_value=0.0,
            tooltip="Shrink or grow the collision shape in scene units. Negative shrinks (useful when objects clip through each other). Positive adds padding around the shape.",
        )
    )
    shape_grp.add_param(
        chunk.Spinbox(
            "hull_max_verts",
            "Hull Max Verts",
            default_value=0,
            is_int=True,
            tooltip="Maximum vertices for Convex Hull collision shape. 0 = use all vertices (highest detail). Lower values give a coarser hull that's faster to compute — useful when the mesh has thousands of points.",
        )
    )
    chunk_api.add_param(shape_grp)

    offset_grp = chunk.Group("Offset Transform", flags=_GF)
    offset_grp.add_param(
        chunk.Spinbox(
            "off_tx",
            "Translate X",
            default_value=0.0,
            tooltip="Offset the starting position along X before simulation begins.",
        )
    )
    offset_grp.add_param(
        chunk.Spinbox(
            "off_ty",
            "Translate Y",
            default_value=0.0,
            tooltip="Offset the starting position along Y before simulation begins.",
        )
    )
    offset_grp.add_param(
        chunk.Spinbox(
            "off_tz",
            "Translate Z",
            default_value=0.0,
            tooltip="Offset the starting position along Z before simulation begins.",
        )
    )
    offset_grp.add_param(
        chunk.Spinbox(
            "off_rx",
            "Rotate X",
            default_value=0.0,
            tooltip="Rotate the starting orientation around X in degrees.",
        )
    )
    offset_grp.add_param(
        chunk.Spinbox(
            "off_ry",
            "Rotate Y",
            default_value=0.0,
            tooltip="Rotate the starting orientation around Y in degrees.",
        )
    )
    offset_grp.add_param(
        chunk.Spinbox(
            "off_rz",
            "Rotate Z",
            default_value=0.0,
            tooltip="Rotate the starting orientation around Z in degrees.",
        )
    )
    chunk_api.add_param(offset_grp)

    vel_grp = chunk.Group("Initial Velocity", flags=_GF)
    vel_grp.add_param(
        chunk.LineEdit(
            "initial_velocity",
            "Linear (x y z)",
            default_value="0 0 0",
            tooltip="Starting linear velocity as 'x y z' in scene units per second. Example: '0 500 0' launches the object upward.",
        )
    )
    vel_grp.add_param(
        chunk.LineEdit(
            "angular_velocity",
            "Angular (x y z)",
            default_value="0 0 0",
            tooltip="Starting angular (spin) velocity as 'x y z' in radians per second. Example: '0 3.14 0' spins around Y at ~180°/s.",
        )
    )
    chunk_api.add_param(vel_grp)

    apple_btn = chunk.Button(
        "preset_apple",
        "Preset: Apple in Bowl",
        tooltip="Load tuned settings for dropping an apple into a bowl (cm scene, Y-up).\nConvex Hull, substeps 40, low bounce, no air damping.",
    )
    apple_btn.add_callback(preset_apple)
    chunk_api.add_param(apple_btn)

    save_btn = chunk.Button(
        "save_settings",
        "Save Settings",
        tooltip="Save all simulation parameters to a JSON file via a save dialog. Does not save prim/collision paths.",
    )
    save_btn.add_callback(save_settings)
    chunk_api.add_param(save_btn)

    load_btn = chunk.Button(
        "load_settings",
        "Load Settings",
        tooltip="Load previously saved simulation parameters from a JSON file via an open dialog.",
    )
    load_btn.add_callback(load_settings)
    chunk_api.add_param(load_btn)

    run_btn = chunk.Button(
        "run_sim",
        "Run Simulation",
        tooltip="Bake the physics simulation to keyframes. Runs in the background — a progress dialog will appear.",
    )
    run_btn.add_callback(execute)
    chunk_api.add_param(run_btn)

    reset_btn = chunk.Button(
        "reset_sim",
        "Reset Simulation",
        tooltip="Remove all baked keyframes and restore the prim to its original transform.",
    )
    reset_btn.add_callback(reset)
    chunk_api.add_param(reset_btn)

    return chunk_api


def config():
    """Register this chunk under the Simulation category."""
    return chunk.DefaulConfig(category="Simulation")
