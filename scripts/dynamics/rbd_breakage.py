"""Breakage for the RBD Solver: rigid bodies that break into their pre-cut pieces.

Not a chunk (no ``main``): the RBD Solver loads it when its target is a fractured
PointInstancer -- one whose prototypes carry a ``fracture:`` tree, as the Fracture chunk
writes -- and Breakable is on.

How pieces come and go
----------------------
MuJoCo cannot add a body to a running simulation; the model would have to be recompiled
at every break. So every piece anything can break into gets a body up front -- one per
(instance or drop, node of its fracture tree) -- and waits *parked*: asleep, weightless,
colliding with nothing, far from the scene. Starting or dropping a body wakes its piece;
breaking one parks it and wakes its children exactly where it was, moving as it moved.
Those bodies are also the output: the instancer is rewritten with one instance per body,
parked ones switched off through ``invisibleIds``.

What breaks a body
------------------
The largest contact force on it within a step, against a threshold that scales with the
piece's cross-section -- a piece half the size needs a quarter of the force. Strength is in
multiples of the whole body's own weight, so it reads the same at any scale or density.

What moves the world
--------------------
The collider prims are read the way a UsdPhysics scene describes them:

* an animated collider -- its own transform or an ancestor's has time samples, a Pose or
  Transform chunk keyed over time -- follows that animation, with the *velocity* of the
  animation, so friction drags and teeth bite rather than the collider teleporting through
  what it touches;
* a collider that is ``body1`` of a ``UsdPhysics.RevoluteJoint`` whose angular drive has a
  ``targetVelocity`` spins about that joint at that speed, with unlimited torque (a
  crusher's flywheel does not notice a rock) -- and the spin is baked onto the prim;
* a collider with ``physxSurfaceVelocity:surfaceVelocity`` (PhysX's conveyor attribute)
  stays put and carries what rests on it at that velocity;
* anything else is static.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, Vt

from usd_chunks import chunk_registry

try:
    import mujoco as _mj
    import numpy as np

    _HAS_MUJOCO = True
except ImportError:  # pragma: no cover - exercised only on a machine without mujoco
    _mj = None
    np = None
    _HAS_MUJOCO = False

try:
    from PySide6 import QtCore
except ImportError:  # pragma: no cover - headless install without Qt
    QtCore = None

logger = logging.getLogger(__name__)

NS = "fracture:"
SURFACE_VELOCITY = "physxSurfaceVelocity:surfaceVelocity"
SURFACE_VELOCITY_ON = "physxSurfaceVelocity:surfaceVelocityEnabled"
THICKNESS = "rbd:colliderThickness"

# Contact bitmasks: a pair collides when either one's contype meets the other's conaffinity.
# Bodies collide with bodies and with the world; world parts never with each other (a rotor
# turning inside its housing would otherwise fight it); a parked body with nothing.
_BODY = (1, 1)
_WORLD = (0, 1)
_PARKED = (0, 0)


# ---------------------------------------------------------------------------
# Small maths
# ---------------------------------------------------------------------------


def _quat_to_mat(q) -> np.ndarray:
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _qmul(a, b) -> np.ndarray:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ])


def _slerp(a, b, s) -> np.ndarray:
    d = float(a @ b)
    if d < 0.0:
        b, d = -b, -d
    if d > 0.9995:
        q = a + (b - a) * s
        return q / np.linalg.norm(q)
    th = math.acos(min(1.0, d))
    return (math.sin((1 - s) * th) * a + math.sin(s * th) * b) / math.sin(th)


def _angular_velocity(q0, q1, dt) -> np.ndarray:
    """World angular velocity that turns q0 into q1 over dt (both w,x,y,z)."""
    dq = _qmul(q1, np.array([q0[0], -q0[1], -q0[2], -q0[3]]))
    if dq[0] < 0.0:
        dq = -dq
    s = float(np.linalg.norm(dq[1:]))
    if s < 1e-12:
        return np.zeros(3)
    angle = 2.0 * math.atan2(s, dq[0])
    return dq[1:] / s * angle / dt


def _random_quat(rng) -> np.ndarray:
    q = rng.normal(size=4)
    return q / np.linalg.norm(q)


# ---------------------------------------------------------------------------
# Simulation (numpy in, numpy out -- no USD, safe on a worker thread)
# ---------------------------------------------------------------------------


def is_convex_solid(verts, tris, tol: float = 1e-4) -> bool:
    """Whether a triangle mesh is a closed, convex solid -- one MuJoCo can collide as its hull.

    Closed: every edge is shared by exactly two triangles. Convex: no vertex lies in front of
    any face's plane (beyond *tol* of the mesh's size).
    """
    v = np.asarray(verts, dtype=np.float64)
    t = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if len(t) < 4:
        return False
    # Weld by position first: a lathe or an extrusion repeats the points on its axis or seam.
    size = float(np.ptp(v, axis=0).max()) or 1.0
    _, first, weld = np.unique(np.round(v / (size * 1e-6)), axis=0, return_index=True, return_inverse=True)
    v = v[first]
    t = weld.reshape(-1)[t]
    t = t[(t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 2] != t[:, 0])]
    edges = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    if not np.all(counts == 2):
        return False
    a = v[t[:, 0]]
    n = np.cross(v[t[:, 1]] - a, v[t[:, 2]] - a)
    ln = np.linalg.norm(n, axis=1)
    keep = ln > 1e-12
    n, a, ln = n[keep], a[keep], ln[keep]
    # Outward is whichever side the centroid is not on; a face with the centroid in front
    # flips. Then no vertex may be in front of any face.
    c = v.mean(axis=0)
    flip = np.einsum("ij,ij->i", n, c - a) > 0.0
    n[flip] *= -1.0
    for start in range(0, len(n), 256):
        d = np.einsum("fvk,fk->fv", v[None, :, :] - a[start:start + 256, None, :], n[start:start + 256])
        if (d / ln[start:start + 256, None]).max() > tol * size:
            return False
    return True


_GEOM_SERIAL = [0]


def _add_soup(spec, body, verts, tris, thickness, name, friction, solref, priority=0) -> int:
    """Collision geometry for one mesh. Returns the number of geoms made.

    A closed convex mesh -- a box, a plate, a cylinder, a cone -- is one mesh geom: MuJoCo
    collides a mesh as its convex hull, which for these *is* the shape, and one geom is far
    cheaper than hundreds.

    Anything else gets the RBD Solver's Mesh collider: one thin convex prism per triangle.
    A hopper as a single mesh geom would collide as a solid block; extruding every triangle
    backwards makes small convex solids whose union is the surface itself.
    """
    v = np.asarray(verts, dtype=np.float64)
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if not len(tris):
        return 0
    # Unique across the whole model: two colliders may share a prim name.
    _GEOM_SERIAL[0] += 1
    name = f"{name}_{_GEOM_SERIAL[0]}"
    if is_convex_solid(v, tris):
        ma = spec.add_mesh()
        ma.name = f"{name}_hull"
        ma.uservert = v.astype(np.float32).reshape(-1)
        g = body.add_geom()
        g.type = _mj.mjtGeom.mjGEOM_MESH
        g.meshname = ma.name
        g.contype, g.conaffinity = _WORLD
        g.friction = [friction, 0.005, 0.0001]
        g.solref = solref
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3
        g.priority = priority
        return 1
    a, b, c = v[tris[:, 0]], v[tris[:, 1]], v[tris[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    keep = ln > 1e-12
    back = -(n[keep] / ln[keep, None]) * thickness
    prisms = np.stack([a[keep], b[keep], c[keep], a[keep] + back, b[keep] + back, c[keep] + back], axis=1)
    for i, prism in enumerate(prisms.astype(np.float32)):
        ma = spec.add_mesh()
        ma.name = f"{name}_t{i}"
        ma.uservert = prism.reshape(-1)
        g = body.add_geom()
        g.type = _mj.mjtGeom.mjGEOM_MESH
        g.meshname = ma.name
        g.contype, g.conaffinity = _WORLD
        g.friction = [friction, 0.005, 0.0001]
        g.solref = solref
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3
        g.priority = priority
    return len(prisms)


def subtree(pieces: dict, key) -> list:
    """*key* and every piece below it, parents first."""
    out, stack = [], [key]
    while stack:
        k = stack.pop(0)
        if k not in pieces:
            continue
        out.append(k)
        stack.extend((k[0], c) for c in pieces[k]["children"])
    return out


def simulate(job: dict, progress=None, cancel=None) -> dict:
    """Run a breakable simulation. *job* is plain data (see gather_job).

    Returns ``positions`` / ``orientations`` (frames x slots x 3 / 4, orientations w,x,y,z),
    ``alive`` (frames x slots), ``slot_key`` ((tree, node) per slot) and ``stats``.
    """
    rng = np.random.default_rng(job.get("seed", 0))
    fps = float(job["fps"])
    frames = int(job["frames"])
    substeps = max(1, int(job["substeps"]))
    sub_dt = 1.0 / fps / substeps
    up_idx = int(job["up_idx"])
    up = np.zeros(3)
    up[up_idx] = 1.0
    g_mag = abs(float(job["gravity"]))
    friction = float(job["friction"])
    contact_tc = max(2.0 * sub_dt, 0.002)
    body_solref = [contact_tc, 1.0 - 0.9 * max(0.0, min(1.0, float(job["restitution"])))]
    world_solref = [contact_tc, 1.0]

    pieces: dict = job["pieces"]
    breakage = job["breakage"]
    max_gen = int(breakage.get("max_generation", 99))

    # ---- groups: each start is one body's whole subtree of slots ----
    # (key, pos, quat, linear velocity, angular velocity -- both world --, start time in
    # seconds from the simulation's first frame). A start at 0 is placed as given; a later
    # one is emitted then -- the instancer said when -- unless something is still in the
    # way, in which case it waits a frame rather than appear inside another body.
    groups = []
    for st in job.get("starts", ()):
        groups.append((tuple(st["key"]), st["pos"], st["quat"], st.get("lin", np.zeros(3)),
                       st.get("ang", np.zeros(3)), float(st.get("time", 0.0))))

    slot_key, slot_group = [], []
    slot_of: dict[tuple, int] = {}
    for gi, (key, *_rest) in enumerate(groups):
        for k in subtree(pieces, key):
            if pieces[k]["generation"] > max_gen:
                continue
            slot_of[(gi, k)] = len(slot_key)
            slot_key.append(k)
            slot_group.append(gi)
    n_slots = len(slot_key)
    slot_group = np.asarray(slot_group, dtype=np.int64)

    radius = {k: float(np.linalg.norm(p["points"], axis=1).max()) if len(p["points"]) else 1.0
              for k, p in pieces.items()}
    biggest = max(radius.values()) if radius else 1.0

    # ---- model ----
    spec = _mj.MjSpec()
    spec.option.timestep = sub_dt
    spec.option.gravity = list(up * float(job["gravity"]))
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST
    # Elliptic cones: a contact's first constraint row is then its normal force, the number
    # breakage is judged on.
    spec.option.cone = _mj.mjtCone.mjCONE_ELLIPTIC
    # PGS, not Newton: a pile is thousands of contact rows and Newton's cost grows with their
    # square -- measured, PGS halves a 12-rock crusher run.
    spec.option.solver = _mj.mjtSolver.mjSOL_PGS
    spec.option.iterations = int(job.get("iterations", 30))
    # Sleeping bodies cost next to nothing. The parked pool sleeps from the start and wakes
    # on a non-zero velocity; a settled pile falls asleep by itself and wakes when hit.
    spec.option.enableflags |= _mj.mjtEnableBit.mjENBL_SLEEP
    # MuJoCo's answer to a diverging step is to reset the whole simulation to its initial
    # state -- every body back in the parking lot, the clock back to zero -- which reads as the
    # simulation vanishing mid-shot. Off: a bad step is rolled back and repaired below.
    spec.option.disableflags |= _mj.mjtDisableBit.mjDSBL_AUTORESET
    spec.option.sleep_tolerance = float(job.get("sleep_tolerance", 0.5))
    spec.memory = int(96 * 1024 * 1024 + n_slots * 48 * 1024)

    lowest = None

    def _low(verts):
        nonlocal lowest
        lo = float(np.min(np.asarray(verts) @ up))
        lowest = lo if lowest is None else min(lowest, lo)

    for st in job.get("statics", ()):
        _add_soup(spec, spec.worldbody, st["verts"], st["tris"], st["thickness"], st["name"], friction, world_solref)
        _low(st["verts"])
    belt_bodies = []
    for i, belt in enumerate(job.get("belts", ())):
        bb = spec.worldbody.add_body()
        bb.name = f"belt{i}"
        # Nearly frictionless, and winning the friction pick (MuJoCo otherwise takes the
        # larger of the two geoms'): the belt does not move, so its own friction would only
        # hold things back. _apply_belts supplies the grip a moving belt has.
        _add_soup(spec, bb, belt["verts"], belt["tris"], belt["thickness"], bb.name, 0.02, world_solref, priority=1)
        belt_bodies.append(bb.name)
        _low(belt["verts"])
    drivers = list(job.get("drivers", ()))
    for i, drv in enumerate(drivers):
        db = spec.worldbody.add_body()
        db.name = f"drv{i}"
        db.pos = list(map(float, drv["pos"][0]))
        db.quat = list(map(float, drv["quat"][0]))
        # Its state is overwritten every step; the armature only keeps the contacts within a
        # step from denting it.
        db.gravcomp = 1.0
        db.sleep = _mj.mjtSleepPolicy.mjSLEEP_NEVER
        j = db.add_joint()
        j.type = _mj.mjtJoint.mjJNT_FREE
        j.armature = 1e10
        _add_soup(spec, db, drv["verts"], drv["tris"], drv["thickness"], db.name, friction, world_solref)
        _low(np.asarray(drv["verts"]) + drv["pos"][0])
    floor = spec.worldbody.add_body()
    floor.name = "catch_floor"
    floor.pos = list(up * ((lowest if lowest is not None else 0.0) - float(job.get("floor_drop", 0.0)) - 1.0))
    if up_idx == 1:
        floor.quat = [math.cos(math.pi / 4), -math.sin(math.pi / 4), 0.0, 0.0]
    elif up_idx == 0:
        floor.quat = [math.cos(math.pi / 4), 0.0, math.sin(math.pi / 4), 0.0]
    fg = floor.add_geom()
    fg.type = _mj.mjtGeom.mjGEOM_PLANE
    fg.size = [0.0, 0.0, 1.0]
    fg.contype, fg.conaffinity = _WORLD
    fg.friction = [friction, 0.005, 0.0001]
    fg.solref = world_solref

    mesh_name = {}
    for k in {k for k in slot_key}:
        ma = spec.add_mesh()
        ma.name = f"piece{k[0]}_{k[1]}"
        ma.uservert = np.asarray(pieces[k]["points"], dtype=np.float32).reshape(-1)
        ma.maxhullvert = int(job.get("hull_verts", 64))
        mesh_name[k] = ma.name

    # Parking lot: a grid far to the side, spaced so no two parked bodies' boxes touch --
    # a collision-less body still costs a broad-phase pair if its box overlaps another's.
    side = int(math.ceil(math.sqrt(max(1, n_slots))))
    pitch = 2.5 * biggest
    ax_a = np.eye(3)[(up_idx + 1) % 3]
    ax_b = np.eye(3)[(up_idx + 2) % 3]
    k_idx = np.arange(n_slots)
    park_pos = 1e5 * ax_a + np.outer(k_idx % side, ax_a) * pitch + np.outer(k_idx // side, ax_b) * pitch

    density = float(job["density"])
    for s in range(n_slots):
        b = spec.worldbody.add_body()
        b.name = f"s{s}"
        b.pos = list(map(float, park_pos[s]))
        b.sleep = _mj.mjtSleepPolicy.mjSLEEP_INIT
        b.gravcomp = 1.0
        jt = b.add_joint()
        jt.type = _mj.mjtJoint.mjJNT_FREE
        g = b.add_geom()
        g.type = _mj.mjtGeom.mjGEOM_MESH
        g.meshname = mesh_name[slot_key[s]]
        g.density = density
        g.contype, g.conaffinity = _PARKED
        g.friction = [friction, 0.005, 0.0001]
        g.solref = body_solref
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3

    t0 = time.perf_counter()
    model = spec.compile()
    data = _mj.MjData(model)
    build_seconds = time.perf_counter() - t0

    def _bid(name):
        return _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, name)

    slot_body = np.array([_bid(f"s{s}") for s in range(n_slots)], dtype=np.int64)
    body_slot = -np.ones(model.nbody, dtype=np.int64)
    body_slot[slot_body] = np.arange(n_slots)
    slot_qadr = model.jnt_qposadr[model.body_jntadr[slot_body]] if n_slots else np.zeros(0, dtype=np.int64)
    slot_dadr = model.jnt_dofadr[model.body_jntadr[slot_body]] if n_slots else np.zeros(0, dtype=np.int64)
    slot_geom = model.body_geomadr[slot_body] if n_slots else np.zeros(0, dtype=np.int64)
    lin_damp = float(job.get("lin_damping", 0.0))
    ang_damp = float(job.get("ang_damping", 0.05))
    for da in slot_dadr:
        model.dof_damping[da:da + 3] = lin_damp
        model.dof_damping[da + 3:da + 6] = ang_damp
    drv_body = [_bid(f"drv{i}") for i in range(len(drivers))]
    drv_qadr = [int(model.jnt_qposadr[model.body_jntadr[b]]) for b in drv_body]
    drv_dadr = [int(model.jnt_dofadr[model.body_jntadr[b]]) for b in drv_body]
    geom_belt = np.full(model.ngeom, -1, dtype=np.int64)
    # Per belt: (vertex positions, per-vertex surface velocity). A contact takes the velocity
    # of the belt's nearest vertex, so a belt whose velocities vary over it -- round a drum,
    # a turntable -- carries each point the way it moves there.
    belt_field = []
    for i, name in enumerate(belt_bodies):
        b = _bid(name)
        geom_belt[model.body_geomadr[b]: model.body_geomadr[b] + model.body_geomnum[b]] = i
        vel = np.asarray(job["belts"][i]["velocity"], dtype=np.float64).reshape(-1, 3)
        pts = np.asarray(job["belts"][i]["verts"], dtype=np.float64)
        if len(vel) != len(pts) or np.ptp(vel, axis=0).max() < 1e-9:
            belt_field.append((None, vel[:1]))
        else:
            belt_field.append((pts, vel))
    has_belts = bool(belt_bodies)
    # A belt pulls whatever it touches towards its speed as hard as friction allows -- the
    # speed-up is over two substeps, i.e. as fast as the step can resolve, and the friction
    # cap is what makes it a grip rather than a push.
    grip_tau = 2.0 * sub_dt

    # ---- breakage thresholds ----
    slot_mass = model.body_mass[slot_body] if n_slots else np.zeros(0)
    slot_vol = np.array([pieces[k]["volume"] for k in slot_key]) if n_slots else np.zeros(0)
    group_start = {}
    for s in range(n_slots):
        group_start.setdefault(int(slot_group[s]), s)
    first = np.array([group_start[int(g)] for g in slot_group], dtype=np.int64) if n_slots else np.zeros(0, dtype=np.int64)
    variation = max(0.0, float(breakage.get("variation", 0.3)))
    jitter = np.clip(1.0 + variation * rng.uniform(-1.0, 1.0, n_slots), 0.05, None)
    strength = float(breakage.get("strength", 0.0))
    threshold = (
        strength * slot_mass[first] * g_mag
        * (slot_vol / np.maximum(slot_vol[first], 1e-12)) ** (2.0 / 3.0) * jitter
    ) if n_slots else np.zeros(0)
    children = [[] for _ in range(n_slots)]
    for s in range(n_slots):
        g, k = int(slot_group[s]), slot_key[s]
        kids = [slot_of.get((g, (k[0], c))) for c in pieces[k]["children"]]
        if kids and all(c is not None for c in kids):
            children[s] = kids
    breakable = np.array([bool(c) for c in children], dtype=bool) if n_slots else np.zeros(0, dtype=bool)
    if strength <= 0.0 or not breakage.get("enabled", True):
        breakable[:] = False
    cooldown = max(0.0, float(breakage.get("cooldown", 0.04)))
    kick = float(breakage.get("kick", 0.0))
    offsets = np.array([pieces[k]["offset"] for k in slot_key], dtype=np.float64).reshape(n_slots, 3)

    alive = np.zeros(n_slots, dtype=bool)
    # A piece moving further than its own size in one substep is past anything the contact
    # solver can resolve -- it tunnels, or, wedged between two driven parts that do not give,
    # it takes an unbounded force and blows up. Capping speed there keeps every body inside
    # what a step can handle. Scaled to each piece, so small gravel is capped lower than a
    # boulder, and generous enough that nothing falling or thrown normally ever reaches it.
    slot_radius = np.array([radius[k] for k in slot_key]) if n_slots else np.zeros(0)
    max_speed = 2.0 * slot_radius / sub_dt
    max_spin = 2.0 / sub_dt
    lin_idx = (slot_dadr[:, None] + np.arange(3)).reshape(-1) if n_slots else np.zeros(0, dtype=np.int64)
    ang_idx = (slot_dadr[:, None] + np.arange(3, 6)).reshape(-1) if n_slots else np.zeros(0, dtype=np.int64)
    recovered = 0
    born = np.zeros(n_slots)

    def _set_collide(s, on):
        # The body's masks too: the broad phase filters on the body's own copy (the OR of
        # its geoms'), which compile fixed and a geom edit does not update.
        ct, ca = _BODY if on else _PARKED
        g, b = slot_geom[s], slot_body[s]
        model.geom_contype[g], model.geom_conaffinity[g] = ct, ca
        model.body_contype[b], model.body_conaffinity[b] = ct, ca
        model.body_gravcomp[b] = 0.0 if on else 1.0

    def _park(s):
        qa, da = slot_qadr[s], slot_dadr[s]
        data.qpos[qa:qa + 3] = park_pos[s]
        data.qpos[qa + 3:qa + 7] = (1.0, 0.0, 0.0, 0.0)
        data.qvel[da:da + 6] = 0.0
        _set_collide(s, False)
        alive[s] = False

    def _place(s, pos, quat, lin, ang_local, t):
        qa, da = slot_qadr[s], slot_dadr[s]
        data.qpos[qa:qa + 3] = pos
        data.qpos[qa + 3:qa + 7] = quat
        data.qvel[da:da + 3] = lin
        data.qvel[da + 3:da + 6] = ang_local
        if not data.qvel[da:da + 6].any():
            data.qvel[da + 3] = 1e-3  # a sleeping body wakes on a non-zero velocity, only that
        _set_collide(s, True)
        alive[s] = True
        born[s] = t

    pending = sorted(range(len(groups)), key=lambda g: groups[g][5])
    delayed = 0

    def _start(gi, t) -> bool:
        nonlocal delayed
        key, pos, quat, lin, ang, when = groups[gi]
        s = slot_of.get((gi, key))
        if s is None:
            return True
        pos = np.asarray(pos, dtype=np.float64)
        if when > 0.0:
            live = np.flatnonzero(alive)
            if len(live):
                others = data.xpos[slot_body[live]]
                other_r = np.array([radius[slot_key[k]] for k in live])
                if np.any(np.linalg.norm(others - pos, axis=1) < other_r + radius[key]):
                    delayed += 1
                    return False
        q = np.asarray(quat, dtype=np.float64)
        _place(s, pos, q, np.asarray(lin, dtype=np.float64),
               _quat_to_mat(q).T @ np.asarray(ang, dtype=np.float64), t)
        return True

    breaks = []
    bad_qacc = _mj.mjtWarning.mjWARN_BADQACC

    def _clamp_speeds():
        if not n_slots:
            return
        lin = data.qvel[lin_idx].reshape(-1, 3)
        speed = np.linalg.norm(lin, axis=1)
        over = speed > max_speed
        if over.any():
            lin[over] *= (max_speed[over] / speed[over])[:, None]
            data.qvel[lin_idx] = lin.reshape(-1)
        ang = data.qvel[ang_idx].reshape(-1, 3)
        spin = np.linalg.norm(ang, axis=1)
        over = spin > max_spin
        if over.any():
            ang[over] *= (max_spin / spin[over])[:, None]
            data.qvel[ang_idx] = ang.reshape(-1)

    def _repair(saved_qpos, saved_qvel, t_now) -> None:
        """Undo a diverged step and deal with the body that diverged.

        MuJoCo names the offending degree of freedom; its body is the piece that took the
        impossible load. If it can break, that is what a load like that means, so it breaks;
        otherwise it is stopped where it was. Everything else resumes from the last good step.
        """
        nonlocal recovered
        dof = int(data.warning[bad_qacc].lastinfo)
        data.qpos[:] = saved_qpos
        data.qvel[:] = saved_qvel
        data.qacc_warmstart[:] = 0.0
        recovered += 1
        s = int(body_slot[model.dof_bodyid[dof]]) if 0 <= dof < model.nv else -1
        if s >= 0 and alive[s]:
            if breakable[s]:
                _break(s, t_now)
            else:
                data.qvel[slot_dadr[s]:slot_dadr[s] + 6] = 0.0
                data.qvel[slot_dadr[s] + 3] = 1e-3
        _mj.mj_forward(model, data)
    peak = np.zeros(model.nbody)

    def _break(s, t):
        qa, da = slot_qadr[s], slot_dadr[s]
        pos = data.qpos[qa:qa + 3].copy()
        quat = data.qpos[qa + 3:qa + 7].copy()
        rot = _quat_to_mat(quat)
        lin = data.qvel[da:da + 3].copy()
        ang_local = data.qvel[da + 3:da + 6].copy()
        ang = rot @ ang_local
        _park(s)
        for c in children[s]:
            off = rot @ offsets[c]
            push = off / max(np.linalg.norm(off), 1e-9) * kick
            _place(c, pos + off, quat, lin + np.cross(ang, off) + push, ang_local, t)
        breaks.append((t, int(s)))

    def _drive(f, frac):
        """Pose every driven collider along its animation, with the animation's velocity."""
        for i, drv in enumerate(drivers):
            P, Q = drv["pos"], drv["quat"]
            f0 = min(f, len(P) - 1)
            f1 = min(f + 1, len(P) - 1)
            q = _slerp(Q[f0], Q[f1], frac)
            qa, da = drv_qadr[i], drv_dadr[i]
            data.qpos[qa:qa + 3] = P[f0] + (P[f1] - P[f0]) * frac
            data.qpos[qa + 3:qa + 7] = q
            w = _angular_velocity(Q[f0], Q[f1], 1.0 / fps) if f1 != f0 else np.zeros(3)
            data.qvel[da:da + 3] = (P[f1] - P[f0]) * fps
            data.qvel[da + 3:da + 6] = _quat_to_mat(q).T @ w

    positions = np.zeros((frames, n_slots, 3), dtype=np.float32)
    orientations = np.zeros((frames, n_slots, 4), dtype=np.float32)
    alive_rec = np.zeros((frames, n_slots), dtype=bool)

    _drive(0, 0.0)
    _mj.mj_forward(model, data)
    t = 0.0
    started = time.perf_counter()
    for f in range(frames):
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        # Every start that is due, in order; one still blocked keeps its place in the queue
        # without holding back the ones behind it.
        waiting = []
        while pending and groups[pending[0]][5] <= t + 1e-9:
            gi = pending.pop(0)
            if not _start(gi, t):
                waiting.append(gi)
        pending[:0] = waiting
        if n_slots:
            _mj.mj_kinematics(model, data)
            positions[f] = data.xpos[slot_body]
            orientations[f] = data.xquat[slot_body]
            alive_rec[f] = alive

        for sub in range(substeps):
            if drivers:
                _drive(f, sub / substeps)
            if has_belts:
                _apply_belts(model, data, geom_belt, belt_field, body_slot, grip_tau, alive, friction)
            saved_qpos = data.qpos.copy()
            saved_qvel = data.qvel.copy()
            warned = data.warning[bad_qacc].number
            _mj.mj_step(model, data)
            t += sub_dt
            if data.warning[bad_qacc].number != warned or not np.isfinite(data.qvel).all():
                _repair(saved_qpos, saved_qvel, t)
                continue
            _clamp_speeds()
            if not breakable.any():
                continue
            ncon = data.ncon
            if not ncon:
                continue
            con = data.contact
            adr = con.efc_address[:ncon]
            ok = adr >= 0
            if not ok.any():
                continue
            fn = data.efc_force[adr[ok]]
            geoms = con.geom[:ncon][ok]
            peak[:] = 0.0
            np.maximum.at(peak, model.geom_bodyid[geoms[:, 0]], fn)
            np.maximum.at(peak, model.geom_bodyid[geoms[:, 1]], fn)
            due = np.flatnonzero(alive & breakable & (peak[slot_body] > threshold) & (t - born >= cooldown))
            for s in due:
                if alive[s]:
                    _break(s, t)
        if progress is not None:
            progress(f, frames)

    gen = np.array([pieces[k]["generation"] for k in slot_key], dtype=np.int64)
    last = alive_rec[-1] if frames and n_slots else np.zeros(n_slots, dtype=bool)
    by_gen = np.bincount(gen[last], minlength=1) if n_slots else np.zeros(1, dtype=np.int64)
    return dict(
        positions=positions,
        orientations=orientations,
        alive=alive_rec,
        slot_key=slot_key,
        slot_generation=gen,
        breaks=breaks,
        fps=fps,
        stats=dict(
            bodies=len(groups),
            slots=n_slots,
            breaks=len(breaks),
            recovered=recovered,
            delayed_starts=delayed,
            pieces_by_generation=by_gen.tolist(),
            build_seconds=build_seconds,
            sim_seconds=time.perf_counter() - started,
        ),
    )


def _apply_belts(model, data, geom_belt, belt_field, body_slot, grip_tau, alive, friction) -> None:
    """Drag bodies resting on a moving surface towards the surface's velocity.

    A moving surface is geometry that stays put while its points say they move -- USD
    ``velocities`` on a static mesh, Houdini's point ``v`` on a static collider. MuJoCo has
    no such thing, and a belt modelled as a moving body would slide off its own rollers. So
    the surface stays put, nearly frictionless, and every body touching it is pushed --
    along the contact plane only -- towards the surface velocity where it touches, capped at
    friction x the contact's normal force: exactly the grip friction gives.
    """
    data.xfrc_applied[:] = 0.0
    ncon = data.ncon
    if not ncon:
        return
    con = data.contact
    geoms = con.geom[:ncon]
    b0 = geom_belt[geoms[:, 0]]
    b1 = geom_belt[geoms[:, 1]]
    hit = (b0 >= 0) ^ (b1 >= 0)
    if not hit.any():
        return
    idx = np.flatnonzero(hit)
    belt = np.where(b0[idx] >= 0, b0[idx], b1[idx])
    other = np.where(b0[idx] >= 0, geoms[idx, 1], geoms[idx, 0])
    bodies = model.geom_bodyid[other]
    slots = body_slot[bodies]
    keep = slots >= 0
    keep[keep] = alive[slots[keep]]
    if not keep.any():
        return
    idx, belt, bodies = idx[keep], belt[keep], bodies[keep]
    normals = con.frame[:ncon][idx, :3]
    points = con.pos[:ncon][idx]
    adr = con.efc_address[:ncon][idx]
    normal_force = np.where(adr >= 0, data.efc_force[np.maximum(adr, 0)], 0.0)
    for b in np.unique(bodies):
        rows = np.flatnonzero(bodies == b)
        k = rows[0]
        da = model.jnt_dofadr[model.body_jntadr[b]]
        n = normals[k]
        pts, vel = belt_field[belt[k]]
        if pts is None:
            surface = vel[0]
        else:
            surface = vel[int(np.argmin(np.einsum("ij,ij->i", pts - points[k], pts - points[k])))]
        dv = surface - data.qvel[da:da + 3]
        dv = dv - n * (dv @ n)
        force = model.body_mass[b] * dv / grip_tau
        cap = friction * float(normal_force[rows].sum())
        mag = float(np.linalg.norm(force))
        if mag > cap > 0.0:
            force *= cap / mag
        data.xfrc_applied[b, :3] = force


# ---------------------------------------------------------------------------
# Reading the scene (main thread)
# ---------------------------------------------------------------------------


def read_fracture(stage, instancer_prim) -> dict:
    """The fracture tree behind an instancer's prototypes, or {} if it has none.

    ``{"pieces": {(tree, node): piece}, "proto": {(tree, node): prototype index},
    "proto_key": [(tree, node) or None, per prototype]}``. A piece is a dict of hull points
    (about its centre), children, offset, volume and generation.
    """
    inst = UsdGeom.PointInstancer(instancer_prim)
    if not inst:
        return {}
    pieces, proto, keys = {}, {}, []
    for i, target in enumerate(inst.GetPrototypesRel().GetTargets()):
        p = stage.GetPrimAtPath(target)
        tree_attr = p.GetAttribute(NS + "tree") if p else None
        if not tree_attr or not tree_attr.HasValue():
            keys.append(None)
            continue
        key = (int(tree_attr.Get()), int(p.GetAttribute(NS + "node").Get()))
        pts = []
        for q in Usd.PrimRange(p):
            m = UsdGeom.Mesh(q)
            if m and (v := m.GetPointsAttr().Get()):
                pts.append(np.asarray(v, dtype=np.float64))
        pieces[key] = dict(
            points=np.concatenate(pts) if pts else np.zeros((0, 3)),
            children=[int(c) for c in (p.GetAttribute(NS + "children").Get() or [])],
            offset=np.asarray(p.GetAttribute(NS + "offset").Get() or (0, 0, 0), dtype=np.float64),
            volume=float(p.GetAttribute(NS + "volume").Get() or 0.0),
            generation=int(p.GetAttribute(NS + "generation").Get() or 0),
        )
        proto[key] = i
        keys.append(key)
    if not pieces:
        return {}
    return dict(pieces=pieces, proto=proto, proto_key=keys)


def _mesh_world(prim, xfc):
    mesh = UsdGeom.Mesh(prim)
    pts = np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get() or [], dtype=np.int64)
    idx = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64)
    tris = []
    k = 0
    for c in counts:
        if c >= 3 and k + c <= len(idx):
            for t in range(1, c - 1):
                tris.append((idx[k], idx[k + t], idx[k + t + 1]))
        k += c
    m = np.array(xfc.GetLocalToWorldTransform(prim), dtype=np.float64)
    world = pts @ m[:3, :3] + m[3, :3] if len(pts) else pts
    return world, np.asarray(tris, dtype=np.int64).reshape(-1, 3)


def _is_animated(prim, stop) -> bool:
    """Whether *prim*'s world transform moves over time: its own ops or an ancestor's."""
    p = prim
    while p and p.IsValid() and p.GetPath() != Sdf.Path.absoluteRootPath:
        xf = UsdGeom.Xformable(p)
        if xf and xf.TransformMightBeTimeVarying():
            return True
        if p == stop:
            break
        p = p.GetParent()
    return False


def _rigid(m: Gf.Matrix4d) -> tuple[np.ndarray, np.ndarray]:
    """(translation, w,x,y,z quaternion) of a matrix, scale and shear removed."""
    t = m.ExtractTranslation()
    q = m.RemoveScaleShear().ExtractRotationQuat()
    i = q.GetImaginary()
    return np.array([t[0], t[1], t[2]]), np.array([q.GetReal(), i[0], i[1], i[2]])


def _joint_spins(stage) -> dict:
    """``{body path: (joint prim, pivot local, axis local, deg/s)}`` for every revolute
    joint with an angular drive asking for a velocity."""
    out = {}
    for p in stage.Traverse():
        if not p.IsA(UsdPhysics.RevoluteJoint):
            continue
        joint = UsdPhysics.RevoluteJoint(p)
        drive = UsdPhysics.DriveAPI(p, "angular")
        vel_attr = drive.GetTargetVelocityAttr() if drive else None
        vel = float(vel_attr.Get()) if vel_attr and vel_attr.HasAuthoredValue() else 0.0
        if not vel:
            continue
        targets = joint.GetBody1Rel().GetTargets()
        local_pos, local_rot = joint.GetLocalPos1Attr().Get(), joint.GetLocalRot1Attr().Get()
        if not targets:
            targets = joint.GetBody0Rel().GetTargets()
            local_pos, local_rot = joint.GetLocalPos0Attr().Get(), joint.GetLocalRot0Attr().Get()
            vel = -vel
        if not targets:
            continue
        axis = {"X": Gf.Vec3d(1, 0, 0), "Y": Gf.Vec3d(0, 1, 0), "Z": Gf.Vec3d(0, 0, 1)}[str(joint.GetAxisAttr().Get() or "X")]
        if local_rot is not None:
            axis = Gf.Rotation(Gf.Quatd(local_rot)).TransformDir(axis)
        pivot = Gf.Vec3d(local_pos) if local_pos is not None else Gf.Vec3d(0, 0, 0)
        out[targets[0]] = (p, pivot, axis.GetNormalized(), vel)
    return out


def gather_world(stage, collider_prims, frames: int, start_frame: int, fps: float,
                 thickness_override: float, body_radius: float, exclude=()) -> dict:
    """Statics, belts and driven colliders under *collider_prims*, in world space.

    Every mesh under the collider prims collides, physics authored or not, so a plain model
    just works. Two things take one out, both stated in the scene: UsdPhysics'
    ``physics:collisionEnabled = false`` on it, or its path in *exclude* (the emitter box).

    A driven collider comes back with its pose per frame (frames + 1 samples) and, when a
    joint drive spins it, what the bake needs to write that spin onto the prim.
    ``world["found"]`` lists what each prim was taken as, for the readout.
    """
    world = dict(statics=[], belts=[], drivers=[], spins=[], found=[])
    excluded = {Sdf.Path(str(e)) for e in exclude if e}
    spins = _joint_spins(stage)
    xfc = UsdGeom.XformCache(Usd.TimeCode(start_frame))
    seen = set()

    def thickness_for(prim, verts):
        if thickness_override > 0.0:
            return thickness_override
        p = prim
        while p and p.IsValid() and p.GetPath() != Sdf.Path.absoluteRootPath:
            a = p.GetAttribute(THICKNESS)
            if a and a.HasValue():
                return float(a.Get())
            p = p.GetParent()
        ext = np.ptp(verts, axis=0) if len(verts) else np.ones(3)
        # Half the thinnest dimension, so a plate's two faces' prisms fill it; no more than
        # half a body, or they poke out of a thick part's far side.
        return float(max(0.05, min(0.5 * float(ext.min() or 1.0), 0.5 * body_radius)))

    def attr_up(prim, name, stop):
        p = prim
        while p and p.IsValid():
            a = p.GetAttribute(name)
            if a and a.HasValue():
                return p, a.Get()
            if p == stop or p.GetPath() == Sdf.Path.absoluteRootPath:
                break
            p = p.GetParent()
        return None, None

    for root in collider_prims:
        for p in Usd.PrimRange(root):
            if not p.IsA(UsdGeom.Mesh) or p.GetPath() in seen:
                continue
            seen.add(p.GetPath())
            if any(p.GetPath().HasPrefix(e) for e in excluded):
                continue
            if p.HasAPI(UsdPhysics.CollisionAPI):
                enabled = UsdPhysics.CollisionAPI(p).GetCollisionEnabledAttr().Get()
                if enabled is not None and not enabled:
                    continue
            verts, tris = _mesh_world(p, xfc)
            if not len(verts) or not len(tris):
                continue
            entry = dict(name=p.GetName(), verts=verts, tris=tris, thickness=thickness_for(p, verts))

            # Driven by a joint on this prim or an ancestor.
            spin = None
            q = p
            while q and q.IsValid() and q.GetPath() != Sdf.Path.absoluteRootPath:
                if q.GetPath() in spins:
                    spin = (q, *spins[q.GetPath()])
                    break
                if q == root:
                    break
                q = q.GetParent()
            if spin is not None:
                body, joint, pivot_l, axis_l, deg_s = spin
                m0 = xfc.GetLocalToWorldTransform(body)
                pivot_w = np.array(m0.Transform(pivot_l))
                axis_w = np.array(m0.TransformDir(axis_l).GetNormalized())
                omega = math.radians(deg_s)
                n = frames + 1
                ts = np.arange(n) / fps
                half = omega * ts * 0.5
                quat = np.column_stack([np.cos(half), np.outer(np.sin(half), axis_w)])
                entry.update(verts=verts - pivot_w, pos=np.repeat(pivot_w[None], n, axis=0), quat=quat)
                world["drivers"].append(entry)
                world["spins"].append(dict(prim=body.GetPath(), pivot=tuple(pivot_l), axis=tuple(axis_l),
                                           omega=omega))
                world["found"].append(("spinning", p.GetName(), f"{deg_s / 6.0:.0f} rpm, {joint.GetName()}"))
                continue

            if _is_animated(p, None):
                mats = [UsdGeom.XformCache(Usd.TimeCode(start_frame + f)).GetLocalToWorldTransform(p)
                        for f in range(frames + 1)]
                m0_inv = mats[0].GetInverse()
                origin = np.array(mats[0].ExtractTranslation())
                pos, quat = [], []
                for m in mats:
                    # The rigid motion since the first frame, applied to the rest geometry.
                    d = m0_inv * m
                    pos.append(np.array(d.Transform(Gf.Vec3d(*origin))))
                    quat.append(_rigid(d)[1])
                entry.update(verts=verts - origin, pos=np.array(pos), quat=np.array(quat))
                world["drivers"].append(entry)
                world["found"].append(("animated", p.GetName(), "follows its transform"))
                continue

            # A moving surface, said the USD way: a mesh that stays put while its points carry
            # velocities -- Houdini's static collider with point v.
            pv = UsdGeom.Mesh(p).GetVelocitiesAttr().Get(start_frame)
            if pv is not None and len(pv) == len(verts):
                pv = np.asarray(pv, dtype=np.float64)
                if np.abs(pv).max() > 1e-9:
                    m = np.array(xfc.GetLocalToWorldTransform(p), dtype=np.float64)
                    entry["velocity"] = pv @ m[:3, :3]
                    world["belts"].append(entry)
                    speed = float(np.linalg.norm(entry["velocity"], axis=1).max())
                    world["found"].append(("moving surface", p.GetName(), f"{speed:.0f} units/s, velocities"))
                    continue
            owner, vel = attr_up(p, SURFACE_VELOCITY, root)
            if owner is not None:
                on = owner.GetAttribute(SURFACE_VELOCITY_ON)
                if not on or not on.HasValue() or on.Get():
                    m = xfc.GetLocalToWorldTransform(owner)
                    entry["velocity"] = np.array(m.TransformDir(Gf.Vec3d(*vel)))
                    world["belts"].append(entry)
                    world["found"].append(("moving surface", p.GetName(),
                                           f"{float(np.linalg.norm(entry['velocity'])):.0f} units/s, PhysX surface velocity"))
                    continue
            world["statics"].append(entry)
            world["found"].append(("static", p.GetName(), ""))
    return world


# ---------------------------------------------------------------------------
# Writing the result
# ---------------------------------------------------------------------------


def _hold_when_parked(values: np.ndarray, alive: np.ndarray) -> np.ndarray:
    """Replace each slot's parked frames with the nearest frame it was alive in.

    A parked body sits in the parking lot, far from the scene. Hidden or not, a position out
    there would stretch the instancer's bounds, and framing the scene would zoom out to the
    lot. Holding the last (or, before it appears, the first) real pose keeps every instance
    where it is, or will be, seen.
    """
    frames, slots = alive.shape
    idx = np.where(alive, np.arange(frames)[:, None], -1)
    last = np.maximum.accumulate(idx, axis=0)
    rev = np.where(alive, np.arange(frames)[:, None], frames)
    first = np.minimum.accumulate(rev[::-1], axis=0)[::-1]
    src = np.where(last >= 0, last, first)
    src = np.where(src >= frames, 0, src)
    return values[src, np.arange(slots)[None, :]]


def author_result(stage, instancer_prim, result, proto_index: dict, start_frame: int, spins=()) -> None:
    """Rewrite the instancer as the simulation's bodies, and bake joint spins.

    One instance per body slot, ids = slot index so ``invisibleIds`` names slots. Positions
    are written in the instancer's own space.
    """
    inst = UsdGeom.PointInstancer(instancer_prim)
    alive = result["alive"]
    frames, n_slots = alive.shape
    keys = result["slot_key"]
    inst.CreateProtoIndicesAttr().Set(Vt.IntArray([proto_index[k] for k in keys]))
    ids = np.arange(n_slots, dtype=np.int64)
    inst.CreateIdsAttr().Set(Vt.Int64Array.FromNumpy(ids))
    for attr in (inst.GetScalesAttr(), inst.GetVelocitiesAttr(), inst.GetAngularVelocitiesAttr()):
        if attr and attr.HasAuthoredValue():
            attr.Block()
    if frames == 0 or n_slots == 0:
        return
    to_local = np.array(UsdGeom.Xformable(instancer_prim).ComputeLocalToWorldTransform(start_frame).GetInverse())
    rot_local = Gf.Matrix4d(to_local.tolist()).RemoveScaleShear().ExtractRotationQuat()
    ri = rot_local.GetImaginary()
    rq = np.array([rot_local.GetReal(), ri[0], ri[1], ri[2]])

    pos = _hold_when_parked(result["positions"], alive).astype(np.float64)
    pos = pos @ to_local[:3, :3] + to_local[3, :3]
    ori = _hold_when_parked(result["orientations"], alive).astype(np.float64)
    if not np.allclose(rq, (1, 0, 0, 0)):
        w1, x1, y1, z1 = rq
        w2, x2, y2, z2 = ori[..., 0], ori[..., 1], ori[..., 2], ori[..., 3]
        ori = np.stack([
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ], axis=-1)
    # MuJoCo is w,x,y,z; Gf.Quath is laid out x,y,z,w.
    ori = np.ascontiguousarray(np.roll(ori, -1, axis=-1), dtype=np.float16)
    pos = np.ascontiguousarray(pos, dtype=np.float32)
    pos_attr = inst.CreatePositionsAttr()
    ori_attr = inst.CreateOrientationsAttr()
    inv_attr = inst.CreateInvisibleIdsAttr()
    for f in range(frames):
        tc = Usd.TimeCode(start_frame + f)
        pos_attr.Set(Vt.Vec3fArray.FromNumpy(pos[f]), tc)
        ori_attr.Set(Vt.QuathArray.FromNumpy(ori[f]), tc)
        inv_attr.Set(Vt.Int64Array.FromNumpy(ids[~alive[f]]), tc)
    # The last frame as the default too: a read at Default() -- a downstream chunk, or a
    # bounds query -- sees where things ended up rather than where they started.
    pos_attr.Set(Vt.Vec3fArray.FromNumpy(pos[-1]))
    ori_attr.Set(Vt.QuathArray.FromNumpy(ori[-1]))
    inv_attr.Set(Vt.Int64Array.FromNumpy(ids[~alive[-1]]))

    for spin in spins:
        prim = stage.GetPrimAtPath(spin["prim"])
        if not prim:
            continue
        ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
        order = [op.GetOpName() for op in ops if ":rbdSpin" not in op.GetOpName()]
        UsdGeom.XformOp(prim.CreateAttribute("xformOp:translate:rbdSpinPivot", Sdf.ValueTypeNames.Double3)).Set(
            Gf.Vec3d(*spin["pivot"]))
        rot = UsdGeom.XformOp(prim.CreateAttribute("xformOp:orient:rbdSpin", Sdf.ValueTypeNames.Quatf))
        axis = Gf.Vec3d(*spin["axis"]).GetNormalized()
        for f in range(frames):
            angle = math.degrees(spin["omega"] * f / result.get("fps", 24.0))
            rot.Set(Gf.Quatf(Gf.Rotation(axis, angle).GetQuat()), Usd.TimeCode(start_frame + f))
        prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray, variability=Sdf.VariabilityUniform).Set(
            Vt.TokenArray(order + ["xformOp:translate:rbdSpinPivot", "xformOp:orient:rbdSpin",
                                   "!invert!xformOp:translate:rbdSpinPivot"]))


# ---------------------------------------------------------------------------
# Running: a worker thread in a UI, inline headless
# ---------------------------------------------------------------------------

if QtCore is not None:

    class _Relay(QtCore.QObject):
        """Runs callables on the thread it was made on -- the main one -- when a worker emits
        them. A queued signal, because QTimer.singleShot fired from a worker thread without a
        main-thread context never runs."""

        call = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.call.connect(self._invoke, QtCore.Qt.QueuedConnection)

        def _invoke(self, fn):
            fn()


_RUNNING = "_rbd_breakage_running"
_CANCEL = "_rbd_breakage_cancel"


def in_background() -> bool:
    """A worker thread only where an event loop can bring the result back to the main
    thread; headless and under test the bake runs inline."""
    if QtCore is None or QtCore.QCoreApplication.instance() is None:
        return False
    return chunk_registry.ChunkRegistry().plugin not in (None, "", "cli", "pytest")


def is_running(node) -> bool:
    return bool(getattr(node, _RUNNING, False))


def cancel(node) -> None:
    setattr(node, _CANCEL, True)


def dispatch(node, job, finish, report) -> None:
    """Simulate *job*, then hand the result to *finish* on the main thread.

    A breakage bake takes tens of seconds; on the main thread the host would stop responding
    for all of it, which reads as a crash.
    """
    setattr(node, _CANCEL, False)
    if not in_background():
        try:
            result = simulate(job)
        except Exception as e:  # noqa: BLE001 - a failed solve reports, it does not take the host down
            logger.exception("Breakable simulation failed")
            report(f"simulation failed: {e}")
            return
        finish(result)
        return
    if is_running(node):
        report("already simulating -- wait, or press Cancel")
        return
    setattr(node, _RUNNING, True)
    relay = _Relay()
    node._rbd_breakage_relay = relay  # the worker's emits need it alive until the last lands
    started = time.perf_counter()

    def progress(f, n):
        if f % 4 == 0 or f == n - 1:
            el = time.perf_counter() - started
            eta = el / (f + 1) * (n - f - 1)
            relay.call.emit(lambda: report(f"simulating... frame {f + 1} of {n} · about {eta:.0f}s left"))

    def work():
        try:
            result = simulate(job, progress=progress, cancel=lambda: getattr(node, _CANCEL, False))
        except Exception as e:  # noqa: BLE001
            cancelled = getattr(node, _CANCEL, False)
            if not cancelled:
                logger.exception("Breakable simulation failed")
            message = "cancelled" if cancelled else f"simulation failed: {e}"

            def failed():
                setattr(node, _RUNNING, False)
                report(message)

            relay.call.emit(failed)
            return

        def done():
            setattr(node, _RUNNING, False)
            try:
                finish(result)
            except Exception as e:  # noqa: BLE001
                logger.exception("Writing the breakable bake failed")
                report(f"writing the result failed: {e}")

        relay.call.emit(done)

    report(f"simulating... frame 0 of {job['frames']}")
    threading.Thread(target=work, name="RBDBreakage", daemon=True).start()


def describe_world(world, emitter_path="") -> str:
    """What the collider prims were taken as, in words -- nothing the solver does to the
    world should be a surprise, so the readout says it."""
    kinds = {}
    for kind, name, note in world["found"]:
        kinds.setdefault(kind, []).append(f"{name} ({note})" if note else name)
    lines = []
    for kind in ("spinning", "animated", "moving surface"):
        if kind in kinds:
            lines.append(f"{kind}: " + ", ".join(kinds[kind]))
    if "static" in kinds:
        lines.append(f"static: {len(kinds['static'])} colliders")
    if emitter_path:
        lines.append(f"emitter: {emitter_path}")
    return "\n".join(lines)


def summary(result) -> str:
    st = result["stats"]
    gens = " ".join(f"{g}:{n}" for g, n in enumerate(st["pieces_by_generation"]) if n)
    text = (f"{st['bodies']} bodies · {st['breaks']} breaks · {st['sim_seconds']:.0f}s\n"
            f"pieces by generation {gens}")
    if st.get("recovered"):
        text += f"\n{st['recovered']} unstable steps rolled back"
    return text
