"""Physics simulation nodes using PyBullet."""
import math
from ._base import UsdNodeBase, PORT_COLORS


class RBDSolverNode(UsdNodeBase):
    """Rigid Body Dynamics solver using PyBullet.

    Takes a PointInstancer (from Scatter Points) and simulates gravity + collisions.
    Collision geometry can be plugged in as separate prim inputs.
    Bakes the result as time-sampled positions/orientations on the instancer.
    """
    NODE_NAME = "RBD Solver"
    CATEGORY = "Create"

    _SHAPE_TYPES = ["Box", "Sphere", "Convex Hull"]

    def __init__(self):
        super().__init__()
        self.add_input("instancer", color=PORT_COLORS["prim"])
        self.add_input("collision", multi_input=True, color=PORT_COLORS["prim"])
        self.add_text_input("frames", "Frame Count", text="120")
        self.add_text_input("fps", "FPS", text="24")
        self.add_text_input("gravity", "Gravity", text="-9.81")
        self.add_text_input("friction", "Friction", text="0.5")
        self.add_text_input("restitution", "Bounce", text="0.3")
        self.add_text_input("mass", "Mass", text="1.0")
        self.add_combo_menu("shape_type", "Collision Shape", items=self._SHAPE_TYPES)
        self.add_text_input("drop_height", "Drop Height", text="50")
        self.add_text_input("start_frame", "Start Frame", text="1")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage):
        import pybullet as p
        import pybullet_data
        from pxr import UsdGeom, Vt, Gf, Sdf, Usd

        instancer_prim = self._get_input_value("instancer")
        collision_prims = self._get_all_collision_inputs()

        if not instancer_prim or not instancer_prim.IsValid():
            print("[RBDSolver] No instancer input")
            return {"prim": None}

        instancer = UsdGeom.PointInstancer(instancer_prim)
        if not instancer:
            print("[RBDSolver] Input is not a PointInstancer")
            return {"prim": None}

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
        dt = 1.0 / fps

        # Get initial positions and orientations
        positions = instancer.GetPositionsAttr().Get()
        orientations = instancer.GetOrientationsAttr().Get()
        scales = instancer.GetScalesAttr().Get()

        if not positions:
            print("[RBDSolver] Instancer has no positions")
            return {"prim": instancer_prim}

        num_instances = len(positions)
        if not orientations:
            orientations = [Gf.Quath(1, 0, 0, 0)] * num_instances
        if not scales:
            scales = [Gf.Vec3f(1, 1, 1)] * num_instances

        # Get prototype bounding box for collision shape size
        proto_rel = instancer.GetPrototypesRel()
        proto_targets = proto_rel.GetTargets()
        half_extents = [0.5, 0.5, 0.5]
        radius = 0.5
        if proto_targets:
            proto_prim = stage.GetPrimAtPath(proto_targets[0])
            if proto_prim.IsValid():
                bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
                bbox = bbox_cache.ComputeLocalBound(proto_prim)
                rng = bbox.ComputeAlignedRange()
                if not rng.IsEmpty():
                    sz = rng.GetMax() - rng.GetMin()
                    half_extents = [sz[0]/2, sz[1]/2, sz[2]/2]
                    radius = max(half_extents)

        # Setup PyBullet
        physics_client = p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, gravity)
        p.setTimeStep(dt)

        # Add collision geometry
        self._add_collision_geometry(p, stage, collision_prims, friction, restitution)

        # Create rigid bodies for each instance
        bodies = []
        for i in range(num_instances):
            pos = [positions[i][0], positions[i][1], positions[i][2] + drop_height]
            q = orientations[i]
            orn = [q.GetImaginary()[0], q.GetImaginary()[1], q.GetImaginary()[2], q.GetReal()]
            sc = scales[i]

            # Scaled half extents
            he = [half_extents[0] * sc[0], half_extents[1] * sc[1], half_extents[2] * sc[2]]

            if shape_type == "Sphere":
                col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=max(he))
            elif shape_type == "Convex Hull":
                # Approximate with box for now unless we have mesh data
                col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=he)
            else:  # Box
                col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=he)

            body = p.createMultiBody(
                baseMass=mass,
                baseCollisionShapeIndex=col_shape,
                basePosition=pos,
                baseOrientation=orn)
            p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
            bodies.append(body)

        # Simulate and record
        all_positions = []
        all_orientations = []

        for frame in range(frame_count):
            p.stepSimulation()

            frame_pos = []
            frame_orn = []
            for body in bodies:
                pos, orn = p.getBasePositionAndOrientation(body)
                frame_pos.append(Gf.Vec3f(pos[0], pos[1], pos[2]))
                # PyBullet: [x,y,z,w], USD Quath: (w, x, y, z)
                frame_orn.append(Gf.Quath(orn[3], orn[0], orn[1], orn[2]))

            all_positions.append(frame_pos)
            all_orientations.append(frame_orn)

        p.disconnect()

        # Write time-sampled data to the instancer
        pos_attr = instancer.GetPositionsAttr()
        orn_attr = instancer.GetOrientationsAttr()

        for frame_idx in range(frame_count):
            time_code = Usd.TimeCode(start_frame + frame_idx)
            pos_attr.Set(Vt.Vec3fArray(all_positions[frame_idx]), time_code)
            orn_attr.Set(Vt.QuathArray(all_orientations[frame_idx]), time_code)

        # Set stage time range
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)

        # Refresh usdview timeline so playback works
        try:
            from usd_chunks import chunk_registry
            scene = chunk_registry.ChunkRegistry().scene
            if scene and hasattr(scene, 'usdviewAPI'):
                ctrl = scene.usdviewAPI._UsdviewApi__appController
                # Update the controller's internal range
                ctrl.realStartTimeCode = start_frame
                ctrl.realEndTimeCode = start_frame + frame_count - 1
                # Rebuild time samples and enable playback
                ctrl._UpdateTimeSamples()
        except Exception as e:
            print(f"[RBDSolver] Could not refresh timeline: {e}")

        print(f"[RBDSolver] Baked {frame_count} frames for {num_instances} bodies")
        return {"prim": instancer_prim}

    def _get_all_collision_inputs(self):
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

    def _add_collision_geometry(self, p, stage, collision_prims, friction, restitution):
        """Add collision geometry from collision prim inputs."""
        from pxr import UsdGeom, Gf, Usd

        if not collision_prims:
            # No collision input — add infinite ground at Z=0
            ground_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
            p.createMultiBody(baseMass=0, baseCollisionShapeIndex=ground_shape,
                              basePosition=[0, 0, 0])
            return

        for collision_prim in collision_prims:
            # Get bounding box and create a flat box collider
            bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
            bbox = bbox_cache.ComputeWorldBound(collision_prim)
            rng = bbox.ComputeAlignedRange()

            if not rng.IsEmpty():
                mn = rng.GetMin()
                mx = rng.GetMax()
                cx = (mn[0] + mx[0]) / 2
                cy = (mn[1] + mx[1]) / 2
                cz = (mn[2] + mx[2]) / 2
                hx = max((mx[0] - mn[0]) / 2, 0.5)
                hy = max((mx[1] - mn[1]) / 2, 0.5)
                hz = max((mx[2] - mn[2]) / 2, 0.1)

                # If very flat (a plane), give it thickness
                if hz < 1.0:
                    hz = 1.0
                    cz = cz - hz

                col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=[hx, hy, hz])
                body = p.createMultiBody(baseMass=0, baseCollisionShapeIndex=col_shape,
                                         basePosition=[cx, cy, cz])
                p.changeDynamics(body, -1, lateralFriction=friction, restitution=restitution)
