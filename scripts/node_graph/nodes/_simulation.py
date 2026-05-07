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
        self.add_text_input("gravity", "Gravity", text="-9.81")
        self.add_text_input("friction", "Friction", text="0.5")
        self.add_text_input("restitution", "Bounce", text="0.3")
        self.add_text_input("mass", "Mass", text="1.0")
        self.add_combo_menu("shape_type", "Collision Shape", items=self._SHAPE_TYPES)
        self.add_text_input("drop_height", "Drop Height", text="50")
        self.add_text_input("start_frame", "Start Frame", text="1")
        self.add_text_input("substeps", "Substeps", text="4")
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

        # Create anonymous layer to capture all baked time samples
        bake_layer = Sdf.Layer.CreateAnonymous("rbd_bake")
        stage.GetRootLayer().subLayerPaths.insert(0, bake_layer.identifier)

        # Detect if input is a PointInstancer or a regular prim
        instancer = UsdGeom.PointInstancer(input_prim)
        is_instancer = bool(instancer.GetPositionsAttr().Get())

        with Usd.EditContext(stage, bake_layer):
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
        return {"prim": input_prim, "layer": bake_layer}

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

        # Setup PyBullet — substep timestep so N steps == 1 frame
        p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, gravity)
        p.setTimeStep(dt / substeps)
        self._add_collision_geometry(p, stage, collision_prims, friction, restitution)

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
            body_restitution = restitution * _variance_multiplier(rng_inst, restitution_var)

            if shape_type == "Sphere":
                col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=max(he))
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
            p.resetBaseVelocity(
                body, linearVelocity=init_vel, angularVelocity=ang_vel
            )
            bodies.append(body)

        force_dicts = force_dicts or []

        # Simulate — substeps steps per frame
        all_positions = []
        all_orientations = []
        for frame in range(frame_count):
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
        # Get prim's world transform as initial position/orientation
        xform_cache = UsdGeom.XformCache()
        world_xf = xform_cache.GetLocalToWorldTransform(prim)
        translate = world_xf.ExtractTranslation()
        rot = world_xf.ExtractRotation()

        # Start from current world position, offset by drop_height
        init_pos = [translate[0], translate[1], translate[2] + drop_height]

        # Convert rotation to quaternion for PyBullet (xyzw)
        quat = rot.GetQuat()
        real = quat.GetReal()
        imag = quat.GetImaginary()
        init_orn = [imag[0], imag[1], imag[2], real]

        # Get parent's world transform to convert sim results back to local space
        parent = prim.GetParent()
        parent_world_inv = Gf.Matrix4d(1.0)
        if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
            parent_xf = xform_cache.GetLocalToWorldTransform(parent)
            parent_world_inv = parent_xf.GetInverse()

        # Get bbox for collision shape
        bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
        bbox = bbox_cache.ComputeLocalBound(prim)
        rng = bbox.ComputeAlignedRange()
        half_extents = [0.5, 0.5, 0.5]
        if not rng.IsEmpty():
            sz = rng.GetMax() - rng.GetMin()
            half_extents = [sz[0] / 2, sz[1] / 2, sz[2] / 2]

        # Per-body variance (single body — deterministic from seed)
        rng_inst = random.Random(var_seed)
        body_mass = mass * _variance_multiplier(rng_inst, mass_var)
        body_friction = friction * _variance_multiplier(rng_inst, friction_var)
        body_restitution = restitution * _variance_multiplier(rng_inst, restitution_var)

        # Setup PyBullet — substep timestep so N steps == 1 frame
        p.connect(p.DIRECT)
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, gravity)
        p.setTimeStep(dt / substeps)
        self._add_collision_geometry(p, stage, collision_prims, friction, restitution)

        # Create rigid body
        if shape_type == "Sphere":
            col_shape = p.createCollisionShape(p.GEOM_SPHERE, radius=max(half_extents))
        else:
            col_shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=half_extents)

        body = p.createMultiBody(
            baseMass=body_mass,
            baseCollisionShapeIndex=col_shape,
            basePosition=init_pos,
            baseOrientation=init_orn,
        )
        p.changeDynamics(
            body, -1, lateralFriction=body_friction, restitution=body_restitution
        )
        p.resetBaseVelocity(body, linearVelocity=init_vel, angularVelocity=ang_vel)

        force_dicts = force_dicts or []

        # Simulate and write xform per frame
        xformable = UsdGeom.Xformable(prim)
        xformable.ClearXformOpOrder()
        xf_op = xformable.AddTransformOp()

        for frame in range(frame_count):
            for sub in range(substeps):
                step = frame * substeps + sub
                _apply_forces(p, body, 0, step, force_dicts)
                p.stepSimulation()
            pos, orn = p.getBasePositionAndOrientation(body)
            # Build world-space matrix from sim result
            world_mat = Gf.Matrix4d()
            world_mat.SetRotate(Gf.Quatd(orn[3], orn[0], orn[1], orn[2]))
            world_mat.SetTranslateOnly(Gf.Vec3d(pos[0], pos[1], pos[2]))
            # Convert to local space relative to parent
            local_mat = world_mat * parent_world_inv
            xf_op.Set(local_mat, Usd.TimeCode(start_frame + frame))

        p.disconnect()

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
        self, p, stage, collision_prims, friction, restitution
    ) -> None:
        """Add collision geometry from collision prim inputs."""
        if not collision_prims:
            # No collision input — add infinite ground at Z=0
            ground_shape = p.createCollisionShape(p.GEOM_PLANE, planeNormal=[0, 0, 1])
            p.createMultiBody(
                baseMass=0, baseCollisionShapeIndex=ground_shape, basePosition=[0, 0, 0]
            )
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
                body = p.createMultiBody(
                    baseMass=0,
                    baseCollisionShapeIndex=col_shape,
                    basePosition=[cx, cy, cz],
                )
                p.changeDynamics(
                    body, -1, lateralFriction=friction, restitution=restitution
                )

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


def _variance_multiplier(rng: random.Random, variance: float) -> float:
    """Return a random multiplier in [1-variance, 1+variance], or 1.0 if variance==0."""
    if variance == 0.0:
        return 1.0
    return rng.uniform(1.0 - variance, 1.0 + variance)
