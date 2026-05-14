"""RBD Solver chunk — rigid body physics simulation via PyBullet."""

from __future__ import annotations

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

_SHAPE_TYPES = ["Box", "Sphere", "Convex Hull"]


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


def _add_collision_body(p, stage, collision_prim, friction, restitution):
    """Add a collision body to PyBullet. Returns (body_id, is_animated_kinematic)."""
    if collision_prim is None or not collision_prim.IsValid():
        ground = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
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

    sphere = UsdGeom.Sphere(collision_prim)
    if sphere.GetRadiusAttr().HasValue():
        radius = sphere.GetRadiusAttr().Get()
        scale = world_xf.GetRow3(0).GetLength()
        col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=radius * scale)
    else:
        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        coll_bbox = bbox_cache.ComputeWorldBound(collision_prim)
        coll_rng = coll_bbox.ComputeAlignedRange()
        if not coll_rng.IsEmpty():
            coll_sz = coll_rng.GetMax() - coll_rng.GetMin()
            max_lateral = max(coll_sz[0], coll_sz[1])
            if max_lateral > 0 and coll_sz[2] / max_lateral < 0.05:
                col_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
                body_pos = [0, 0, coll_rng.GetMax()[2]]

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
                list(child_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2])))
                for pt in points
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
                col_shape = p.createCollisionShape(
                    p.GEOM_MESH, vertices=vertices, indices=indices, meshScale=[1, 1, 1]
                )
                body_pos = [0, 0, 0]
                break

    if col_shape is None:
        bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
        rng = bbox_cache.ComputeWorldBound(collision_prim).ComputeAlignedRange()
        if not rng.IsEmpty():
            mn, mx = rng.GetMin(), rng.GetMax()
            sx, sy, sz = mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]
            max_lateral = max(sx, sy)
            if max_lateral > 0 and sz / max_lateral < 0.2:
                col_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
                body_pos = [0, 0, mx[2]]
            else:
                col_shape = p.createCollisionShape(
                    p.GEOM_BOX,
                    halfExtents=[max(sx / 2, 0.5), max(sy / 2, 0.5), max(sz / 2, 0.5)],
                )
                body_pos = [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2]

    if col_shape is None:
        print("[RBDSolver] Could not create collision shape, using ground plane")
        col_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
        body_pos = [0, 0, 0]

    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=col_shape, basePosition=body_pos
    )
    p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
    return body, is_animated


def execute(owner, notice, sender):
    """Run RBD simulation — triggered by the Run Simulation button."""
    if not _HAS_PYBULLET:
        print("[RBDSolver] pybullet not installed. Run: pip install pybullet")
        return

    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        print("[RBDSolver] No target prim selected")
        return
    prim = prims[0]

    collision_prims = _chunk.get_param("collision_path").get_prims()
    collision_prim = collision_prims[0] if collision_prims else None

    frame_count = int(_chunk.get_param("frame_count").value or 120)
    fps = float(_chunk.get_param("fps").value or 24)
    gravity = float(_chunk.get_param("gravity").value or -981)
    friction = float(_chunk.get_param("friction").value or 0.5)
    restitution = float(_chunk.get_param("restitution").value or 0.3)
    mass = float(_chunk.get_param("mass").value or 1.0)
    shape_type = _chunk.get_param("shape_type").value or "Box"
    drop_height = float(_chunk.get_param("drop_height").value or 50)
    start_frame = int(_chunk.get_param("start_frame").value or 1)
    substeps = max(1, int(_chunk.get_param("substeps").value or 10))
    init_vel = _parse_vec3(_chunk.get_param("initial_velocity").value or "0 0 0")
    ang_vel = _parse_vec3(_chunk.get_param("angular_velocity").value or "0 0 0")
    dt = 1.0 / fps

    layer = owner.get_edit_context()
    if not layer:
        return

    # Clear our layer first so the rest-pose world transform is unaffected by previous bake
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    translate = world_xf.ExtractTranslation()
    rot = world_xf.ExtractRotation()

    parent = prim.GetParent()
    parent_world_inv = Gf.Matrix4d(1.0)
    if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
        parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()

    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    rng = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
    half_extents = [0.5, 0.5, 0.5]
    z_offset = 0.0
    if not rng.IsEmpty():
        sz = rng.GetMax() - rng.GetMin()
        half_extents = [sz[0] / 2, sz[1] / 2, sz[2] / 2]
        z_offset = (rng.GetMin()[2] + rng.GetMax()[2]) / 2 - translate[2]

    mesh_pts = mesh_idx = None
    if shape_type == "Convex Hull":
        mesh_pts, mesh_idx = _extract_mesh_data(prim)
        if mesh_pts is None:
            print("[RBDSolver] Convex Hull: no mesh data, falling back to Box")

    p = _pb
    p.connect(p.DIRECT)
    p.setAdditionalSearchPath(_pb_data.getDataPath())
    p.setGravity(0, 0, gravity)
    p.setTimeStep(dt / substeps)

    col_body, col_animated = _add_collision_body(p, stage, collision_prim, friction, restitution)

    quat = rot.GetQuat()
    imag = quat.GetImaginary()
    init_orn = [imag[0], imag[1], imag[2], quat.GetReal()]
    init_pos = [translate[0], translate[1], translate[2] + drop_height + z_offset]

    if shape_type == "Sphere":
        col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=max(half_extents))
    elif shape_type == "Convex Hull" and mesh_pts is not None:
        kwargs = {"meshScale": [1.0, 1.0, 1.0], "vertices": mesh_pts}
        if mesh_idx is not None:
            kwargs["indices"] = mesh_idx
        col_shape = p.createCollisionShape(p.GEOM_MESH, **kwargs)
    else:
        col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=half_extents)

    body = p.createMultiBody(
        baseMass=mass,
        baseCollisionShapeIndex=col_shape,
        basePosition=init_pos,
        baseOrientation=init_orn,
    )
    p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
    p.resetBaseVelocity(body, linearVelocity=init_vel, angularVelocity=ang_vel)

    all_mats = []
    for frame in range(frame_count):
        if col_animated and collision_prim is not None:
            tc = Usd.TimeCode(start_frame + frame)
            kine_cache = UsdGeom.XformCache(tc)
            kine_xf = kine_cache.GetLocalToWorldTransform(collision_prim)
            kine_t = kine_xf.ExtractTranslation()
            kine_q = kine_xf.ExtractRotation().GetQuat()
            kine_imag = kine_q.GetImaginary()
            p.resetBasePositionAndOrientation(
                col_body,
                [kine_t[0], kine_t[1], kine_t[2]],
                [kine_imag[0], kine_imag[1], kine_imag[2], kine_q.GetReal()],
            )

        pos, orn = p.getBasePositionAndOrientation(body)
        world_mat = Gf.Matrix4d()
        world_mat.SetRotate(Gf.Quatd(orn[3], orn[0], orn[1], orn[2]))
        world_mat.SetTranslateOnly(Gf.Vec3d(pos[0], pos[1], pos[2] - z_offset))
        all_mats.append(world_mat * parent_world_inv)

        for _ in range(substeps):
            p.stepSimulation()

    p.disconnect()

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

        xf_attr.Set(all_mats[0])  # default value (shown outside sim range)
        for frame_idx, mat in enumerate(all_mats):
            xf_attr.Set(mat, Usd.TimeCode(start_frame + frame_idx))

    stage.SetStartTimeCode(start_frame)
    stage.SetEndTimeCode(start_frame + frame_count - 1)

    try:
        from scripts.node_graph.graph_host import GraphHost

        GraphHost.current().refresh_timeline(start_frame, start_frame + frame_count - 1)
    except Exception:
        pass

    print(f"[RBDSolver] Baked {frame_count} frames on {prim.GetPath()}")


def main():
    chunk_api = chunk.Chunk("RBD Solver")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)

    collision_path = chunk.LineEdit(
        "collision_path",
        "Collision Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(collision_path)

    sim_grp = chunk.Group("Simulation")
    sim_grp.add_param(chunk.Spinbox("frame_count", "Frame Count", default_value=120, is_int=True))
    sim_grp.add_param(chunk.Spinbox("fps", "FPS", default_value=24.0))
    sim_grp.add_param(chunk.Spinbox("gravity", "Gravity", default_value=-981.0))
    sim_grp.add_param(chunk.Spinbox("substeps", "Substeps", default_value=10, is_int=True))
    sim_grp.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True))
    chunk_api.add_param(sim_grp)

    physics_grp = chunk.Group("Physics")
    physics_grp.add_param(chunk.Spinbox("friction", "Friction", default_value=0.5))
    physics_grp.add_param(chunk.Spinbox("restitution", "Bounce", default_value=0.3))
    physics_grp.add_param(chunk.Spinbox("mass", "Mass", default_value=1.0))
    chunk_api.add_param(physics_grp)

    shape_grp = chunk.Group("Shape")
    shape_grp.add_param(
        chunk.Menu(
            "shape_type",
            "Collision Shape",
            menu_items=_SHAPE_TYPES,
            default_value="Box",
        )
    )
    shape_grp.add_param(chunk.Spinbox("drop_height", "Drop Height", default_value=50.0))
    chunk_api.add_param(shape_grp)

    vel_grp = chunk.Group("Initial Velocity")
    vel_grp.add_param(
        chunk.LineEdit("initial_velocity", "Linear (x y z)", default_value="0 0 0")
    )
    vel_grp.add_param(
        chunk.LineEdit("angular_velocity", "Angular (x y z)", default_value="0 0 0")
    )
    chunk_api.add_param(vel_grp)

    run_btn = chunk.Button("run_sim", "Run Simulation")
    run_btn.add_callback(execute)
    chunk_api.add_param(run_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Simulation")
