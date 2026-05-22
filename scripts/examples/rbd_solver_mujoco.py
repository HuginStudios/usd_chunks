"""MuJoCo RBD Solver — rigid body simulation via MuJoCo."""

from __future__ import annotations

from pxr import Gf, Sdf, Usd, UsdGeom

try:
    import mujoco as _mj

    _HAS_MUJOCO = True
except ImportError:
    _HAS_MUJOCO = False


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
    xml = f"""<mujoco>
  <option gravity="0 0 {gravity}" timestep="{dt / substeps}" viscosity="{lin_damp}"/>
  <default>
    <geom solref="{solref}" friction="{friction_3}"/>
    <joint damping="{ang_damp}"/>
  </default>
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
