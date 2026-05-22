"""MuJoCo RBD Solver — rigid body simulation via MuJoCo."""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

try:
    import mujoco as _mj

    _HAS_MUJOCO = True
except ImportError:
    _HAS_MUJOCO = False


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

    # Build MJCF XML
    xml = f"""<mujoco>
  <option gravity="0 0 {gravity}" timestep="{dt / substeps}"/>
  <worldbody>
    <geom type="plane" size="100 100 0.1"/>
    <body name="dynamic" pos="{px} {py} {pz}" quat="{w} {x} {y} {z}">
      <freejoint/>
      <geom type="sphere" size="{radius}" mass="{mass}"/>
    </body>
  </worldbody>
</mujoco>"""

    model = _mj.MjModel.from_xml_string(xml)
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
