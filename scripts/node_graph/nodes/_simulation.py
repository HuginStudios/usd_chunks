"""Physics simulation nodes using PyBullet."""

from __future__ import annotations

import random

import numpy as _np
from NodeGraphQt.widgets.node_widgets import NodeBaseWidget
from PySide6 import QtCore, QtWidgets
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

try:
    import pybullet as _pybullet
    import pybullet_data as _pybullet_data

    _HAS_PYBULLET = True
except ImportError:
    _HAS_PYBULLET = False

try:
    import noise as _noise

    _HAS_NOISE = True
except ImportError:
    _HAS_NOISE = False

from ._base import UsdNodeBase, PORT_COLORS


class RBDSolverNode(UsdNodeBase):
    """Rigid Body Dynamics solver using PyBullet.

    Takes a PointInstancer (from Scatter Points) and simulates gravity + collisions.
    Collision geometry can be plugged in as separate prim inputs.
    Bakes the result as time-sampled positions/orientations on the instancer.
    """

    NODE_NAME = "RBD Solver"
    CATEGORY = "Simulation"

    _SHAPE_TYPES = ["Box", "Sphere", "Convex Hull"]

    def __init__(self) -> None:
        super().__init__()
        self.add_input("instancer", color=PORT_COLORS["prim"])
        self.add_input("collision", multi_input=True, color=PORT_COLORS["prim"])
        self.add_input("forces", multi_input=True, color=PORT_COLORS["data"])
        self.add_text_input("frames", "Frame Count", text="120")
        self.add_text_input("fps", "FPS", text="24")
        self.add_text_input("gravity", "Gravity", text="-981")
        self.add_text_input("friction", "Friction", text="0.5")
        self.add_text_input("restitution", "Bounce", text="0.3")
        self.add_text_input("mass", "Mass", text="1.0")
        self.add_combo_menu("shape_type", "Collision Shape", items=self._SHAPE_TYPES)
        self.add_text_input("drop_height", "Drop Height", text="50")
        self.add_text_input("start_frame", "Start Frame", text="1")
        self.add_text_input("substeps", "Substeps", text="10")
        self.add_text_input("initial_velocity", "Initial Velocity", text="0 0 0")
        self.add_text_input("angular_velocity", "Angular Velocity", text="0 0 0")
        self.add_text_input("mass_variance", "Mass Variance", text="0.0")
        self.add_text_input("friction_variance", "Friction Variance", text="0.0")
        self.add_text_input("restitution_variance", "Bounce Variance", text="0.0")
        self.add_text_input("variance_seed", "Variance Seed", text="42")
        self.add_output("prim", color=PORT_COLORS["prim"])
        self.add_output("layer", color=PORT_COLORS["layer"])

    def execute(self, stage) -> dict:
        if not _HAS_PYBULLET:
            print("[RBDSolver] pybullet not installed. Run: pip install pybullet")
            return {"prim": None, "layer": None}

        # Only run if the instancer port has a connected input with data
        inst_port = self.get_input("instancer")
        if not inst_port or not inst_port.connected_ports():
            # Clean up previous edits
            self._cleanup_previous_edits(stage)
            return {"prim": None, "layer": None}

        p = _pybullet
        pybullet_data = _pybullet_data

        input_prim = self._get_input_value("instancer")
        collision_prims = self._get_all_collision_inputs()
        force_dicts = self._get_all_force_inputs()

        if not input_prim or not input_prim.IsValid():
            print("[RBDSolver] No input prim")
            return {"prim": None, "layer": None}

        # Read params
        frame_count = int(self.get_property("frames") or 120)
        fps = float(self.get_property("fps") or 24)
        gravity = float(self.get_property("gravity") or -9.81)
        friction = float(self.get_property("friction") or 0.5)
        restitution = float(self.get_property("restitution") or 0.3)
        mass = float(self.get_property("mass") or 1.0)
        shape_type = self.get_property("shape_type") or "Box"
        drop_height = float(self.get_property("drop_height") or 50)
        start_frame = int(self.get_property("start_frame") or 1)
        substeps = max(1, int(self.get_property("substeps") or 4))
        init_vel = self._parse_vec3(self.get_property("initial_velocity") or "0 0 0")
        ang_vel = self._parse_vec3(self.get_property("angular_velocity") or "0 0 0")
        mass_var = float(self.get_property("mass_variance") or 0.0)
        friction_var = float(self.get_property("friction_variance") or 0.0)
        restitution_var = float(self.get_property("restitution_variance") or 0.0)
        var_seed = int(self.get_property("variance_seed") or 42)
        dt = 1.0 / fps

        # Write sim results to the session layer
        session = stage.GetSessionLayer()

        # Detect if input is a PointInstancer or a regular prim
        instancer = UsdGeom.PointInstancer(input_prim)
        is_instancer = bool(instancer.GetPositionsAttr().Get())

        with Usd.EditContext(stage, session):
            if is_instancer:
                self._sim_instancer(
                    p,
                    pybullet_data,
                    stage,
                    instancer,
                    collision_prims,
                    frame_count,
                    fps,
                    gravity,
                    friction,
                    restitution,
                    mass,
                    shape_type,
                    drop_height,
                    start_frame,
                    dt,
                    substeps,
                    init_vel,
                    ang_vel,
                    mass_var,
                    friction_var,
                    restitution_var,
                    var_seed,
                    force_dicts,
                )
            else:
                self._sim_single_prim(
                    p,
                    pybullet_data,
                    stage,
                    input_prim,
                    collision_prims,
                    frame_count,
                    fps,
                    gravity,
                    friction,
                    restitution,
                    mass,
                    shape_type,
                    drop_height,
                    start_frame,
                    dt,
                    substeps,
                    init_vel,
                    ang_vel,
                    mass_var,
                    friction_var,
                    restitution_var,
                    var_seed,
                    force_dicts,
                )

        # Set stage time range
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)

        # Refresh usdview timeline so playback works
        try:
            from ..graph_host import GraphHost

            GraphHost.current().refresh_timeline(
                start_frame, start_frame + frame_count - 1
            )
        except Exception:
            pass

        print(f"[RBDSolver] Baked {frame_count} frames")
        return {"prim": input_prim, "layer": session}

    def _sim_instancer(
        self,
        p,
        pybullet_data,
        stage,
        instancer,
        collision_prims,
        frame_count,
        fps,
        gravity,
        friction,
        restitution,
        mass,
        shape_type,
        drop_height,
        start_frame,
        dt,
        substeps,
        init_vel,
        ang_vel,
        mass_var,
        friction_var,
        restitution_var,
        var_seed,
        force_dicts=None,
    ) -> None:
        """Simulate a PointInstancer — multiple rigid bodies."""
        positions = instancer.GetPositionsAttr().Get()
        orientations = instancer.GetOrientationsAttr().Get()
        scales = instancer.GetScalesAttr().Get()

        num_instances = len(positions)
        if not orientations:
            orientations = [Gf.Quath(1, 0, 0, 0)] * num_instances
        if not scales:
            scales = [Gf.Vec3f(1, 1, 1)] * num_instances

        # Get prototype bbox for collision shape
        half_extents = [0.5, 0.5, 0.5]
        proto_mesh_pts = None
        proto_mesh_idx = None
        proto_targets = instancer.GetPrototypesRel().GetTargets()
        if proto_targets:
            proto_prim = stage.GetPrimAtPath(proto_targets[0])
            if proto_prim.IsValid():
                bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
                bbox = bbox_cache.ComputeLocalBound(proto_prim)
                rng = bbox.ComputeAlignedRange()
                if not rng.IsEmpty():
                    sz = rng.GetMax() - rng.GetMin()
                    half_extents = [sz[0] / 2, sz[1] / 2, sz[2] / 2]
                if shape_type == "Convex Hull":
                    proto_mesh_pts, proto_mesh_idx = _extract_mesh_data(proto_prim)
        if shape_type == "Convex Hull" and proto_mesh_pts is None:
            print(
                "[RBDSolver] Convex Hull: no mesh points in prototype, falling back to Box"
            )

        # Setup PyBullet — substep timestep so N steps == 1 frame
        p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, gravity)
        p.setTimeStep(dt / substeps)
        kinematic_bodies = self._add_collision_geometry(
            p, stage, collision_prims, friction, restitution, start_frame
        )

        # Per-body variance RNG — seeded for determinism
        rng_inst = random.Random(var_seed)

        # Create rigid bodies
        bodies = []
        for i in range(num_instances):
            pos = [positions[i][0], positions[i][1], positions[i][2] + drop_height]
            q = orientations[i]
            orn = [
                q.GetImaginary()[0],
                q.GetImaginary()[1],
                q.GetImaginary()[2],
                q.GetReal(),
            ]
            sc = scales[i]
            he = [
                half_extents[0] * sc[0],
                half_extents[1] * sc[1],
                half_extents[2] * sc[2],
            ]

            body_mass = mass * _variance_multiplier(rng_inst, mass_var)
            body_friction = friction * _variance_multiplier(rng_inst, friction_var)
            body_restitution = restitution * _variance_multiplier(
                rng_inst, restitution_var
            )

            if shape_type == "Sphere":
                col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=max(he))
            elif shape_type == "Convex Hull" and proto_mesh_pts is not None:
                kwargs = {
                    "meshScale": [sc[0], sc[1], sc[2]],
                    "vertices": proto_mesh_pts,
                }
                if proto_mesh_idx is not None:
                    kwargs["indices"] = proto_mesh_idx
                col_shape = p.createCollisionShape(p.GEOM_MESH, **kwargs)
            else:
                col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=he)

            body = p.createMultiBody(
                baseMass=body_mass,
                baseCollisionShapeIndex=col_shape,
                basePosition=pos,
                baseOrientation=orn,
            )
            p.changeDynamics(
                body, -1, lateralFriction=body_friction, restitution=body_restitution
            )
            p.resetBaseVelocity(body, linearVelocity=init_vel, angularVelocity=ang_vel)
            bodies.append(body)

        force_dicts = force_dicts or []

        # Simulate — substeps steps per frame
        all_positions = []
        all_orientations = []
        for frame in range(frame_count):
            # Update kinematic collision bodies from their USD animation
            self._update_kinematic_bodies(p, kinematic_bodies, start_frame + frame)
            for sub in range(substeps):
                step = frame * substeps + sub
                for i, body in enumerate(bodies):
                    _apply_forces(p, body, i, step, force_dicts)
                p.stepSimulation()
            frame_pos = []
            frame_orn = []
            for body in bodies:
                pos, orn = p.getBasePositionAndOrientation(body)
                frame_pos.append(Gf.Vec3f(pos[0], pos[1], pos[2]))
                frame_orn.append(Gf.Quath(orn[3], orn[0], orn[1], orn[2]))
            all_positions.append(frame_pos)
            all_orientations.append(frame_orn)
        p.disconnect()

        # Write time samples
        pos_attr = instancer.GetPositionsAttr()
        orn_attr = instancer.GetOrientationsAttr()
        for frame_idx in range(frame_count):
            tc = Usd.TimeCode(start_frame + frame_idx)
            pos_attr.Set(Vt.Vec3fArray(all_positions[frame_idx]), tc)
            orn_attr.Set(Vt.QuathArray(all_orientations[frame_idx]), tc)

    def _sim_single_prim(
        self,
        p,
        pybullet_data,
        stage,
        prim,
        collision_prims,
        frame_count,
        fps,
        gravity,
        friction,
        restitution,
        mass,
        shape_type,
        drop_height,
        start_frame,
        dt,
        substeps,
        init_vel,
        ang_vel,
        mass_var,
        friction_var,
        restitution_var,
        var_seed,
        force_dicts=None,
    ) -> None:
        """Simulate a single prim as one rigid body — writes time-sampled xform."""
        # Clear only our previous sim time samples (xformOp:transform)
        # but leave other xform ops (from SetTransform etc.) intact for reading
        session = stage.GetSessionLayer()
        prim_path = prim.GetPath()
        prim_spec = session.GetPrimAtPath(prim_path)
        if prim_spec:
            for prop_name in ["xformOp:transform", "xformOpOrder"]:
                if session.GetPropertyAtPath(prim_path.AppendProperty(prop_name)):
                    prim_spec.RemoveProperty(prim_spec.properties[prop_name])

        # Read the prim's composed world transform (includes SetTransform, parent hierarchy)
        # Use a fresh XformCache at default time to get the non-animated pose
        xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        world_xf = xform_cache.GetLocalToWorldTransform(prim)
        translate = world_xf.ExtractTranslation()
        rot = world_xf.ExtractRotation()

        # Get parent's world transform for local-space conversion
        parent = prim.GetParent()
        parent_world_inv = Gf.Matrix4d(1.0)
        if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
            parent_xf = xform_cache.GetLocalToWorldTransform(parent)
            parent_world_inv = parent_xf.GetInverse()

        # Start from composed position, offset by drop_height
        init_pos = [translate[0], translate[1], translate[2] + drop_height]

        # Convert rotation to quaternion for PyBullet (xyzw)
        quat = rot.GetQuat()
        real = quat.GetReal()
        imag = quat.GetImaginary()
        init_orn = [imag[0], imag[1], imag[2], real]

        # Get bbox for collision shape size and vertical offset
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]
        )
        bbox = bbox_cache.ComputeWorldBound(prim)
        rng = bbox.ComputeAlignedRange()
        half_extents = [0.5, 0.5, 0.5]
        # Vertical offset: how far the bbox center is above the prim origin
        z_offset = 0.0
        if not rng.IsEmpty():
            sz = rng.GetMax() - rng.GetMin()
            half_extents = [sz[0] / 2, sz[1] / 2, sz[2] / 2]
            bbox_center_z = (rng.GetMin()[2] + rng.GetMax()[2]) / 2
            z_offset = (
                bbox_center_z - translate[2]
            )  # offset from prim origin (without drop_height)

        print(
            f"[RBDSolver] Prim init: world_pos={init_pos}, half_extents={half_extents}"
        )

        # Per-body variance (single body — deterministic from seed)
        rng_inst = random.Random(var_seed)
        body_mass = mass * _variance_multiplier(rng_inst, mass_var)
        body_friction = friction * _variance_multiplier(rng_inst, friction_var)
        body_restitution = restitution * _variance_multiplier(rng_inst, restitution_var)

        # Extract mesh data for convex hull before starting PyBullet
        single_mesh_pts = None
        single_mesh_idx = None
        if shape_type == "Convex Hull":
            single_mesh_pts, single_mesh_idx = _extract_mesh_data(prim)
            if single_mesh_pts is None:
                print(
                    "[RBDSolver] Convex Hull: no mesh points found, falling back to Box"
                )

        # Setup PyBullet — substep timestep so N steps == 1 frame
        p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, gravity)
        p.setTimeStep(dt / substeps)
        kinematic_bodies = self._add_collision_geometry(
            p, stage, collision_prims, friction, restitution, start_frame
        )

        # Create rigid body
        if shape_type == "Sphere":
            col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=max(half_extents))
        elif shape_type == "Convex Hull" and single_mesh_pts is not None:
            kwargs = {"meshScale": [1.0, 1.0, 1.0], "vertices": single_mesh_pts}
            if single_mesh_idx is not None:
                kwargs["indices"] = single_mesh_idx
            col_shape = p.createCollisionShape(p.GEOM_MESH, **kwargs)
        else:
            col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=half_extents)

        body = p.createMultiBody(
            baseMass=body_mass,
            baseCollisionShapeIndex=col_shape,
            basePosition=[init_pos[0], init_pos[1], init_pos[2] + z_offset],
            baseOrientation=init_orn,
        )
        p.changeDynamics(
            body, -1, lateralFriction=body_friction, restitution=body_restitution
        )
        p.resetBaseVelocity(body, linearVelocity=init_vel, angularVelocity=ang_vel)

        force_dicts = force_dicts or []

        # Simulate and write xform per frame
        # Author transform directly on the prim's xformOp:transform attribute
        xformable = UsdGeom.Xformable(prim)
        # Track which prim we're editing for cleanup on disconnect
        self._last_sim_prim_path = prim.GetPath()

        # Override xformOpOrder to just our single transform op
        order_attr = prim.GetAttribute("xformOpOrder")
        if not order_attr:
            order_attr = prim.CreateAttribute(
                "xformOpOrder", Sdf.ValueTypeNames.TokenArray
            )
        order_attr.Set(["xformOp:transform"])
        # Get or create the transform attribute
        xf_attr = prim.GetAttribute("xformOp:transform")
        if not xf_attr:
            xf_attr = prim.CreateAttribute(
                "xformOp:transform", Sdf.ValueTypeNames.Matrix4d
            )

        # Write initial position as default value (shown when not on a time-sampled frame)
        init_body_pos, init_body_orn = p.getBasePositionAndOrientation(body)
        init_world_mat = Gf.Matrix4d()
        init_world_mat.SetRotate(
            Gf.Quatd(
                init_body_orn[3], init_body_orn[0], init_body_orn[1], init_body_orn[2]
            )
        )
        init_world_mat.SetTranslateOnly(
            Gf.Vec3d(init_body_pos[0], init_body_pos[1], init_body_pos[2] - z_offset)
        )
        init_local_mat = init_world_mat * parent_world_inv
        xf_attr.Set(init_local_mat)  # default value (no time code)

        for frame in range(frame_count):
            # Write position BEFORE stepping (frame 0 = initial position)
            pos, orn = p.getBasePositionAndOrientation(body)
            world_mat = Gf.Matrix4d()
            world_mat.SetRotate(Gf.Quatd(orn[3], orn[0], orn[1], orn[2]))
            world_mat.SetTranslateOnly(Gf.Vec3d(pos[0], pos[1], pos[2] - z_offset))
            local_mat = world_mat * parent_world_inv
            xf_attr.Set(local_mat, Usd.TimeCode(start_frame + frame))

            # Then step physics for next frame
            self._update_kinematic_bodies(p, kinematic_bodies, start_frame + frame)
            for sub in range(substeps):
                step = frame * substeps + sub
                _apply_forces(p, body, 0, step, force_dicts)
                p.stepSimulation()

        p.disconnect()

    def _cleanup_previous_edits(self, stage) -> None:
        """Remove solver's xform edits from session layer when disconnected."""
        if not hasattr(self, "_last_sim_prim_path") or not self._last_sim_prim_path:
            return
        session = stage.GetSessionLayer()
        prim_spec = session.GetPrimAtPath(self._last_sim_prim_path)
        if prim_spec:
            for prop_name in ["xformOp:transform", "xformOpOrder"]:
                if session.GetPropertyAtPath(
                    self._last_sim_prim_path.AppendProperty(prop_name)
                ):
                    prim_spec.RemoveProperty(prim_spec.properties[prop_name])
        self._last_sim_prim_path = None

    def _get_all_force_inputs(self) -> list:
        """Get all force dicts connected to the multi-input forces port."""
        port = self.get_input("forces")
        if not port:
            return []
        force_dicts = []
        for src_port in port.connected_ports():
            src_node = src_port.node()
            if hasattr(src_node, "_output_cache"):
                val = src_node._output_cache.get(src_port.name())
                if isinstance(val, dict):
                    force_dicts.append(val)
        return force_dicts

    def _get_all_collision_inputs(self) -> list:
        """Get all prims connected to the multi-input collision port."""
        port = self.get_input("collision")
        if not port:
            return []
        prims = []
        for src_port in port.connected_ports():
            src_node = src_port.node()
            if hasattr(src_node, "_output_cache"):
                val = src_node._output_cache.get(src_port.name())
                if val is not None:
                    if isinstance(val, list):
                        prims.extend(val)
                    else:
                        prims.append(val)
        return [p for p in prims if p is not None and p.IsValid()]

    def _add_collision_geometry(
        self, p, stage, collision_prims, friction, restitution, start_frame=1
    ) -> list:
        """Add collision geometry from collision prim inputs.

        Detects prim type and uses appropriate PyBullet shape.
        If a prim has time-sampled transforms, it becomes kinematic (animated).
        Returns list of (body_id, prim) for kinematic bodies that need per-frame updates.
        """
        kinematic_bodies = []

        if not collision_prims:
            ground_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
            p.createMultiBody(
                baseMass=0, baseCollisionShapeIndex=ground_shape, basePosition=[0, 0, 0]
            )
            return kinematic_bodies

        for collision_prim in collision_prims:
            xform_cache = UsdGeom.XformCache()
            world_xf = xform_cache.GetLocalToWorldTransform(collision_prim)
            translate = world_xf.ExtractTranslation()
            print(
                f"[RBDSolver] Collision prim: {collision_prim.GetPath()}, world pos: {translate}"
            )

            # Detect if prim has animated transform (time samples)
            is_animated = False
            xformable = UsdGeom.Xformable(collision_prim)
            for op in xformable.GetOrderedXformOps():
                if op.GetNumTimeSamples() > 1:
                    is_animated = True
                    break

            # Build collision shape based on prim type
            col_shape = None
            body_pos = [translate[0], translate[1], translate[2]]

            # Check if it's a Sphere
            sphere = UsdGeom.Sphere(collision_prim)
            if sphere.GetRadiusAttr().HasValue():
                radius = sphere.GetRadiusAttr().Get()
                scale = world_xf.GetRow3(0).GetLength()
                col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=radius * scale)
            else:
                # Check bbox — if very flat, use an infinite plane (most reliable)
                bbox_cache = UsdGeom.BBoxCache(
                    Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]
                )
                coll_bbox = bbox_cache.ComputeWorldBound(collision_prim)
                coll_rng = coll_bbox.ComputeAlignedRange()
                if not coll_rng.IsEmpty():
                    coll_sz = coll_rng.GetMax() - coll_rng.GetMin()
                    coll_top = coll_rng.GetMax()[2]
                    # If the prim is very flat (height < 5% of width), treat as ground plane
                    max_lateral = max(coll_sz[0], coll_sz[1])
                    if max_lateral > 0 and coll_sz[2] / max_lateral < 0.05:
                        col_shape = p.createCollisionShape(
                            p.GEOM_PLANE, planeNormal=[0, 0, 1]
                        )
                        body_pos = [0, 0, coll_top]  # plane at the top surface

            if col_shape is None:
                # Check if it's a Mesh — traverse prim and children
                mesh_found = False
                for child in Usd.PrimRange(collision_prim):
                    mesh = UsdGeom.Mesh(child)
                    points = mesh.GetPointsAttr().Get()
                    if not points:
                        continue
                    face_counts = mesh.GetFaceVertexCountsAttr().Get()
                    face_indices = mesh.GetFaceVertexIndicesAttr().Get()
                    if not face_counts or not face_indices:
                        continue
                    # Get this child's world transform
                    child_xf = xform_cache.GetLocalToWorldTransform(child)
                    vertices = []
                    for pt in points:
                        wp = child_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
                        vertices.append([wp[0], wp[1], wp[2]])
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
                            p.GEOM_MESH,
                            vertices=vertices,
                            indices=indices,
                            meshScale=[1, 1, 1],
                        )
                        body_pos = [0, 0, 0]  # verts already in world space
                        mesh_found = True
                        break  # use first mesh found

            # Fallback: bounding box
            if col_shape is None:
                bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
                bbox = bbox_cache.ComputeWorldBound(collision_prim)
                rng = bbox.ComputeAlignedRange()
                if not rng.IsEmpty():
                    mn = rng.GetMin()
                    mx = rng.GetMax()
                    sx = mx[0] - mn[0]
                    sy = mx[1] - mn[1]
                    sz = mx[2] - mn[2]
                    max_lateral = max(sx, sy)
                    # Flat object → infinite plane at top surface
                    if max_lateral > 0 and sz / max_lateral < 0.2:
                        col_shape = p.createCollisionShape(
                            p.GEOM_PLANE, planeNormal=[0, 0, 1]
                        )
                        body_pos = [0, 0, mx[2]]
                    else:
                        cx = (mn[0] + mx[0]) / 2
                        cy = (mn[1] + mx[1]) / 2
                        cz = (mn[2] + mx[2]) / 2
                        hx = max(sx / 2, 0.5)
                        hy = max(sy / 2, 0.5)
                        hz = max(sz / 2, 0.5)
                        col_shape = p.createCollisionShape(
                            p.GEOM_BOX, halfExtents=[hx, hy, hz]
                        )
                        body_pos = [cx, cy, cz]

            if col_shape is None:
                continue

            print(f"[RBDSolver] Collision body at {body_pos}, animated={is_animated}")
            body = p.createMultiBody(
                baseMass=0,
                baseCollisionShapeIndex=col_shape,
                basePosition=body_pos,
            )
            p.changeDynamics(
                body, -1, lateralFriction=friction, restitution=restitution
            )

            if is_animated:
                # Mark as kinematic so we can move it each frame
                p.changeDynamics(body, -1, mass=0)
                p.setCollisionFilterGroupMask(body, -1, 1, 1)
                kinematic_bodies.append((body, collision_prim))

        return kinematic_bodies

    def _update_kinematic_bodies(self, p, kinematic_bodies, frame) -> None:
        """Update kinematic collision bodies to their USD transform at the given frame."""
        if not kinematic_bodies:
            return
        tc = Usd.TimeCode(frame)
        for body_id, prim in kinematic_bodies:
            xform_cache = UsdGeom.XformCache(tc)
            world_xf = xform_cache.GetLocalToWorldTransform(prim)
            translate = world_xf.ExtractTranslation()
            rot = world_xf.ExtractRotation()
            quat = rot.GetQuat()
            imag = quat.GetImaginary()
            pos = [translate[0], translate[1], translate[2]]
            orn = [imag[0], imag[1], imag[2], quat.GetReal()]
            p.resetBasePositionAndOrientation(body_id, pos, orn)

    @staticmethod
    def _parse_vec3(s: str) -> list:
        parts = s.split()
        if len(parts) >= 3:
            return [float(parts[0]), float(parts[1]), float(parts[2])]
        return [0.0, 0.0, 0.0]


class _PrimPathsWidget(NodeBaseWidget):
    """Multi-line text area for prim paths — one per line."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, name="prim_paths", label="")
        self._editor = QtWidgets.QPlainTextEdit()
        self._editor.setPlaceholderText("/World/Floor\n/World/WallA")
        self._editor.setFixedHeight(80)
        self._editor.setStyleSheet(
            "background: #181828; color: #c0c8e8; font: 8pt Consolas; "
            "border: 1px solid #3a3a5a; border-radius: 2px;"
        )
        self._editor.setTitleAlign = lambda *a: None
        self._editor.textChanged.connect(self.on_value_changed)
        self.setWidget(self._editor)

    def get_value(self) -> str:
        return self._editor.toPlainText()

    def set_value(self, text: str) -> None:
        self._editor.blockSignals(True)
        self._editor.setPlainText(text or "")
        self._editor.blockSignals(False)


class CollisionSetNode(UsdNodeBase):
    """Specifies multiple collision prims, outputting list[Usd.Prim] for RBDSolverNode."""

    NODE_NAME = "Collision Set"
    CATEGORY = "Simulation"

    def __init__(self) -> None:
        super().__init__()
        self._paths_widget = _PrimPathsWidget(self.view)
        self.add_custom_widget(self._paths_widget)
        self.add_output("prims", color=PORT_COLORS["prim"])
        QtCore.QTimer.singleShot(0, lambda: self._on_resize(260))

    def append_prim_path(self, path: str) -> None:
        """Append a prim path line (called by the graph drop handler)."""
        current = (self.get_property("prim_paths") or "").strip()
        self.set_property("prim_paths", (current + "\n" + path) if current else path)

    def execute(self, stage) -> dict:
        text = self.get_property("prim_paths") or ""
        prims = []
        for line in text.splitlines():
            path = line.strip()
            if not path:
                continue
            prim = stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                prims.append(prim)
            else:
                print(f"[CollisionSet] Invalid or non-existent path {path!r}, skipping")
        return {"prims": prims}


class WindForceNode(UsdNodeBase):
    """Applies a constant directional wind force to simulated bodies."""

    NODE_NAME = "Wind Force"
    CATEGORY = "Simulation"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("wind_x", "Wind X", text="1.0")
        self.add_text_input("wind_y", "Wind Y", text="0.0")
        self.add_text_input("wind_z", "Wind Z", text="0.0")
        self.add_text_input("strength", "Strength", text="10.0")
        self.add_output("force", color=PORT_COLORS["data"])

    def execute(self, stage) -> dict:
        return {
            "force": {
                "type": "wind",
                "x": float(self.get_property("wind_x") or 1.0),
                "y": float(self.get_property("wind_y") or 0.0),
                "z": float(self.get_property("wind_z") or 0.0),
                "strength": float(self.get_property("strength") or 10.0),
            }
        }


class DragForceNode(UsdNodeBase):
    """Applies linear air resistance opposing velocity."""

    NODE_NAME = "Drag Force"
    CATEGORY = "Simulation"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("coefficient", "Coefficient", text="0.5")
        self.add_output("force", color=PORT_COLORS["data"])

    def execute(self, stage) -> dict:
        return {
            "force": {
                "type": "drag",
                "coefficient": float(self.get_property("coefficient") or 0.5),
            }
        }


class TurbulenceForceNode(UsdNodeBase):
    """Applies smoothly-varying random forces using Perlin noise."""

    NODE_NAME = "Turbulence Force"
    CATEGORY = "Simulation"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("strength", "Strength", text="5.0")
        self.add_text_input("frequency", "Frequency", text="1.0")
        self.add_text_input("seed", "Seed", text="42")
        self.add_output("force", color=PORT_COLORS["data"])

    def execute(self, stage) -> dict:
        if not _HAS_NOISE:
            print(
                "[TurbulenceForce] 'noise' package not installed, using numpy fallback"
            )
        return {
            "force": {
                "type": "turbulence",
                "strength": float(self.get_property("strength") or 5.0),
                "frequency": float(self.get_property("frequency") or 1.0),
                "seed": int(self.get_property("seed") or 42),
            }
        }


def _apply_forces(p, body: int, body_index: int, step: int, force_dicts: list) -> None:
    """Apply all force dicts to a single body for one simulation substep."""
    for fd in force_dicts:
        ftype = fd.get("type")
        if ftype == "wind":
            strength = fd.get("strength", 1.0)
            fx = fd.get("x", 0.0) * strength
            fy = fd.get("y", 0.0) * strength
            fz = fd.get("z", 0.0) * strength
            p.applyExternalForce(body, -1, [fx, fy, fz], [0, 0, 0], p.WORLD_FRAME)
        elif ftype == "drag":
            coeff = fd.get("coefficient", 0.5)
            vel, _ = p.getBaseVelocity(body)
            drag = [-coeff * v for v in vel]
            p.applyExternalForce(body, -1, drag, [0, 0, 0], p.WORLD_FRAME)
        elif ftype == "turbulence":
            strength = fd.get("strength", 5.0)
            frequency = fd.get("frequency", 1.0)
            seed = fd.get("seed", 42)
            if _HAS_NOISE:
                t = step * frequency
                fx = _noise.pnoise1(t + body_index * 31.7 + seed) * strength
                fy = _noise.pnoise1(t + body_index * 31.7 + seed + 100.3) * strength
                fz = _noise.pnoise1(t + body_index * 31.7 + seed + 200.7) * strength
            else:
                rng = _np.random.default_rng(seed + body_index * 37 + step)
                fx, fy, fz = (rng.standard_normal(3) * strength).tolist()
            p.applyExternalForce(body, -1, [fx, fy, fz], [0, 0, 0], p.WORLD_FRAME)
        else:
            print(f"[RBDSolver] Unknown force type: {ftype!r}, skipping")


def _extract_mesh_data(prim) -> tuple:
    """Extract mesh vertices and face vertex indices from a UsdGeom.Mesh prim.

    Returns (vertices as np.ndarray of shape [N,3], flat indices list) or (None, None)
    if the prim has no UsdGeom.Mesh point data.
    """
    mesh = UsdGeom.Mesh(prim)
    pts = mesh.GetPointsAttr().Get()
    if pts is None or len(pts) == 0:
        return None, None
    vertices = _np.array([[v[0], v[1], v[2]] for v in pts])
    face_idx_attr = mesh.GetFaceVertexIndicesAttr()
    face_indices = face_idx_attr.Get() if face_idx_attr else None
    indices = list(face_indices) if face_indices is not None else None
    return vertices, indices


def _variance_multiplier(rng: random.Random, variance: float) -> float:
    """Return a random multiplier in [1-variance, 1+variance], or 1.0 if variance==0."""
    if variance == 0.0:
        return 1.0
    return rng.uniform(1.0 - variance, 1.0 + variance)
