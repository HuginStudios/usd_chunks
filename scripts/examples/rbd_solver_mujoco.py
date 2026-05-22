"""MuJoCo RBD Solver — rigid body simulation via MuJoCo."""

from __future__ import annotations

import os
import shutil
import struct
import tempfile

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk

try:
    import mujoco as _mj
    import numpy as np

    _HAS_MUJOCO = True
except ImportError:
    _HAS_MUJOCO = False


def _export_mesh_stl(prim):
    """Export all meshes under *prim* to a single binary STL in a temp dir.

    Returns (temp_dir, stl_filename) or (None, None) if no mesh found.
    """
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    all_triangles = []

    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        if not mesh:
            continue
        points = mesh.GetPointsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        counts = mesh.GetFaceVertexCountsAttr().Get()
        if not points or not indices or not counts:
            continue

        world_xf = xform_cache.GetLocalToWorldTransform(p)
        xf_points = [Gf.Vec3f(world_xf.Transform(Gf.Vec3d(pt))) for pt in points]

        idx = 0
        for fc in counts:
            for t in range(fc - 2):
                v0 = xf_points[indices[idx]]
                v1 = xf_points[indices[idx + t + 1]]
                v2 = xf_points[indices[idx + t + 2]]
                all_triangles.append((v0, v1, v2))
            idx += fc

    if not all_triangles:
        return None, None

    temp_dir = tempfile.mkdtemp(prefix="usd_stl_")
    stl_name = "collision.stl"
    stl_path = os.path.join(temp_dir, stl_name)

    num_tri = len(all_triangles)
    with open(stl_path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", num_tri))
        for v0, v1, v2 in all_triangles:
            e1 = Gf.Vec3f(v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
            e2 = Gf.Vec3f(v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
            n = Gf.Vec3f(
                e1[1] * e2[2] - e1[2] * e2[1],
                e1[2] * e2[0] - e1[0] * e2[2],
                e1[0] * e2[1] - e1[1] * e2[0],
            )
            f.write(struct.pack("<3f", n[0], n[1], n[2]))
            f.write(struct.pack("<3f", v0[0], v0[1], v0[2]))
            f.write(struct.pack("<3f", v1[0], v1[1], v1[2]))
            f.write(struct.pack("<3f", v2[0], v2[1], v2[2]))
            f.write(struct.pack("<H", 0))

    return temp_dir, stl_name


def _build_collision_mjcf(stage, collision_prims):
    """Build MJCF asset/body XML for concave mesh colliders.

    Returns (asset_xml_str, body_xml_str, temp_dirs_to_cleanup).
    """
    shared_dir = tempfile.mkdtemp(prefix="usd_mjcf_")
    temp_dirs = [shared_dir]
    asset_lines = []
    body_lines = []

    for i, cprim in enumerate(collision_prims):
        src_dir, stl_name = _export_mesh_stl(cprim)
        if src_dir is None:
            continue
        unique_name = f"col{i}.stl"
        shutil.move(os.path.join(src_dir, stl_name), os.path.join(shared_dir, unique_name))
        shutil.rmtree(src_dir, ignore_errors=True)

        mesh_id = f"col{i}"
        asset_lines.append(f'    <mesh name="{mesh_id}" file="{unique_name}"/>')
        body_lines.append(
            f'    <body name="collider_{i}" pos="0 0 0">'
            f'<geom type="mesh" mesh="{mesh_id}" contype="1" conaffinity="1"/>'
            f"</body>"
        )

    asset_xml = "\n".join(asset_lines)
    body_xml = "\n".join(body_lines)
    return asset_xml, body_xml, temp_dirs


def _cleanup_temp_dirs(dirs):
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def _map_friction(friction_value: float) -> str:
    slide = max(friction_value, 0.001)
    torsional = friction_value * 0.005
    rolling = friction_value * 0.001
    return f"{slide} {torsional} {rolling}"


def _map_bounce(bounce_value: float) -> str:
    dampratio = 1.0 - max(0.0, min(1.0, bounce_value)) * 0.9
    return f"0.02 {dampratio}"


def _map_damping(lin_damping: float, ang_damping: float) -> tuple[str, str]:
    return f"{lin_damping}", f"{ang_damping}"


def simulate_mujoco(
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
    half_extents,
    z_offset,
    otx,
    oty,
    otz,
    orx,
    ory,
    orz,
    collision_prims=None,
    friction=0.5,
    restitution=0.1,
    lin_damping=0.0,
    ang_damping=0.05,
):
    """Run a MuJoCo sphere simulation and bake keyframes onto the USD prim.

    Parameters match the PyBullet solver pattern. Collects all matrices first,
    then writes them inside a Usd.EditContext.
    """
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    # Compute world position and orientation from prim transform
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    translate = world_xf.ExtractTranslation()
    rot = world_xf.ExtractRotation()

    # Apply rotation offsets
    if orx != 0.0 or ory != 0.0 or orz != 0.0:
        rot = (
            rot
            * Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
            * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
            * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz)
        )

    quat = rot.GetQuat()
    imag = quat.GetImaginary()
    # MuJoCo quat: w, x, y, z (same as Gf.Quatd)
    w, x, y, z = float(quat.GetReal()), float(imag[0]), float(imag[1]), float(imag[2])

    # Starting position with offsets
    px = float(translate[0]) + otx
    py = float(translate[1]) + oty
    pz = float(translate[2]) + otz + z_offset

    # Sphere radius from half extents
    radius = max(half_extents)

    # Timestep
    dt = 1.0 / fps

    # Map material parameters
    friction_3 = _map_friction(friction)
    solref = _map_bounce(restitution)
    lin_damp, ang_damp = _map_damping(lin_damping, ang_damping)

    # Build MJCF XML
    temp_dirs = []
    compiler_line = ""
    asset_block = ""
    collision_bodies = ""

    if collision_prims:
        asset_xml, body_xml, temp_dirs = _build_collision_mjcf(stage, collision_prims)
        if asset_xml:
            meshdir = temp_dirs[0].replace("\\", "/")
            compiler_line = f'  <compiler meshdir="{meshdir}"/>'
            asset_block = f"  <asset>\n{asset_xml}\n  </asset>"
            collision_bodies = body_xml

    xml = f"""<mujoco>
{compiler_line}
  <option gravity="0 0 {gravity}" timestep="{dt / substeps}" viscosity="{lin_damp}"/>
  <default>
    <geom solref="{solref}" friction="{friction_3}"/>
    <joint damping="{ang_damp}"/>
  </default>
{asset_block}
  <worldbody>
    <geom type="plane" size="100 100 0.1" contype="1" conaffinity="1"/>
    <body name="dynamic" pos="{px} {py} {pz}" quat="{w} {x} {y} {z}">
      <freejoint/>
      <geom type="sphere" size="{radius}" mass="{mass}" contype="1" conaffinity="1"/>
    </body>
{collision_bodies}
  </worldbody>
</mujoco>"""

    model = _mj.MjModel.from_xml_string(xml)
    _cleanup_temp_dirs(temp_dirs)
    data = _mj.MjData(model)

    # Set initial velocities (linear: qvel[0:3], angular: qvel[3:6])
    if init_vel:
        data.qvel[0] = init_vel[0]
        data.qvel[1] = init_vel[1]
        data.qvel[2] = init_vel[2]
    if ang_vel:
        data.qvel[3] = ang_vel[0]
        data.qvel[4] = ang_vel[1]
        data.qvel[5] = ang_vel[2]

    # Simulate and collect matrices
    all_mats = []
    for _ in range(frame_count):
        # Read position and quaternion from MuJoCo
        pos = data.xpos[1]  # body index 1 (0 is world)
        quat_mj = data.xquat[1]  # [w, x, y, z]

        # Build world matrix
        world_mat = Gf.Matrix4d()
        world_mat.SetRotate(
            Gf.Quatd(float(quat_mj[0]), float(quat_mj[1]), float(quat_mj[2]), float(quat_mj[3]))
        )
        corrected = [float(pos[0]), float(pos[1]), float(pos[2])]
        corrected[2] -= z_offset
        world_mat.SetTranslateOnly(Gf.Vec3d(*corrected))

        # Convert world to local space
        local_mat = world_mat * parent_world_inv
        all_mats.append(local_mat)

        # Step simulation
        for _ in range(substeps):
            _mj.mj_step(model, data)

    # Write keyframes to USD
    with Usd.EditContext(stage, layer):
        order_attr = prim.GetAttribute("xformOpOrder")
        if not order_attr:
            order_attr = prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray)
        order_attr.Set(["xformOp:transform"])

        xf_attr = prim.GetAttribute("xformOp:transform")
        if not xf_attr:
            xf_attr = prim.CreateAttribute("xformOp:transform", Sdf.ValueTypeNames.Matrix4d)

        xf_attr.Set(all_mats[0])
        for frame_idx, mat in enumerate(all_mats):
            xf_attr.Set(mat, Usd.TimeCode(start_frame + frame_idx))

    # Set stage time codes on session layer
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)

    print(f"[MuJoCoSolver] Baked {frame_count} frames on {prim.GetPath()}")
    return all_mats


def simulate_mujoco_instancer(
    stage, prim, layer, collision_prims,
    frame_count, fps, gravity, mass, substeps,
    init_vel, ang_vel, start_frame,
    otx, oty, otz, orx, ory, orz,
):
    """Run MuJoCo multi-body simulation for a UsdGeom.PointInstancer."""
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

    # World transform of the instancer
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    world_inv = world_xf.GetInverse()

    # Derive sphere radius from first prototype's bounding box
    proto_rel = instancer.GetPrototypesRel()
    proto_targets = proto_rel.GetTargets()
    radius = 0.5
    if proto_targets:
        proto_prim = stage.GetPrimAtPath(proto_targets[0])
        if proto_prim:
            bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default"])
            bbox = bbox_cache.ComputeWorldBound(proto_prim)
            rng = bbox.ComputeAlignedRange()
            ext = rng.GetMax() - rng.GetMin()
            radius = max(float(ext[0]), float(ext[1]), float(ext[2])) / 2.0

    dt = 1.0 / fps

    # Build per-instance world positions and orientations
    body_entries = []
    for i in range(num_instances):
        lp = positions[i]
        pos_local = Gf.Vec3d(float(lp[0]), float(lp[1]), float(lp[2]))
        pos_world = world_xf.Transform(pos_local)

        # Apply translation offset
        px = pos_world[0] + otx
        py = pos_world[1] + oty
        pz = pos_world[2] + otz

        # Apply rotation offset
        if orientations and i < len(orientations):
            q = orientations[i]
            quat = Gf.Quatd(float(q.GetReal()), float(q.GetImaginary()[0]),
                            float(q.GetImaginary()[1]), float(q.GetImaginary()[2]))
        else:
            quat = Gf.Quatd(1, 0, 0, 0)

        if orx != 0.0 or ory != 0.0 or orz != 0.0:
            rot = Gf.Rotation(quat)
            rot = (rot
                   * Gf.Rotation(Gf.Vec3d(1, 0, 0), orx)
                   * Gf.Rotation(Gf.Vec3d(0, 1, 0), ory)
                   * Gf.Rotation(Gf.Vec3d(0, 0, 1), orz))
            quat = rot.GetQuat()

        w = float(quat.GetReal())
        im = quat.GetImaginary()
        x, y, z = float(im[0]), float(im[1]), float(im[2])
        body_entries.append((px, py, pz, w, x, y, z))

    # Build MJCF XML
    bodies_xml = ""
    for i, (px, py, pz, w, x, y, z) in enumerate(body_entries):
        bodies_xml += (
            f'    <body name="b{i}" pos="{px} {py} {pz}" quat="{w} {x} {y} {z}">\n'
            f'      <freejoint/>\n'
            f'      <geom type="sphere" size="{radius}" mass="{mass}"/>\n'
            f'    </body>\n'
        )

    xml = (
        f'<mujoco>\n'
        f'  <option gravity="0 0 {gravity}" timestep="{dt / substeps}"/>\n'
        f'  <worldbody>\n'
        f'    <geom type="plane" size="100 100 0.1"/>\n'
        # TODO: Add collision mesh geoms from collision_prims (handled by slice 02)
        f'{bodies_xml}'
        f'  </worldbody>\n'
        f'</mujoco>'
    )

    model = _mj.MjModel.from_xml_string(xml)
    data = _mj.MjData(model)

    # Set initial velocities — 6 DOF per freejointed body
    for i in range(num_instances):
        base = i * 6
        if init_vel:
            data.qvel[base + 0] = init_vel[0]
            data.qvel[base + 1] = init_vel[1]
            data.qvel[base + 2] = init_vel[2]
        if ang_vel:
            data.qvel[base + 3] = ang_vel[0]
            data.qvel[base + 4] = ang_vel[1]
            data.qvel[base + 5] = ang_vel[2]

    # Simulate and collect per-frame data
    all_positions = []
    all_orientations = []
    for _ in range(frame_count):
        frame_pos = Vt.Vec3fArray(num_instances)
        frame_ori = Vt.QuathArray(num_instances)
        for i in range(num_instances):
            body_idx = i + 1  # body 0 is world
            pos = data.xpos[body_idx]
            quat_mj = data.xquat[body_idx]  # [w, x, y, z]

            # Convert world position back to instancer-local space
            world_pos = Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2]))
            local_pos = world_inv.Transform(world_pos)
            frame_pos[i] = Gf.Vec3f(float(local_pos[0]), float(local_pos[1]), float(local_pos[2]))

            # Convert quaternion to GfQuath
            frame_ori[i] = Gf.Quath(
                float(quat_mj[0]), float(quat_mj[1]),
                float(quat_mj[2]), float(quat_mj[3])
            )

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

    # Set stage time codes on session layer
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


def _clear_chunk_bake(owner):
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)


def reset(owner, notice, sender):
    _clear_chunk_bake(owner)
    print("[MuJoCoSolver] Reset — bake cleared.")


def execute(owner, notice, sender):
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prims = _chunk.get_param("prim_path").get_prims()
    if not prims:
        print("[MuJoCoSolver] No target prim selected")
        return
    prim = prims[0]
    if not prim.IsValid() or prim.GetPath() == Sdf.Path("/"):
        return

    collision_prims = _chunk.get_param("collision_path").get_prims()

    frame_count = int(_chunk.get_param("frame_count").value or 120)
    fps = float(_chunk.get_param("fps").value or 24)
    gravity = float(_chunk.get_param("gravity").value or -981)
    substeps = max(1, int(_chunk.get_param("substeps").value or 4))
    start_frame = int(_chunk.get_param("start_frame").value or 1)
    friction = float(_chunk.get_param("friction").value or 0.5)
    restitution = float(_chunk.get_param("restitution").value or 0.1)
    mass = float(_chunk.get_param("mass").value or 1.0)
    lin_damping = float(_chunk.get_param("lin_damping").value or 0.0)
    ang_damping = float(_chunk.get_param("ang_damping").value or 0.05)
    otx = float(_chunk.get_param("off_tx").value or 0)
    oty = float(_chunk.get_param("off_ty").value or 0)
    otz = float(_chunk.get_param("off_tz").value or 0)
    orx = float(_chunk.get_param("off_rx").value or 0)
    ory = float(_chunk.get_param("off_ry").value or 0)
    orz = float(_chunk.get_param("off_rz").value or 0)
    init_vel = _parse_vec3(_chunk.get_param("initial_velocity").value)
    ang_vel = _parse_vec3(_chunk.get_param("angular_velocity").value)

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    if UsdGeom.PointInstancer(prim).GetPositionsAttr().HasValue():
        simulate_mujoco_instancer(
            stage, prim, layer, collision_prims,
            frame_count, fps, gravity, mass, substeps,
            init_vel, ang_vel, start_frame,
            otx, oty, otz, orx, ory, orz,
        )
    else:
        xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
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
            translate = xform_cache.GetLocalToWorldTransform(prim).ExtractTranslation()
            z_offset = ((rng.GetMin()[2] + rng.GetMax()[2]) / 2) - translate[2]

        simulate_mujoco(
            stage, prim, layer, frame_count, fps, gravity, mass, substeps,
            init_vel, ang_vel, start_frame, parent_world_inv, half_extents, z_offset,
            otx, oty, otz, orx, ory, orz,
            collision_prims=collision_prims,
            friction=friction, restitution=restitution,
            lin_damping=lin_damping, ang_damping=ang_damping,
        )

    try:
        from scripts.node_graph.graph_host import GraphHost
        GraphHost.current().refresh_timeline(start_frame, start_frame + frame_count - 1)
    except Exception:
        pass


def main():
    chunk_api = chunk.Chunk("RBD Solver (MuJoCo)")

    prim_path = chunk.LineEdit(
        "prim_path", "Target Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(prim_path)

    collision_path = chunk.LineEdit(
        "collision_path", "Collision Prim",
        flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
    )
    chunk_api.add_param(collision_path)

    _GF = chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED | chunk.ChunkFlags.GroupFlags.FRAMED

    sim_grp = chunk.Group("Simulation", flags=_GF)
    sim_grp.add_param(chunk.Spinbox("frame_count", "Frame Count", default_value=120, is_int=True))
    sim_grp.add_param(chunk.Spinbox("fps", "FPS", default_value=24.0))
    sim_grp.add_param(chunk.Spinbox("gravity", "Gravity", default_value=-981.0))
    sim_grp.add_param(chunk.Spinbox("substeps", "Substeps", default_value=4, is_int=True))
    sim_grp.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True))
    chunk_api.add_param(sim_grp)

    physics_grp = chunk.Group("Physics", flags=_GF)
    physics_grp.add_param(chunk.Spinbox("friction", "Friction", default_value=0.5))
    physics_grp.add_param(chunk.Spinbox("restitution", "Bounce", default_value=0.1))
    physics_grp.add_param(chunk.Spinbox("mass", "Mass", default_value=1.0))
    physics_grp.add_param(chunk.Spinbox("lin_damping", "Linear Damping", default_value=0.0))
    physics_grp.add_param(chunk.Spinbox("ang_damping", "Angular Damping", default_value=0.05))
    chunk_api.add_param(physics_grp)

    offset_grp = chunk.Group("Offsets", flags=_GF)
    offset_grp.add_param(chunk.Spinbox("off_tx", "Offset X", default_value=0.0))
    offset_grp.add_param(chunk.Spinbox("off_ty", "Offset Y", default_value=0.0))
    offset_grp.add_param(chunk.Spinbox("off_tz", "Offset Z", default_value=0.0))
    offset_grp.add_param(chunk.Spinbox("off_rx", "Rotate X", default_value=0.0))
    offset_grp.add_param(chunk.Spinbox("off_ry", "Rotate Y", default_value=0.0))
    offset_grp.add_param(chunk.Spinbox("off_rz", "Rotate Z", default_value=0.0))
    chunk_api.add_param(offset_grp)

    vel_grp = chunk.Group("Velocity", flags=_GF)
    vel_grp.add_param(chunk.LineEdit("initial_velocity", "Linear Velocity", default_value="0 0 0"))
    vel_grp.add_param(chunk.LineEdit("angular_velocity", "Angular Velocity", default_value="0 0 0"))
    chunk_api.add_param(vel_grp)

    run_btn = chunk.Button("run_sim", "Run Simulation")
    run_btn.add_callback(execute)
    chunk_api.add_param(run_btn)

    reset_btn = chunk.Button("reset_sim", "Reset")
    reset_btn.add_callback(reset)
    chunk_api.add_param(reset_btn)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Simulation")
