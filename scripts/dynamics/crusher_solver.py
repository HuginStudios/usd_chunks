"""Crusher Solver chunk -- rigid rock that breaks under load, in MuJoCo.

Rock is dropped into a machine, the machine's moving parts load it, and any rock pushed
harder than it can take is replaced by the two pieces the Rock Library cut it into. The
pieces carry on -- through the machine, onto a belt, into a bucket -- and break again if
they are loaded again. Material is conserved: a rock's two pieces tile it exactly.

How the pieces come and go
--------------------------
MuJoCo cannot add a body to a running simulation; the model would have to be rebuilt and
recompiled at every break. So every piece any rock can break into gets a body up front --
one per (rock dropped, node of its breakage tree) -- and waits *parked*: collisions off,
far from the machine. Dropping a rock unparks its root; breaking one parks it and unparks
its two children exactly where it was, moving as it moved. Those bodies are the output
too: a PointInstancer with one instance per body and one prototype per library piece, the
parked ones switched off through ``invisibleIds``.

What breaks a rock
------------------
The largest contact force on it within a step, against a threshold that scales with the
rock's cross-section -- a piece half the size needs a quarter of the force. Strength is
given in multiples of the dropped rock's own weight, so it reads the same at any scale or
density: 40 means "a load of 40 times its own weight breaks it". A fall onto steel is a
short spike of a few tens of weights; a rock trapped between two rollers that do not slow
down sees whatever it takes to push it through, which is why the nip is where the
crushing happens.

The machine
-----------
Everything under the Machine prim collides. Parts tagged with ``crusher:role`` do more:
``rotor`` parts spin about ``crusher:axis`` at ``crusher:rpm`` with unlimited torque (a
real crusher's flywheel does not notice a rock), ``conveyor`` parts drag what rests on
them towards ``crusher:beltVelocity``, and the ``emitter`` part's bounds are where rock is
dropped from. The Crusher Rig chunk builds a machine with all of that set; any mesh can be
tagged by hand to the same effect.

The rotors' own spin is baked too, onto the rotor prims, so the rollers turn in playback.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_registry, chunk_stage

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

NS = "crusher:"

# Contact bitmasks. MuJoCo collides a pair when either one's contype meets the other's
# conaffinity. Rock collides with rock and with the machine; machine parts never collide
# with each other (a rotor turning inside its cheek plates would otherwise fight them),
# and a parked body collides with nothing at all.
_ROCK = (1, 1)
_MACHINE = (0, 1)
_PARKED = (0, 0)


# ---------------------------------------------------------------------------
# Simulation (pure: numpy in, numpy out -- no USD, safe on a worker thread)
# ---------------------------------------------------------------------------


def _add_soup(spec, body, verts, tris, thickness, name, friction, solref, origin=None, priority=0) -> int:
    """One thin convex prism per triangle -- a collider of any shape, concave or not.

    The same construction the RBD Solver uses for its Mesh colliders: MuJoCo collides a
    mesh geom as its convex hull, so a hopper modelled as one mesh would collide as a solid
    block. Extruding every triangle backwards makes small convex solids whose union is the
    surface itself.
    """
    v = np.asarray(verts, dtype=np.float64)
    if origin is not None:
        v = v - origin
    count = 0
    for i, (a, b, c) in enumerate(np.asarray(tris)):
        v0, v1, v2 = v[a], v[b], v[c]
        n = np.cross(v1 - v0, v2 - v0)
        ln = float(np.linalg.norm(n))
        if ln < 1e-12:
            continue
        back = -n / ln * thickness
        ma = spec.add_mesh()
        ma.name = f"{name}_t{i}"
        ma.uservert = np.array([v0, v1, v2, v0 + back, v1 + back, v2 + back], dtype=np.float32).flatten()
        g = body.add_geom()
        g.type = _mj.mjtGeom.mjGEOM_MESH
        g.meshname = ma.name
        g.contype, g.conaffinity = _MACHINE
        g.friction = [friction, 0.005, 0.0001]
        g.solref = solref
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3
        g.priority = priority
        count += 1
    return count


def _quat_to_mat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _random_quat(rng) -> np.ndarray:
    q = rng.normal(size=4)
    return q / np.linalg.norm(q)


def simulate(job: dict, progress=None, cancel=None) -> dict:
    """Run the crusher. *job* is plain data (see _gather_job); returns plain arrays.

    Result keys: ``positions`` / ``orientations`` (frames x slots x 3 / 4, orientations as
    w,x,y,z), ``alive`` (frames x slots, bool), ``rotor_angles`` (frames x rotors, radians),
    ``slot_rock`` / ``slot_node`` (which library piece each slot is), ``breaks`` and
    ``stats``.
    """
    rng = np.random.default_rng(job.get("seed", 0))
    fps = float(job["fps"])
    frames = int(job["frames"])
    substeps = max(1, int(job["substeps"]))
    sub_dt = 1.0 / fps / substeps
    up = np.zeros(3)
    up[job["up_idx"]] = 1.0
    g_mag = abs(float(job["gravity"]))
    friction = float(job["friction"])
    contact_tc = max(2.0 * sub_dt, 0.002)
    rock_solref = [contact_tc, 1.0 - 0.9 * max(0.0, min(1.0, float(job["restitution"])))]
    machine_solref = [contact_tc, 1.0]

    pieces: dict[int, dict[int, dict]] = job["pieces"]
    rock_ids = sorted(pieces)
    if not rock_ids:
        raise ValueError("the rock library is empty")
    emitter = job["emitter"]
    count = max(0, int(emitter["count"]))
    breakage = job["breakage"]
    max_gen = int(breakage.get("max_generation", 99))

    # ---- which rock each drop is, and when ----
    drop_rock = rng.choice(rock_ids, size=count) if count else np.zeros(0, dtype=int)
    rate = max(1e-6, float(emitter["rate"]))
    drop_time = float(emitter.get("start", 0.0)) + np.arange(count) / rate

    # ---- slots: one body per (drop, node) the drop's rock actually has ----
    slot_drop, slot_node, slot_rock = [], [], []
    slot_of: dict[tuple[int, int], int] = {}
    for e in range(count):
        tree = pieces[int(drop_rock[e])]
        for n in sorted(tree):
            if tree[n]["generation"] > max_gen:
                continue
            slot_of[(e, n)] = len(slot_drop)
            slot_drop.append(e)
            slot_node.append(n)
            slot_rock.append(int(drop_rock[e]))
    n_slots = len(slot_drop)
    slot_drop = np.asarray(slot_drop, dtype=np.int64)
    slot_node = np.asarray(slot_node, dtype=np.int64)
    slot_rock = np.asarray(slot_rock, dtype=np.int64)

    radius = {(r, n): float(np.linalg.norm(p["points"], axis=1).max()) for r, t in pieces.items() for n, p in t.items()}
    biggest = max(radius.values())

    # ---- build the model ----
    spec = _mj.MjSpec()
    spec.option.timestep = sub_dt
    spec.option.gravity = list(up * float(job["gravity"]))
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST
    # Elliptic cones: the first constraint row of a contact is then its normal force, which
    # is the number breakage is judged on.
    spec.option.cone = _mj.mjtCone.mjCONE_ELLIPTIC
    # PGS rather than Newton: a pile of rock is thousands of contact rows, and Newton's cost
    # grows with the square of them -- measured, PGS halves a 12-rock run.
    spec.option.solver = _mj.mjtSolver.mjSOL_PGS
    spec.option.iterations = 40
    # Sleeping bodies cost nothing. The parked pool sleeps from the start and wakes when a
    # rock is dropped or a piece is broken off (a non-zero velocity wakes a body); rock that
    # has come to rest in the bucket goes to sleep by itself, and wakes when hit.
    spec.option.enableflags |= _mj.mjtEnableBit.mjENBL_SLEEP
    spec.option.sleep_tolerance = float(job.get("sleep_tolerance", 0.5))
    spec.memory = int(96 * 1024 * 1024 + n_slots * 48 * 1024)

    lowest = None
    for st in job["statics"]:
        _add_soup(spec, spec.worldbody, st["verts"], st["tris"], st["thickness"], st["name"], friction, machine_solref)
        lo = float(np.min(np.asarray(st["verts"]) @ up))
        lowest = lo if lowest is None else min(lowest, lo)
    belt_names = []
    for i, belt in enumerate(job["belts"]):
        bb = spec.worldbody.add_body()
        bb.name = f"belt{i}"
        # Nearly frictionless: the belt itself does not move, so its own friction would only
        # hold rock back. _apply_belts supplies the grip a moving belt would have. Priority,
        # because MuJoCo otherwise takes the larger of the two geoms' frictions -- the rock's.
        _add_soup(spec, bb, belt["verts"], belt["tris"], belt["thickness"], f"belt{i}", 0.02, machine_solref,
                  priority=1)
        belt_names.append(bb.name)
        lo = float(np.min(np.asarray(belt["verts"]) @ up))
        lowest = lo if lowest is None else min(lowest, lo)
    rotor_names = []
    for i, rot in enumerate(job["rotors"]):
        rb = spec.worldbody.add_body()
        rb.name = f"rotor{i}"
        rb.pos = list(map(float, rot["pivot"]))
        j = rb.add_joint()
        j.type = _mj.mjtJoint.mjJNT_HINGE
        j.axis = list(map(float, rot["axis"]))
        # Unlimited torque, in effect: the rotor's speed is reset every step, and an armature
        # this size means a contact within the step barely dents it either.
        j.armature = 1e12
        _add_soup(spec, rb, rot["verts"], rot["tris"], rot["thickness"], f"rotor{i}", friction,
                  machine_solref, origin=np.asarray(rot["pivot"], dtype=np.float64))
        rotor_names.append(rb.name)
    # A catch-all floor under the lot, so nothing falls forever.
    floor = spec.worldbody.add_body()
    floor.name = "catch_floor"
    floor.pos = list(up * ((lowest if lowest is not None else 0.0) - 1.0))
    if job["up_idx"] == 1:
        a = math.pi / 4
        floor.quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
    elif job["up_idx"] == 0:
        a = math.pi / 4
        floor.quat = [math.cos(a), 0.0, math.sin(a), 0.0]
    fg = floor.add_geom()
    fg.type = _mj.mjtGeom.mjGEOM_PLANE
    fg.size = [0.0, 0.0, 1.0]
    fg.contype, fg.conaffinity = _MACHINE
    fg.friction = [friction, 0.005, 0.0001]
    fg.solref = machine_solref

    for r, tree in pieces.items():
        for n, p in tree.items():
            ma = spec.add_mesh()
            ma.name = f"piece{r}_{n}"
            ma.uservert = np.asarray(p["points"], dtype=np.float32).flatten()

    # Parking lot: a grid far to the side, spaced so no two parked bodies' bounds overlap --
    # a collision-less body still costs a broad-phase pair if its box touches another's.
    side = int(math.ceil(math.sqrt(max(1, n_slots))))
    pitch = 2.5 * biggest
    park_origin = np.array([1.0, 0.0, 0.0]) * 1e5 if job["up_idx"] != 0 else np.array([0.0, 1e5, 0.0])
    lateral = np.array([0.0, 0.0, 1.0]) if job["up_idx"] != 2 else np.array([0.0, 1.0, 0.0])
    park_pos = np.array(
        [park_origin + (np.array([1.0, 0.0, 0.0]) if job["up_idx"] != 0 else np.array([0.0, 1.0, 0.0])) * (k % side) * pitch
         + lateral * (k // side) * pitch for k in range(n_slots)]
    ).reshape(n_slots, 3)

    density = float(job["density"])
    for s in range(n_slots):
        b = spec.worldbody.add_body()
        b.name = f"s{s}"
        b.pos = list(map(float, park_pos[s]))
        # Parked: asleep and weightless, so it stays put without being reset.
        b.sleep = _mj.mjtSleepPolicy.mjSLEEP_INIT
        b.gravcomp = 1.0
        jt = b.add_joint()
        jt.type = _mj.mjtJoint.mjJNT_FREE
        g = b.add_geom()
        g.type = _mj.mjtGeom.mjGEOM_MESH
        g.meshname = f"piece{slot_rock[s]}_{slot_node[s]}"
        g.density = density
        g.contype, g.conaffinity = _PARKED
        g.friction = [friction, 0.005, 0.0001]
        g.solref = rock_solref
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
    rotor_bodies = [_bid(n) for n in rotor_names]
    rotor_dof = np.array([model.jnt_dofadr[model.body_jntadr[b]] for b in rotor_bodies], dtype=np.int64)
    rotor_qadr = np.array([model.jnt_qposadr[model.body_jntadr[b]] for b in rotor_bodies], dtype=np.int64)
    rotor_omega = np.array([float(r["omega"]) for r in job["rotors"]])
    geom_belt = np.full(model.ngeom, -1, dtype=np.int64)
    belt_vel = np.zeros((max(1, len(belt_names)), 3))
    for i, name in enumerate(belt_names):
        b = _bid(name)
        geom_belt[model.body_geomadr[b]: model.body_geomadr[b] + model.body_geomnum[b]] = i
        belt_vel[i] = job["belts"][i]["velocity"]
    grip_tau = max(float(job.get("belt_grip_time", 0.08)), sub_dt)

    # ---- per-slot breakage thresholds ----
    body_mass = model.body_mass
    slot_mass = body_mass[slot_body] if n_slots else np.zeros(0)
    root_mass = np.zeros(count)
    root_vol = np.zeros(count)
    for s in range(n_slots):
        if slot_node[s] == 0:
            root_mass[slot_drop[s]] = slot_mass[s]
    for e in range(count):
        root_vol[e] = pieces[int(drop_rock[e])][0]["volume"]
    slot_vol = np.array([pieces[int(slot_rock[s])][int(slot_node[s])]["volume"] for s in range(n_slots)])
    variation = max(0.0, float(breakage.get("variation", 0.3)))
    jitter = np.clip(1.0 + variation * rng.uniform(-1.0, 1.0, n_slots), 0.05, None)
    strength = float(breakage["strength"])
    size_law = float(breakage.get("size_exponent", 2.0 / 3.0))
    threshold = (
        strength * root_mass[slot_drop] * g_mag
        * (slot_vol / np.maximum(root_vol[slot_drop], 1e-12)) ** size_law * jitter
    ) if n_slots else np.zeros(0)
    children = np.full((n_slots, 2), -1, dtype=np.int64)
    for s in range(n_slots):
        e, n = int(slot_drop[s]), int(slot_node[s])
        a, b = slot_of.get((e, 2 * n + 1)), slot_of.get((e, 2 * n + 2))
        if a is not None and b is not None:
            children[s] = (a, b)
    breakable = children[:, 0] >= 0 if n_slots else np.zeros(0, dtype=bool)
    if strength <= 0.0:
        breakable[:] = False
    cooldown = max(0.0, float(breakage.get("cooldown", 0.05)))
    kick = float(breakage.get("kick", 0.0))
    offsets = np.array(
        [pieces[int(slot_rock[s])][int(slot_node[s])]["offset"] for s in range(n_slots)], dtype=np.float64
    ).reshape(n_slots, 3)

    alive = np.zeros(n_slots, dtype=bool)
    born = np.zeros(n_slots)
    dropped = np.zeros(count, dtype=bool)

    def _set_collide(s, on):
        # Both the geom's masks and its body's: the broad phase filters on the body's own
        # copy (the OR of its geoms'), which compile fixed and a geom edit does not update.
        ct, ca = _ROCK if on else _PARKED
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
            # A sleeping body wakes on a non-zero velocity, and only on that.
            data.qvel[da + 3] = 1e-3
        _set_collide(s, True)
        alive[s] = True
        born[s] = t

    lo = np.asarray(emitter["lo"], dtype=np.float64)
    hi = np.asarray(emitter["hi"], dtype=np.float64)
    drop_vel = np.asarray(emitter.get("velocity", (0.0, 0.0, 0.0)), dtype=np.float64)
    spin = float(emitter.get("spin", 1.0))

    def _try_drop(e, t) -> bool:
        s = slot_of[(e, 0)]
        r_new = radius[(int(drop_rock[e]), 0)]
        live = np.flatnonzero(alive)
        others = data.xpos[slot_body[live]] if len(live) else np.zeros((0, 3))
        other_r = np.array([radius[(int(slot_rock[k]), int(slot_node[k]))] for k in live])
        for _ in range(12):
            pos = lo + (hi - lo) * rng.random(3)
            if not len(live) or np.all(np.linalg.norm(others - pos, axis=1) > (other_r + r_new)):
                _place(s, pos, _random_quat(rng), drop_vel, rng.normal(size=3) * spin, t)
                return True
        return False

    breaks = []
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

    positions = np.zeros((frames, n_slots, 3), dtype=np.float32)
    orientations = np.zeros((frames, n_slots, 4), dtype=np.float32)
    alive_rec = np.zeros((frames, n_slots), dtype=bool)
    rotor_angles = np.zeros((frames, len(rotor_bodies)))

    _mj.mj_forward(model, data)
    t = 0.0
    next_drop = 0
    started = time.perf_counter()
    for f in range(frames):
        if cancel is not None and cancel():
            raise RuntimeError("cancelled")
        # Drops due by now. One that finds no free space waits for the next frame rather
        # than spawning inside another rock.
        while next_drop < count and drop_time[next_drop] <= t + 1e-9:
            if not _try_drop(next_drop, t):
                break
            dropped[next_drop] = True
            next_drop += 1

        if n_slots:
            _mj.mj_kinematics(model, data)
            positions[f] = data.xpos[slot_body]
            orientations[f] = data.xquat[slot_body]
            alive_rec[f] = alive
        if len(rotor_qadr):
            rotor_angles[f] = data.qpos[rotor_qadr]

        for _ in range(substeps):
            if len(rotor_dof):
                data.qvel[rotor_dof] = rotor_omega
            _apply_belts(model, data, geom_belt, belt_vel, body_slot, grip_tau, alive, friction)
            _mj.mj_step(model, data)
            t += sub_dt
            if not n_slots or not breakable.any():
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
            loaded = peak[slot_body]
            due = np.flatnonzero(alive & breakable & (loaded > threshold) & (t - born >= cooldown))
            for s in due:
                if alive[s]:
                    _break(s, t)
        if progress is not None:
            progress(f, frames)

    gen = np.array([pieces[int(slot_rock[s])][int(slot_node[s])]["generation"] for s in range(n_slots)], dtype=np.int64)
    final_alive = alive_rec[-1] if frames else np.zeros(n_slots, dtype=bool)
    by_gen = np.bincount(gen[final_alive], minlength=max_gen + 1 if n_slots else 1) if n_slots else np.zeros(1)
    return dict(
        positions=positions,
        orientations=orientations,
        alive=alive_rec,
        rotor_angles=rotor_angles,
        slot_rock=slot_rock,
        slot_node=slot_node,
        slot_generation=gen,
        breaks=breaks,
        stats=dict(
            slots=n_slots,
            dropped=int(dropped.sum()),
            breaks=len(breaks),
            pieces_by_generation=by_gen.tolist(),
            build_seconds=build_seconds,
            sim_seconds=time.perf_counter() - started,
        ),
    )


def _apply_belts(model, data, geom_belt, belt_vel, body_slot, grip_tau, alive, friction) -> None:
    """Drag rock resting on a belt towards the belt's surface velocity.

    MuJoCo has no moving surfaces, and a belt modelled as a moving body would slide off its
    own rollers. So the belt stays put, nearly frictionless, and every rock touching it is
    pushed -- along the contact plane only -- towards the belt's speed over *grip_tau*
    seconds. The push is capped at friction x the contact's normal force, which is all the
    grip a real belt has: a rock pressed down hard by the pile above it is carried, one
    held back by a wall slips.
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
    adr = con.efc_address[:ncon][idx]
    normal_force = np.where(adr >= 0, data.efc_force[np.maximum(adr, 0)], 0.0)
    for b in np.unique(bodies):
        rows = np.flatnonzero(bodies == b)
        k = rows[0]
        da = model.jnt_dofadr[model.body_jntadr[b]]
        n = normals[k]
        dv = belt_vel[belt[k]] - data.qvel[da:da + 3]
        dv = dv - n * (dv @ n)
        force = model.body_mass[b] * dv / grip_tau
        cap = friction * float(normal_force[rows].sum())
        mag = float(np.linalg.norm(force))
        if mag > cap > 0.0:
            force *= cap / mag
        data.xfrc_applied[b, :3] = force


# ---------------------------------------------------------------------------
# Writing the result
# ---------------------------------------------------------------------------


def _hold_when_parked(values: np.ndarray, alive: np.ndarray) -> np.ndarray:
    """Replace each slot's parked frames with the nearest frame it was alive in.

    A parked body sits in the parking lot, far from the machine. Hidden or not, a position
    out there would still stretch the instancer's bounds -- framing the scene in a viewer
    would zoom out to take in the lot. Holding the last (or, before its drop, the first)
    real pose keeps every instance where it is, or will be, seen.
    """
    frames, slots = alive.shape
    idx = np.where(alive, np.arange(frames)[:, None], -1)
    last = np.maximum.accumulate(idx, axis=0)
    rev = np.where(alive, np.arange(frames)[:, None], frames)
    first = np.minimum.accumulate(rev[::-1], axis=0)[::-1]
    src = np.where(last >= 0, last, first)
    never = src >= frames
    src = np.where(never, 0, src)
    out = values[src, np.arange(slots)[None, :]]
    return out


def _copy_piece_mesh(stage, path, piece) -> None:
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(piece["points"], dtype=np.float32)))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.ascontiguousarray(piece["counts"], dtype=np.int32)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(np.ascontiguousarray(piece["indices"], dtype=np.int32)))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    if piece.get("normals") is not None:
        mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(piece["normals"], dtype=np.float32)))
        mesh.SetNormalsInterpolation(piece.get("normals_interp") or UsdGeom.Tokens.faceVarying)
    mesh.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, piece["colour"]))]))
    pts = np.asarray(piece["points"])
    mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, pts.min(axis=0))), Gf.Vec3f(*map(float, pts.max(axis=0)))]))
    if piece.get("material"):
        from pxr import UsdShade

        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(UsdShade.Material(stage.GetPrimAtPath(piece["material"])))


def author_result(stage, path, result, pieces, start_frame, rotor_prims=(), rotor_frames=()) -> None:
    """Bake *result* as a PointInstancer at *path*, and spin the rotor prims.

    One instance per body slot, in slot order, with ``ids`` set to the slot index so
    ``invisibleIds`` names slots; one prototype per library piece any slot uses. Positions
    are world space: the instancer is written untransformed.

    *rotor_frames* is, per rotor, ``(pivot, axis)`` in the rotor prim's own space; the spin
    is appended to its existing transform as pivot, rotate, un-pivot.
    """
    path = Sdf.Path(str(path))
    alive = result["alive"]
    frames = alive.shape[0]
    slot_rock, slot_node = result["slot_rock"], result["slot_node"]
    used = sorted({(int(r), int(n)) for r, n in zip(slot_rock, slot_node)})
    proto_index = {key: i for i, key in enumerate(used)}

    inst = UsdGeom.PointInstancer.Define(stage, path)
    protos_path = path.AppendChild("Prototypes")
    UsdGeom.Scope.Define(stage, protos_path)
    targets = []
    for r, n in used:
        ppath = protos_path.AppendChild(f"rock{r}_n{n}")
        _copy_piece_mesh(stage, ppath, pieces[r][n])
        targets.append(ppath)
    inst.CreatePrototypesRel().SetTargets(targets)
    n_slots = len(slot_rock)
    inst.CreateProtoIndicesAttr(Vt.IntArray([proto_index[(int(r), int(n))] for r, n in zip(slot_rock, slot_node)]))
    inst.CreateIdsAttr(Vt.Int64Array.FromNumpy(np.arange(n_slots, dtype=np.int64)))
    for name, arr in (("crusher:generation", result.get("slot_generation")),):
        if arr is not None:
            inst.GetPrim().CreateAttribute(name, Sdf.ValueTypeNames.IntArray, custom=True).Set(
                Vt.IntArray.FromNumpy(np.ascontiguousarray(arr, dtype=np.int32))
            )
    if frames == 0 or n_slots == 0:
        return

    pos = _hold_when_parked(result["positions"], alive)
    ori = _hold_when_parked(result["orientations"], alive)
    # MuJoCo writes w,x,y,z; Gf.Quath is laid out x,y,z,w.
    ori = np.ascontiguousarray(np.roll(ori, -1, axis=2), dtype=np.float16)
    pos_attr = inst.CreatePositionsAttr()
    ori_attr = inst.CreateOrientationsAttr()
    inv_attr = inst.CreateInvisibleIdsAttr()
    ids = np.arange(n_slots, dtype=np.int64)
    for f in range(frames):
        tc = Usd.TimeCode(start_frame + f)
        pos_attr.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(pos[f], dtype=np.float32)), tc)
        ori_attr.Set(Vt.QuathArray.FromNumpy(ori[f]), tc)
        inv_attr.Set(Vt.Int64Array.FromNumpy(ids[~alive[f]]), tc)
    # The last frame as the default too, so a read at Default() -- a downstream chunk, or a
    # bounds query -- sees the settled pile rather than nothing.
    pos_attr.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(pos[-1], dtype=np.float32)))
    ori_attr.Set(Vt.QuathArray.FromNumpy(ori[-1]))
    inv_attr.Set(Vt.Int64Array.FromNumpy(ids[~alive[-1]]))

    angles = result.get("rotor_angles")
    for i, prim in enumerate(rotor_prims):
        if angles is None or i >= angles.shape[1]:
            break
        pivot, axis = rotor_frames[i]
        ops = UsdGeom.Xformable(prim).GetOrderedXformOps()
        order = [op.GetOpName() for op in ops if not op.GetOpName().endswith(":crusherPivot")
                 and not op.GetOpName().endswith(":crusherSpin")]
        t_op = UsdGeom.XformOp(prim.CreateAttribute("xformOp:translate:crusherPivot", Sdf.ValueTypeNames.Double3))
        t_op.Set(Gf.Vec3d(*map(float, pivot)))
        spin = UsdGeom.XformOp(prim.CreateAttribute("xformOp:orient:crusherSpin", Sdf.ValueTypeNames.Quatf))
        ax = Gf.Vec3d(*map(float, axis)).GetNormalized()
        for f in range(frames):
            q = Gf.Rotation(ax, math.degrees(float(angles[f, i]))).GetQuat()
            spin.Set(Gf.Quatf(q), Usd.TimeCode(start_frame + f))
        prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray, variability=Sdf.VariabilityUniform).Set(
            Vt.TokenArray(order + ["xformOp:translate:crusherPivot", "xformOp:orient:crusherSpin",
                                   "!invert!xformOp:translate:crusherPivot"])
        )


# ---------------------------------------------------------------------------
# Reading the scene (main thread)
# ---------------------------------------------------------------------------


def read_library(stage, root) -> dict:
    """``{rock: {node: piece}}`` from the pieces a Rock Library wrote under *root*.

    Read from the attributes rather than by importing the library chunk: the two meet in
    USD, so a library written by anything else -- or by hand -- works the same.
    """
    out: dict[int, dict[int, dict]] = {}
    prim = stage.GetPrimAtPath(str(root)) if root else None
    if not prim or not prim.IsValid():
        return out
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        rock_attr = p.GetAttribute(NS + "rock")
        if not mesh or not rock_attr or not rock_attr.HasValue():
            continue
        pts = mesh.GetPointsAttr().Get()
        if not pts:
            continue
        colour = mesh.GetDisplayColorAttr().Get()
        normals = mesh.GetNormalsAttr().Get()
        out.setdefault(int(rock_attr.Get()), {})[int(p.GetAttribute(NS + "node").Get())] = dict(
            points=np.asarray(pts, dtype=np.float64),
            indices=np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64),
            counts=np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64),
            normals=np.asarray(normals, dtype=np.float64) if normals else None,
            normals_interp=mesh.GetNormalsInterpolation(),
            offset=np.asarray(p.GetAttribute(NS + "offset").Get() or (0, 0, 0), dtype=np.float64),
            volume=float(p.GetAttribute(NS + "volume").Get() or 0.0),
            generation=int(p.GetAttribute(NS + "generation").Get() or 0),
            colour=tuple(colour[0]) if colour else (0.45, 0.42, 0.4),
        )
    return out


def _mesh_world(prim, xfc):
    """(world verts, triangles, local-to-world) of one Mesh prim."""
    mesh = UsdGeom.Mesh(prim)
    pts = np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
    counts = mesh.GetFaceVertexCountsAttr().Get() or []
    idx = mesh.GetFaceVertexIndicesAttr().Get() or []
    tris, k = [], 0
    for c in counts:
        if c >= 3 and k + c <= len(idx):
            for t in range(1, c - 1):
                tris.append((idx[k], idx[k + t], idx[k + t + 1]))
        k += c
    m = xfc.GetLocalToWorldTransform(prim)
    mat = np.array(m, dtype=np.float64)
    world = pts @ mat[:3, :3] + mat[3, :3] if len(pts) else pts
    return world, np.asarray(tris, dtype=np.int64).reshape(-1, 3), m


def _auto_thickness(verts: np.ndarray, rock_radius: float) -> float:
    """How deep the solid behind each collider triangle is, when the part does not say.

    Half the part's thinnest dimension -- so the prisms from its two faces meet and fill a
    plate completely -- but no more than half the smallest rock, or they poke out of the
    far side of a thin-walled part and eat into what it holds.
    """
    ext = np.ptp(verts, axis=0) if len(verts) else np.ones(3)
    return float(max(0.05, min(0.5 * float(ext.min() or 1.0), 0.5 * rock_radius)))


def gather_machine(stage, root, rock_radius) -> dict:
    """The machine under *root*: statics, rotors, belts and the emitter box, in world space.

    Also returns, per rotor, its prim path and its (pivot, axis) in its own space -- what
    the bake needs to append the spin to the prim's own transform.

    Hidden parts still collide: hiding a wall is how a cutaway is made, and the rock must
    not fall out through it. ``crusher:collide = false`` leaves a part out.
    """
    machine = dict(statics=[], rotors=[], belts=[], emitter=None, rotor_prims=[], rotor_frames=[])
    prim = stage.GetPrimAtPath(str(root)) if root else None
    if not prim or not prim.IsValid():
        return machine
    xfc = UsdGeom.XformCache(Usd.TimeCode.Default())
    for p in Usd.PrimRange(prim):
        if not p.IsA(UsdGeom.Mesh):
            continue
        collide = p.GetAttribute(NS + "collide")
        if collide and collide.HasValue() and not collide.Get():
            continue
        role_attr = p.GetAttribute(NS + "role")
        role = str(role_attr.Get()) if role_attr and role_attr.HasValue() else "static"
        verts, tris, m = _mesh_world(p, xfc)
        if not len(verts):
            continue
        if role == "emitter":
            machine["emitter"] = (verts.min(axis=0), verts.max(axis=0))
            continue
        if not len(tris):
            continue
        th_attr = p.GetAttribute(NS + "thickness")
        thickness = float(th_attr.Get()) if th_attr and th_attr.HasValue() else _auto_thickness(verts, rock_radius)
        entry = dict(name=p.GetName(), verts=verts, tris=tris, thickness=thickness)
        if role == "rotor":
            axis_attr, rpm_attr, pivot_attr = (p.GetAttribute(NS + n) for n in ("axis", "rpm", "pivot"))
            axis_l = Gf.Vec3d(*(axis_attr.Get() if axis_attr and axis_attr.HasValue() else (1, 0, 0)))
            if pivot_attr and pivot_attr.HasValue():
                pivot_l = Gf.Vec3d(*pivot_attr.Get())
            else:
                pts = np.asarray(UsdGeom.Mesh(p).GetPointsAttr().Get(), dtype=np.float64)
                pivot_l = Gf.Vec3d(*map(float, (pts.min(axis=0) + pts.max(axis=0)) * 0.5))
            axis_w = m.TransformDir(axis_l).GetNormalized()
            rpm = float(rpm_attr.Get()) if rpm_attr and rpm_attr.HasValue() else 30.0
            entry.update(
                pivot=np.array(m.Transform(pivot_l), dtype=np.float64),
                axis=np.array(axis_w, dtype=np.float64),
                omega=rpm * 2.0 * math.pi / 60.0,
            )
            machine["rotors"].append(entry)
            machine["rotor_prims"].append(p.GetPath())
            machine["rotor_frames"].append((tuple(pivot_l), tuple(axis_l.GetNormalized())))
        elif role == "conveyor":
            v_attr = p.GetAttribute(NS + "beltVelocity")
            v_l = Gf.Vec3d(*(v_attr.Get() if v_attr and v_attr.HasValue() else (0, 0, 0)))
            entry["velocity"] = np.array(m.TransformDir(v_l), dtype=np.float64)
            machine["belts"].append(entry)
        else:
            machine["statics"].append(entry)
    return machine


def _promote_bake_layer(layer) -> None:
    """Make this chunk's layer the strongest in its graph layer.

    The rotor spin is appended to the rotor prims' own ``xformOpOrder``, which the machine's
    chunk also authors -- and a chunk layer added earlier wins, so without this the
    machine's order would hide the spin. The same move as the RBD Solver's bake promotion.
    """
    lreg = chunk.get_layer_manager()
    if not lreg or not layer:
        return
    try:
        hierarchy = {}
        for parent, sub in lreg.iter_chunk_layers():
            hierarchy[sub.identifier] = parent
            if sub.identifier != layer.identifier:
                continue
            if sub.identifier in parent.subLayerPaths and parent.subLayerPaths[0] != sub.identifier:
                parent.subLayerPaths.remove(sub.identifier)
                parent.subLayerPaths.insert(0, sub.identifier)
            grand = hierarchy.get(parent.identifier)
            if grand and parent.identifier in grand.subLayerPaths and grand.subLayerPaths[0] != parent.identifier:
                grand.subLayerPaths.remove(parent.identifier)
                grand.subLayerPaths.insert(0, parent.identifier)
            break
    except Exception as e:  # noqa: BLE001 - a failed promotion still leaves a usable bake
        logger.warning("Could not promote the crusher bake layer: %s", e)


# ---------------------------------------------------------------------------
# Running (a worker thread in a UI, inline headless)
# ---------------------------------------------------------------------------

if QtCore is not None:

    class _Relay(QtCore.QObject):
        """Runs callables on the thread it was created on -- the main one -- when a worker
        emits them. A queued signal, because QTimer.singleShot fired from a worker thread
        without a main-thread context never runs at all."""

        call = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.call.connect(self._invoke, QtCore.Qt.QueuedConnection)

        def _invoke(self, fn):
            fn()


#: Set on a chunk while its bake is running, so a second click does not start another.
_RUNNING = "_crusher_sim_running"


def _in_background() -> bool:
    """Simulate on a worker thread only where a running event loop can deliver the result
    back to the main thread. Headless and under test the bake runs inline instead."""
    if QtCore is None or QtCore.QCoreApplication.instance() is None:
        return False
    return chunk_registry.ChunkRegistry().plugin not in (None, "", "cli", "pytest")


def _dispatch(node, job, finish, report) -> None:
    """Simulate *job*, then hand the result to *finish* on the main thread.

    A bake takes tens of seconds; on the main thread usdview would stop responding for all
    of it, which reads as a crash.
    """
    if not _in_background():
        try:
            result = simulate(job)
        except Exception as e:  # noqa: BLE001 - a failed solve reports, it does not take the host down
            logger.exception("Crusher simulation failed")
            report(f"simulation failed: {e}")
            return
        finish(result)
        return
    if getattr(node, _RUNNING, False):
        report("already simulating -- wait for it to finish")
        return
    setattr(node, _RUNNING, True)
    relay = _Relay()
    # Held on the chunk: the worker's emits need the relay alive until the last one lands.
    node._crusher_relay = relay
    frames = int(job["frames"])

    def progress(f, n):
        if f % 6 == 0 or f == n - 1:
            relay.call.emit(lambda: report(f"simulating... frame {f + 1} of {n}"))

    def work():
        try:
            result = simulate(job, progress=progress)
        except Exception as e:  # noqa: BLE001
            logger.exception("Crusher simulation failed")
            message = f"simulation failed: {e}"

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
                logger.exception("Writing the crusher bake failed")
                report(f"writing the result failed: {e}")

        relay.call.emit(done)

    report(f"simulating... frame 0 of {frames}")
    threading.Thread(target=work, name="CrusherSolver", daemon=True).start()


def _run(owner, notice, sender) -> None:
    node = owner.get_chunk()
    readout = node.get_param("readout")

    def report(text):
        if readout is not None:
            readout.value = text
        logger.info("[Crusher Solver] %s", text.replace("\n", " | "))

    if not _HAS_MUJOCO:
        report("mujoco is not installed -- pip install mujoco")
        return
    stage = owner.get_stage()
    layer = owner.get_edit_context()
    if stage is None or layer is None:
        return
    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)

    # Machine and library are read without this chunk's own layer, which holds the last bake
    # -- including the rotor spin, which must not end up baked into the rotors' rest pose.
    scene = owner.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    pieces = read_library(upstream, node.get_param("library_path", is_s).value.strip())
    if not pieces:
        report("no rock library -- add a Rock Library and point Rock Library at its prim")
        return
    rock_radius = min(float(np.linalg.norm(p["points"], axis=1).max()) for t in pieces.values() for p in t.values())
    machine = gather_machine(upstream, node.get_param("machine_path", is_s).value.strip(), rock_radius)
    if not (machine["statics"] or machine["rotors"] or machine["belts"]):
        report("no machine -- point Machine at the prim holding its meshes (a Crusher Rig, or your own)")
        return
    if machine["emitter"] is None:
        report('the machine has no emitter -- tag a box mesh with crusher:role = "emitter"')
        return

    up_idx = {"X": 0, "Y": 1, "Z": 2}.get(UsdGeom.GetStageUpAxis(stage).upper(), 1)
    frames = max(2, node.get_param("frame_count", is_i).value)
    start_frame = node.get_param("start_frame", is_i).value
    up = np.zeros(3)
    up[up_idx] = 1.0
    job = dict(
        up_idx=up_idx,
        gravity=node.get_param("gravity", is_f).value,
        fps=max(1.0, node.get_param("fps", is_f).value),
        frames=frames,
        substeps=max(1, node.get_param("substeps", is_i).value),
        friction=node.get_param("friction", is_f).value,
        restitution=node.get_param("restitution", is_f).value,
        density=max(1e-6, node.get_param("density", is_f).value),
        belt_grip_time=max(1e-3, node.get_param("belt_grip", is_f).value),
        statics=machine["statics"],
        rotors=machine["rotors"],
        belts=machine["belts"],
        pieces=pieces,
        emitter=dict(
            lo=machine["emitter"][0],
            hi=machine["emitter"][1],
            count=max(0, node.get_param("rock_count", is_i).value),
            rate=max(1e-3, node.get_param("drop_rate", is_f).value),
            start=max(0.0, node.get_param("drop_start", is_f).value),
            velocity=-up * node.get_param("drop_speed", is_f).value,
            spin=node.get_param("drop_spin", is_f).value,
        ),
        breakage=dict(
            strength=node.get_param("strength", is_f).value,
            variation=node.get_param("strength_variation", is_f).value,
            cooldown=max(0.0, node.get_param("break_delay", is_f).value),
            kick=node.get_param("break_kick", is_f).value,
            max_generation=max(0, node.get_param("max_generations", is_i).value),
        ),
        seed=node.get_param("seed", is_i).value,
    )
    out_path = Sdf.Path(node.get_param("output_path", is_s).value.strip() or "/CrusherSim/Rocks")
    material = node.get_param("material_path", is_s).value.strip()
    rotor_paths = machine["rotor_prims"]
    rotor_frames = machine["rotor_frames"]

    def finish(result):
        if node.get_edit_context() is None:
            return  # the chunk went away while it was simulating
        if material:
            for tree in pieces.values():
                for p in tree.values():
                    p["material"] = material
        if lreg := chunk.get_layer_manager():
            lreg.clear_layer(layer.identifier)
        with Usd.EditContext(stage, layer):
            if out_path.GetParentPath() != Sdf.Path.absoluteRootPath:
                UsdGeom.Xform.Define(stage, out_path.GetParentPath())
            author_result(
                stage, out_path, result, pieces, start_frame,
                [stage.GetPrimAtPath(p) for p in rotor_paths], rotor_frames,
            )
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.SetStartTimeCode(start_frame)
            stage.SetEndTimeCode(start_frame + frames - 1)
        _promote_bake_layer(layer)
        try:
            host = chunk_registry.ChunkRegistry().scene
            if host is not None and hasattr(host, "refresh_timeline"):
                host.refresh_timeline(start_frame, start_frame + frames - 1)
        except Exception:  # noqa: BLE001 - a timeline refresh is a courtesy
            pass
        st = result["stats"]
        gens = " ".join(f"{g}:{n}" for g, n in enumerate(st["pieces_by_generation"]) if n)
        report(
            f"{st['dropped']} rocks dropped · {st['breaks']} breaks · {st['sim_seconds']:.0f}s\n"
            f"pieces by generation {gens}"
        )
        node.unset_state(chunk.ChunkFlags.State.STALE)

    _dispatch(node, job, finish, report)


def _is_headless() -> bool:
    """No UI: no event loop, and nobody to press Run."""
    return chunk_registry.ChunkRegistry().plugin in (None, "", "cli")


def execute(node) -> None:
    """Bake on cook -- headless only.

    Interactively a bake is tens of seconds, so it waits for Run (the node's stale border
    says when it is out of date). Under ``usdchunks run`` nobody can press anything, and a
    network that cooked without baking would render an empty machine that looks entirely
    plausible -- the same reason the RBD Solver auto-simulates headless.
    """
    if _is_headless():
        _run(node, None, None)


def _reset(owner, notice, sender) -> None:
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)
    owner.get_chunk().set_state(chunk.ChunkFlags.State.STALE)


#: Controls, not simulation inputs: touching them must not mark the bake stale.
_NOT_INPUTS = {"run_sim", "reset_sim", "readout"}


def _mark_stale(owner, notice, sender) -> None:
    owner.get_chunk().set_state(chunk.ChunkFlags.State.STALE)


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Crusher Solver")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    def section(name, label):
        grp = chunk.Group(name, label, flags=_GF)
        grp.set_state(chunk.ChunkFlags.State.COLLAPSED)
        return grp

    chunk_api.add_param(chunk.LineEdit(
        "machine_path", "Machine", default_value="/CrusherRig", flags=labeled,
        tooltip="The prim holding the machine. Every mesh under it collides; parts tagged "
        "crusher:role = rotor / conveyor / emitter spin, carry, or drop the rock.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "library_path", "Rock Library", default_value="/RockLibrary", flags=labeled,
        tooltip="Where a Rock Library wrote its rocks and their pieces.",
    ))
    run_btn = chunk.Button("run_sim", "Run Simulation", tooltip="Simulate and bake.")
    run_btn.add_callback(_run)
    chunk_api.add_param(run_btn)
    reset_btn = chunk.Button("reset_sim", "Reset", tooltip="Clear the bake.")
    reset_btn.add_callback(_reset)
    chunk_api.add_param(reset_btn)
    chunk_api.add_param(chunk.Label("readout", "not simulated yet", default_value="not simulated yet",
                                    tooltip="What the last run produced."))

    rocks = section("Rocks", "Rocks")
    rocks.add_param(chunk.Spinbox("rock_count", "Rock Count", default_value=30, is_int=True,
                                  tooltip="How many rocks are dropped in."))
    rocks.add_param(chunk.Spinbox("drop_rate", "Drop Rate", default_value=4.0, tooltip="Rocks per second."))
    rocks.add_param(chunk.Spinbox("drop_start", "Drop Start", default_value=0.0,
                                  tooltip="Seconds into the simulation before the first rock drops."))
    rocks.add_param(chunk.Spinbox("drop_speed", "Drop Speed", default_value=100.0,
                                  tooltip="Downward speed a rock is dropped with, scene units/s."))
    rocks.add_param(chunk.Spinbox("drop_spin", "Drop Spin", default_value=1.0,
                                  tooltip="Random tumble a rock is dropped with, radians/s."))
    rocks.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    chunk_api.add_param(rocks)

    brk = section("Breakage", "Breakage")
    brk.add_param(chunk.Spinbox(
        "strength", "Strength", default_value=40.0,
        tooltip="The load that breaks a rock, in multiples of its own weight. A fall onto "
        "steel is a spike of a few tens; a rock trapped between the rollers sees as much as "
        "it takes to get through. A piece half the size needs a quarter of the load.\n"
        "0 = nothing breaks.",
    ))
    brk.add_param(chunk.Slider("strength_variation", "Variation", min_value=0.0, max_value=0.9,
                               default_value=0.3, tooltip="Each piece's strength varies by ± this fraction."))
    brk.add_param(chunk.Spinbox("max_generations", "Max Generations", default_value=8, is_int=True,
                                tooltip="Stop breaking past this generation, even where the library goes further."))
    brk.add_param(chunk.Spinbox("break_delay", "Break Delay", default_value=0.04,
                                tooltip="Seconds a fresh piece holds before it can break again, so one "
                                "blow cannot shatter a rock to gravel within a single step."))
    brk.add_param(chunk.Spinbox("break_kick", "Break Kick", default_value=10.0,
                                tooltip="Speed the two pieces spring apart with, scene units/s."))
    chunk_api.add_param(brk)

    phys = section("Physics", "Physics")
    phys.add_param(chunk.Slider("friction", "Friction", min_value=0.0, max_value=1.0, default_value=0.6))
    phys.add_param(chunk.Slider("restitution", "Bounce", min_value=0.0, max_value=1.0, default_value=0.1))
    phys.add_param(chunk.Spinbox("density", "Density", default_value=2.6,
                                 tooltip="Rock density; 2.6 is granite in g/cm³. Strength is relative to "
                                 "weight, so this mostly matters against the belt's grip."))
    phys.add_param(chunk.Spinbox("belt_grip", "Belt Grip Time", default_value=0.08,
                                 tooltip="Seconds for a rock on a conveyor to come up to belt speed."))
    chunk_api.add_param(phys)

    sim = section("Simulation", "Simulation")
    sim.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True))
    sim.add_param(chunk.Spinbox("frame_count", "Frame Count", default_value=240, is_int=True))
    sim.add_param(chunk.Spinbox("fps", "FPS", default_value=24.0))
    sim.add_param(chunk.Spinbox("substeps", "Substeps", default_value=10, is_int=True,
                                tooltip="Physics steps per frame. Fewer is faster; too few and fast "
                                "rock tunnels through thin plates."))
    sim.add_param(chunk.Spinbox("gravity", "Gravity", default_value=-981.0,
                                tooltip="Along the stage's up axis, scene units/s². -981 = centimetres."))
    chunk_api.add_param(sim)

    out = section("Output", "Output")
    out.add_param(chunk.LineEdit("output_path", "Instancer", default_value="/CrusherSim/Rocks", flags=labeled,
                                 tooltip="Where the baked rock PointInstancer is written."))
    out.add_param(chunk.LineEdit("material_path", "Material", default_value="", flags=labeled,
                                 tooltip="Optional material bound to every rock prototype."))
    chunk_api.add_param(out)

    for p in chunk_api.get_params():
        if getattr(p, "name", None) not in _NOT_INPUTS:
            p.add_callback(_mark_stale, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)
    chunk_api.set_state(chunk.ChunkFlags.State.STALE)
    return chunk_api


def config() -> chunk.DefaulConfig:
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.DYNAMICS,
        description=(
            "Rock breakage in MuJoCo: rock dropped into a machine breaks under load into the\n"
            "pieces a Rock Library cut it into, and is carried on by rotors and conveyors.\n"
            "Bakes a PointInstancer. Requires the 'mujoco' package."
        ),
        requirements=["mujoco>=3.1,<4", "numpy>=2,<3"],
    )
