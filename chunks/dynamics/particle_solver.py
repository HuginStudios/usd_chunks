"""Particle Solver chunk -- water, sand, snow and jelly, as an MLS-MPM simulation in Taichi.

Built to sit between the scatter tools and the RBD Solver rather than beside them:

* **Source** says where the material starts. A mesh has its volume filled; a Scatter Brush's
  Painted Points each become a ball of material; a Scatter Points instancer turns every
  instance into a ball the size of that instance.
* **Colliders** are static meshes, plus anything animated -- the RBD Solver's baked
  instancer, or a baked prim -- as moving spheres. Bake the rigid bodies first and they
  shove the material around as they fall into it (one-way: the material does not push back).
* **Output** is a time-sampled Points prim of every particle, and a Surface Points prim of
  the final frame, thinned to a spacing, which wires straight into Scatter Points' Painted
  Points input: pour sand, let it settle, scatter rocks on what it settled into.
* **Drive Instancer** animates a Scatter Points instancer with the flow. If it is the same
  instancer the material came from, each instance follows its own ball (jelly apples);
  otherwise the instances are carried by the material as passive debris (leaves on water).

Everything runs in a unit cube -- the Domain -- that is mapped onto the scene, so gravity
and stiffness are rescaled rather than the solver retuned per scene scale.

Requires: pxr (OpenUSD), usd_chunks, taichi, numpy.
"""

# ruff: noqa: I002
# No `from __future__ import annotations`: it turns the kernels' `ti.f32` hints into strings,
# which Taichi refuses to compile.

import importlib.util
import logging
import math
import sys
import time

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk, chunk_registry, chunk_stage, sim_bake, chunk_utils

logger = logging.getLogger(__name__)


#: taichi, once `_import_taichi` has run. Not imported when the chunk loads: that takes 2-4 s,
#: and Blender loads every chunk to make its node types, so each New Chunk Network paid it
#: with no sim in sight. A bake solves in its own process and imports it there.
ti = None
#: Whether taichi is installed. Looking up its spec imports nothing.
_HAS_TAICHI = importlib.util.find_spec("taichi") is not None


def _import_taichi():
    """Import taichi into `ti`, once, retrying from clean if it comes back half-built.

    Importing it runs taichi's own logger, which asks `inspect` which module it was called
    from -- and `inspect` walks sys.modules, touching every chunk module the registry
    loaded lazily and executing them on the spot. One that raises (a module with a relative
    import, say) aborts taichi's import part-way, and what is left in sys.modules is a
    `taichi` with no `lang` attribute that every later import happily returns. Dropping the
    partial modules and importing again gets a whole one.

    Returns:
        The taichi module, or None if it cannot be imported.
    """
    global ti
    if ti is not None:
        return ti
    try:
        import taichi
        ti = taichi
    except Exception:  # noqa: BLE001 - a half-built taichi raises AttributeError, not ImportError
        for name in [n for n in sys.modules if n == "taichi" or n.startswith("taichi.")]:
            del sys.modules[name]
        try:
            import taichi
            ti = taichi
        except Exception:  # noqa: BLE001
            logger.warning("taichi could not be imported; the Particle Solver cannot simulate",
                           exc_info=True)
    return ti


#: Cached by `_mpm_class`, which compiles nothing -- it only defines the kernels.
_MPM_CLASS = None

WATER, SAND, SNOW, JELLY = "Water", "Sand", "Snow", "Jelly"
_MATERIALS = [WATER, SAND, SNOW, JELLY]
_MATERIAL_IDS = {WATER: 0, SAND: 1, SNOW: 2, JELLY: 3}

#: Young's modulus in domain units, where gravity is ~9.81 for a one-metre domain. Water's is
#: a bulk modulus rather than an elastic one; it is the figure Taichi's own mpm3d uses.
_DEFAULT_STIFFNESS = {WATER: 400.0, SAND: 5000.0, SNOW: 5000.0, JELLY: 5000.0}

_COLOURS = {
    WATER: (0.2, 0.45, 0.9),
    SAND: (0.76, 0.62, 0.38),
    SNOW: (0.92, 0.95, 1.0),
    JELLY: (0.9, 0.35, 0.55),
}

_AS_POINTS = "Points"
_AS_SPHERES = "Instanced Spheres"
_RENDER_MODES = [_AS_POINTS, _AS_SPHERES]

_POISSON = 0.2
_CFL = 0.4

#: Grid cells of solid wall on every face of the domain.
_BOUND = 3

#: How far from a collider's surface its normal is written onto the grid, in cells. Particles
#: are stopped by the nearest grid nodes, so they come to rest roughly this far off the surface.
_COLLIDER_BAND = 1.5


# ---------------------------------------------------------------------------
# the solver -- runs in [0, 1]^3, knows nothing about USD
# ---------------------------------------------------------------------------

def _mpm_class():
    """The solver class, built the first time it is needed.

    A module-level `@ti.data_oriented class` would need taichi imported at module scope --
    see `ti`.
    """
    global _MPM_CLASS
    if _MPM_CLASS is not None:
        return _MPM_CLASS

    @ti.data_oriented
    class _MPM:
        """One simulation's worth of Taichi fields and kernels.

        Built fresh per run: `ti.init` resets the runtime, which frees every field, so the
        fields are members of an instance rather than module globals that would dangle.
        """

        def __init__(self, n_particles, n_grid, n_tracers, n_spheres, material, stiffness,
                     friction, sand_angle, gravity):
            self.n_particles = int(n_particles)
            self.n_grid = int(n_grid)
            self.n_tracers = int(n_tracers)
            self.n_spheres = int(n_spheres)
            self.material = int(material)
            self.dx = 1.0 / n_grid
            self.inv_dx = float(n_grid)
            self.p_vol = (self.dx * 0.5) ** 3
            self.p_mass = self.p_vol
            self.E = float(stiffness)
            self.mu0 = self.E / (2.0 * (1.0 + _POISSON))
            self.la0 = self.E * _POISSON / ((1.0 + _POISSON) * (1.0 - 2.0 * _POISSON))
            sin_phi = math.sin(math.radians(sand_angle))
            self.alpha = math.sqrt(2.0 / 3.0) * 2.0 * sin_phi / (3.0 - sin_phi)
            self.friction = float(friction)
            self.gx, self.gy, self.gz = (float(g) for g in gravity)
            self.lo = self.dx
            self.hi = 1.0 - 2.0 * self.dx

            n = max(self.n_particles, 1)
            self.x = ti.Vector.field(3, ti.f32, n)
            self.v = ti.Vector.field(3, ti.f32, n)
            self.C = ti.Matrix.field(3, 3, ti.f32, n)
            self.F = ti.Matrix.field(3, 3, ti.f32, n)
            self.Jp = ti.field(ti.f32, n)

            shape = (self.n_grid,) * 3
            self.grid_v = ti.Vector.field(3, ti.f32, shape)
            self.grid_m = ti.field(ti.f32, shape)
            self.solid_n = ti.Vector.field(3, ti.f32, shape)
            self.solid_n2 = ti.Vector.field(3, ti.f32, shape)
            self.dyn_on = ti.field(ti.i32, shape)
            self.dyn_n = ti.Vector.field(3, ti.f32, shape)
            self.dyn_v = ti.Vector.field(3, ti.f32, shape)

            self.sph = ti.Vector.field(4, ti.f32, max(self.n_spheres, 1))
            self.sph_v = ti.Vector.field(3, ti.f32, max(self.n_spheres, 1))

            t = max(self.n_tracers, 1)
            self.tx = ti.Vector.field(3, ti.f32, t)
            self.tv = ti.Vector.field(3, ti.f32, t)
            self.tq = ti.Vector.field(4, ti.f32, t)

        @ti.kernel
        def reset_state(self):
            for p in range(self.n_particles):
                self.F[p] = ti.Matrix.identity(ti.f32, 3)
                self.C[p] = ti.Matrix.zero(ti.f32, 3, 3)
                # Sand keeps a log-volume correction that starts at zero; everything else a
                # volume ratio that starts at one.
                if ti.static(self.material == 1):
                    self.Jp[p] = 0.0
                else:
                    self.Jp[p] = 1.0
            for t in range(self.n_tracers):
                self.tq[t] = ti.Vector([1.0, 0.0, 0.0, 0.0])
                self.tv[t] = ti.Vector.zero(ti.f32, 3)

        @ti.func
        def _project(self, v, n, mu):
            """v with the part heading into a surface of outward normal n removed, and the
            sliding part slowed by Coulomb friction."""
            out = v
            vn = v.dot(n)
            if vn < 0.0:
                vt = v - vn * n
                vt_len = vt.norm()
                scale = 0.0
                if vt_len > 1e-9:
                    scale = ti.max(0.0, 1.0 + mu * vn / vt_len)
                out = vt * scale
            return out

        @ti.func
        def _sand_projection(self, sigma, p):
            """Drucker-Prager return mapping on the singular values (Klar et al. 2016)."""
            out = ti.Matrix.identity(ti.f32, 3)
            eps = ti.Vector([
                ti.log(ti.max(ti.abs(sigma[0, 0]), 1e-4)),
                ti.log(ti.max(ti.abs(sigma[1, 1]), 1e-4)),
                ti.log(ti.max(ti.abs(sigma[2, 2]), 1e-4)),
            ])
            tr = eps.sum() + self.Jp[p]
            eps_hat = eps - tr / 3.0
            eps_hat_norm = eps_hat.norm() + 1e-20
            if tr >= 0.0:
                # Pulled apart: grains separate and carry no stress at all.
                self.Jp[p] = tr
            else:
                self.Jp[p] = 0.0
                delta_gamma = eps_hat_norm + (3.0 * self.la0 + 2.0 * self.mu0) / (
                    2.0 * self.mu0) * tr * self.alpha
                for d in ti.static(range(3)):
                    out[d, d] = ti.exp(eps[d] - ti.max(0.0, delta_gamma) / eps_hat_norm * eps_hat[d])
            return out

        @ti.kernel
        def raster_spheres(self, t: ti.f32):
            """Stamp the moving spheres onto the grid at time t into the frame."""
            for cell in ti.grouped(self.dyn_on):
                self.dyn_on[cell] = 0
            for s in range(self.n_spheres):
                c = ti.Vector([self.sph[s][0], self.sph[s][1], self.sph[s][2]]) + self.sph_v[s] * t
                r = self.sph[s][3]
                lo = ti.max(ti.cast((c - r) * self.inv_dx, ti.i32), 0)
                hi = ti.min(ti.cast((c + r) * self.inv_dx, ti.i32) + 1, self.n_grid - 1)
                for i in range(lo[0], hi[0] + 1):
                    for j in range(lo[1], hi[1] + 1):
                        for k in range(lo[2], hi[2] + 1):
                            d = ti.Vector([ti.cast(i, ti.f32), ti.cast(j, ti.f32),
                                           ti.cast(k, ti.f32)]) * self.dx - c
                            dist = d.norm()
                            if dist < r:
                                self.dyn_on[i, j, k] = 1
                                self.dyn_n[i, j, k] = d / ti.max(dist, 1e-6)
                                self.dyn_v[i, j, k] = self.sph_v[s]

        @ti.kernel
        def substep(self, dt: ti.f32):
            for cell in ti.grouped(self.grid_m):
                self.grid_v[cell] = ti.Vector.zero(ti.f32, 3)
                self.grid_m[cell] = 0.0

            # Particle to grid.
            for p in range(self.n_particles):
                Xp = self.x[p] * self.inv_dx
                base = ti.cast(Xp - 0.5, ti.i32)
                fx = Xp - ti.cast(base, ti.f32)
                w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1.0) ** 2, 0.5 * (fx - 0.5) ** 2]
                affine = self.p_mass * self.C[p]
                if ti.static(self.material == 0):
                    pressure = -dt * 4.0 * self.E * self.p_vol * (self.Jp[p] - 1.0) * self.inv_dx * self.inv_dx
                    affine += ti.Matrix.identity(ti.f32, 3) * pressure
                else:
                    F = (ti.Matrix.identity(ti.f32, 3) + dt * self.C[p]) @ self.F[p]
                    U, sig, V = ti.svd(F)
                    stress = ti.Matrix.zero(ti.f32, 3, 3)
                    if ti.static(self.material == 1):
                        sig = self._sand_projection(sig, p)
                        F = U @ sig @ V.transpose()
                        log_sum = 0.0
                        centre = ti.Matrix.zero(ti.f32, 3, 3)
                        for d in ti.static(range(3)):
                            log_sum += ti.log(sig[d, d])
                            centre[d, d] = 2.0 * self.mu0 * ti.log(sig[d, d]) / sig[d, d]
                        for d in ti.static(range(3)):
                            centre[d, d] += self.la0 * log_sum / sig[d, d]
                        stress = U @ centre @ V.transpose() @ F.transpose()
                    else:
                        # Fixed corotated; snow adds the Stomakhin plasticity clamp and hardens
                        # as it compacts.
                        h = 0.3
                        if ti.static(self.material == 2):
                            h = ti.min(5.0, ti.max(0.1, ti.exp(10.0 * (1.0 - self.Jp[p]))))
                        mu = self.mu0 * h
                        la = self.la0 * h
                        J = 1.0
                        for d in ti.static(range(3)):
                            new_sig = sig[d, d]
                            if ti.static(self.material == 2):
                                new_sig = ti.min(ti.max(sig[d, d], 1.0 - 2.5e-2), 1.0 + 4.5e-3)
                                self.Jp[p] *= sig[d, d] / new_sig
                            sig[d, d] = new_sig
                            J *= new_sig
                        if ti.static(self.material == 2):
                            F = U @ sig @ V.transpose()
                        stress = 2.0 * mu * (F - U @ V.transpose()) @ F.transpose() + \
                            ti.Matrix.identity(ti.f32, 3) * la * J * (J - 1.0)
                    self.F[p] = F
                    affine += (-dt * self.p_vol * 4.0 * self.inv_dx * self.inv_dx) * stress
                for offset in ti.static(ti.grouped(ti.ndrange(3, 3, 3))):
                    dpos = (ti.cast(offset, ti.f32) - fx) * self.dx
                    weight = w[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2]
                    self.grid_v[base + offset] += weight * (self.p_mass * self.v[p] + affine @ dpos)
                    self.grid_m[base + offset] += weight * self.p_mass

            # Grid velocities, forces and boundaries.
            for cell in ti.grouped(self.grid_m):
                m = self.grid_m[cell]
                if m > 0.0:
                    v = self.grid_v[cell] / m
                    v += dt * ti.Vector([self.gx, self.gy, self.gz])
                    if ti.static(self.n_spheres > 0):
                        if self.dyn_on[cell] == 1:
                            sv = self.dyn_v[cell]
                            v = self._project(v - sv, self.dyn_n[cell], self.friction) + sv
                    # Static colliders after the moving ones, so they have the last word: the
                    # other way round, a rigid body landing in a pool shoved water through the
                    # floor beneath it.
                    n = self.solid_n[cell]
                    if n.norm_sqr() > 0.25:
                        v = self._project(v, n, self.friction)
                    n2 = self.solid_n2[cell]
                    if n2.norm_sqr() > 0.25:
                        v = self._project(v, n2, self.friction)
                    for d in ti.static(range(3)):
                        if cell[d] < _BOUND:
                            wall = ti.Vector.zero(ti.f32, 3)
                            wall[d] = 1.0
                            v = self._project(v, wall, self.friction)
                        if cell[d] > self.n_grid - _BOUND:
                            wall = ti.Vector.zero(ti.f32, 3)
                            wall[d] = -1.0
                            v = self._project(v, wall, self.friction)
                    self.grid_v[cell] = v

            # Grid to particle.
            for p in range(self.n_particles):
                Xp = self.x[p] * self.inv_dx
                base = ti.cast(Xp - 0.5, ti.i32)
                fx = Xp - ti.cast(base, ti.f32)
                w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1.0) ** 2, 0.5 * (fx - 0.5) ** 2]
                new_v = ti.Vector.zero(ti.f32, 3)
                new_C = ti.Matrix.zero(ti.f32, 3, 3)
                for offset in ti.static(ti.grouped(ti.ndrange(3, 3, 3))):
                    dpos = ti.cast(offset, ti.f32) - fx
                    weight = w[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2]
                    g_v = self.grid_v[base + offset]
                    new_v += weight * g_v
                    new_C += 4.0 * self.inv_dx * weight * g_v.outer_product(dpos)
                self.v[p] = new_v
                self.C[p] = new_C
                self.x[p] = ti.min(ti.max(self.x[p] + dt * new_v, self.lo), self.hi)
                if ti.static(self.material == 0):
                    self.Jp[p] = ti.max(self.Jp[p] * (1.0 + dt * new_C.trace()), 0.1)

        @ti.kernel
        def advect_tracers(self, dt: ti.f32):
            """Carry passive points with the material, spinning them with its local curl.

            A tracer out of the material -- dropped from above, or left behind by a wave --
            falls under gravity and stops on colliders, rather than hanging in the air.
            """
            for t in range(self.n_tracers):
                Xp = self.tx[t] * self.inv_dx
                base = ti.cast(Xp - 0.5, ti.i32)
                fx = Xp - ti.cast(base, ti.f32)
                w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1.0) ** 2, 0.5 * (fx - 0.5) ** 2]
                vel = ti.Vector.zero(ti.f32, 3)
                grad = ti.Matrix.zero(ti.f32, 3, 3)
                cover = 0.0
                for offset in ti.static(ti.grouped(ti.ndrange(3, 3, 3))):
                    weight = w[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2]
                    if self.grid_m[base + offset] > 0.0:
                        g_v = self.grid_v[base + offset]
                        vel += weight * g_v
                        grad += 4.0 * self.inv_dx * weight * g_v.outer_product(ti.cast(offset, ti.f32) - fx)
                        cover += weight
                if cover > 0.3:
                    vel /= cover
                    grad /= cover
                    self.tv[t] = vel
                    omega = 0.5 * ti.Vector([grad[2, 1] - grad[1, 2],
                                             grad[0, 2] - grad[2, 0],
                                             grad[1, 0] - grad[0, 1]])
                    q = self.tq[t]
                    dq = 0.5 * ti.Vector([
                        -omega[0] * q[1] - omega[1] * q[2] - omega[2] * q[3],
                        omega[0] * q[0] + omega[1] * q[3] - omega[2] * q[2],
                        -omega[0] * q[3] + omega[1] * q[0] + omega[2] * q[1],
                        omega[0] * q[2] - omega[1] * q[1] + omega[2] * q[0],
                    ])
                    self.tq[t] = (q + dt * dq).normalized()
                else:
                    tv = self.tv[t] + dt * ti.Vector([self.gx, self.gy, self.gz])
                    node = ti.min(ti.max(ti.cast(Xp + 0.5, ti.i32), 0), self.n_grid - 1)
                    n = self.solid_n[node]
                    if n.norm_sqr() > 0.25:
                        tv = self._project(tv, n, self.friction)
                    self.tv[t] = tv
                moved = self.tx[t] + dt * self.tv[t]
                clamped = ti.min(ti.max(moved, self.lo), self.hi)
                for d in ti.static(range(3)):
                    if clamped[d] != moved[d]:
                        self.tv[t][d] = 0.0
                self.tx[t] = clamped

    _MPM_CLASS = _MPM
    return _MPM


def _stiffness_scale(gravity_n):
    """Stiffen the material when the domain is small enough that gravity is strong in domain
    units, so a ten-centimetre tank of water is not ten times squashier than a one-metre one."""
    return max(1.0, float(np.linalg.norm(gravity_n)) / 9.81)


def _auto_substeps(material, stiffness, gravity_n, init_speed_n, n_grid, fps):
    """Substeps per frame from the CFL condition on the stiffer of sound speed and fall speed."""
    dx = 1.0 / n_grid
    if material == WATER:
        wave = math.sqrt(stiffness)
    else:
        modulus = stiffness * (1.0 - _POISSON) / ((1.0 + _POISSON) * (1.0 - 2.0 * _POISSON))
        wave = math.sqrt(modulus)
    fall = math.sqrt(2.0 * float(np.linalg.norm(gravity_n))) + float(init_speed_n)
    dt = _CFL * dx / max(wave, fall, 1e-6)
    return max(1, int(math.ceil((1.0 / fps) / dt)))


def simulate(particles, velocity, material, *, n_grid=64, stiffness=0.0, friction=0.4,
             sand_angle=30.0, gravity=(0.0, 0.0, -9.81), solid_normals=None,
             sphere_frames=None, tracers=None, frame_count=48, fps=24.0, substeps=0,
             device="GPU", progress=None):
    """Run the simulation in the unit cube and return every frame.

    `particles` is (N, 3) in [0, 1]; `velocity` a single (3,) start velocity. `solid_normals`
    is the grid of static collider normals `_voxelize` builds. `sphere_frames` is one
    (centres, radii, velocities) triple per frame for moving colliders. `tracers` are passive
    points to carry along.

    Returns a dict: `positions` and `velocities` as (frames, N, 3), `tracer_positions`
    (frames, T, 3), `tracer_rotations` (frames, T, 4) as w-x-y-z world-space deltas, and the
    `substeps` actually used. Frame 0 is the starting state.
    """
    if _import_taichi() is None:
        raise RuntimeError("taichi is not installed. Run: pip install taichi")

    particles = np.ascontiguousarray(particles, dtype=np.float32).reshape(-1, 3)
    tracers = np.zeros((0, 3), np.float32) if tracers is None else \
        np.ascontiguousarray(tracers, dtype=np.float32).reshape(-1, 3)
    n_spheres = max((len(f[0]) for f in sphere_frames), default=0) if sphere_frames else 0

    gravity = np.asarray(gravity, dtype=np.float64)
    stiffness = (stiffness or _DEFAULT_STIFFNESS[material]) * _stiffness_scale(gravity)
    velocity = np.asarray(velocity, dtype=np.float32).reshape(3)
    if substeps <= 0:
        substeps = _auto_substeps(material, stiffness, gravity, np.linalg.norm(velocity), n_grid, fps)
    dt = (1.0 / fps) / substeps

    arch = ti.gpu if device == "GPU" else ti.cpu
    ti.init(arch=arch, default_fp=ti.f32, log_level=ti.WARN, offline_cache=True)
    try:
        return _solve(particles, tracers, velocity, material, n_grid, stiffness, friction,
                      sand_angle, gravity, solid_normals, sphere_frames, frame_count, fps,
                      substeps, dt, n_spheres, progress)
    finally:
        # Torn down on the thread that started it. Left for interpreter exit instead, a
        # runtime made on a worker thread aborts the whole process -- "Fatal Python error:
        # Aborted" as usdview closes. It also hands the GPU memory back between bakes.
        ti.reset()


def _solve(particles, tracers, velocity, material, n_grid, stiffness, friction, sand_angle,
           gravity, solid_normals, sphere_frames, frame_count, fps, substeps, dt, n_spheres,
           progress):
    n, n_tracers = len(particles), len(tracers)
    sim = _mpm_class()(n, n_grid, n_tracers, n_spheres, _MATERIAL_IDS[material], stiffness,
               friction, sand_angle, gravity)
    if n:
        sim.x.from_numpy(particles)
        sim.v.from_numpy(np.broadcast_to(velocity, (n, 3)).copy())
    if n_tracers:
        sim.tx.from_numpy(tracers)
    if solid_normals is not None:
        sim.solid_n.from_numpy(np.ascontiguousarray(solid_normals[..., 0, :], dtype=np.float32))
        sim.solid_n2.from_numpy(np.ascontiguousarray(solid_normals[..., 1, :], dtype=np.float32))
    sim.reset_state()

    positions = np.empty((frame_count, n, 3), np.float32)
    velocities = np.empty((frame_count, n, 3), np.float32)
    tracer_pos = np.empty((frame_count, n_tracers, 3), np.float32)
    tracer_rot = np.empty((frame_count, n_tracers, 4), np.float32)
    sphere_buf = np.zeros((max(n_spheres, 1), 4), np.float32)
    sphere_vel = np.zeros((max(n_spheres, 1), 3), np.float32)

    for frame in range(frame_count):
        if n:
            positions[frame] = sim.x.to_numpy()[:n]
            velocities[frame] = sim.v.to_numpy()[:n]
        if n_tracers:
            tracer_pos[frame] = sim.tx.to_numpy()[:n_tracers]
            tracer_rot[frame] = sim.tq.to_numpy()[:n_tracers]
        if progress is not None:
            progress(frame, frame_count)
        if frame == frame_count - 1:
            break

        if n_spheres:
            centres, radii, vels = sphere_frames[frame]
            count = len(centres)
            sphere_buf[:] = 0.0
            sphere_vel[:] = 0.0
            # A sphere with no radius stamps nothing: frames with fewer colliders pad out.
            sphere_buf[:count, :3] = centres
            sphere_buf[:count, 3] = radii
            sphere_vel[:count] = vels
            sim.sph.from_numpy(sphere_buf)
            sim.sph_v.from_numpy(sphere_vel)

        for step in range(substeps):
            if n_spheres:
                sim.raster_spheres(step * dt)
            sim.substep(dt)
            if n_tracers:
                sim.advect_tracers(dt)

    return {
        "positions": positions,
        "velocities": velocities,
        "tracer_positions": tracer_pos,
        "tracer_rotations": tracer_rot,
        "substeps": substeps,
    }


# ---------------------------------------------------------------------------
# geometry -- numpy, no Taichi
# ---------------------------------------------------------------------------


def _world_triangles(prim, time=Usd.TimeCode.Default()):
    """World-space triangles of every mesh under `prim`, as three (T, 3) float64 arrays."""
    parts = ([], [], [])
    if not prim or not prim.IsValid():
        return tuple(np.zeros((0, 3)) for _ in range(3))
    xform_cache = UsdGeom.XformCache(time)
    for p in Usd.PrimRange(prim):
        if not p.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(p)
        points = mesh.GetPointsAttr().Get(time)
        counts = mesh.GetFaceVertexCountsAttr().Get(time)
        indices = mesh.GetFaceVertexIndicesAttr().Get(time)
        if not points or not counts or not indices:
            continue
        counts = np.asarray(counts, dtype=np.int64)
        indices = np.asarray(indices, dtype=np.int64)
        per_face = np.maximum(counts - 2, 0)
        total = int(per_face.sum())
        if not total:
            continue
        face_start = np.concatenate(([0], np.cumsum(counts)[:-1]))
        fan_start = np.concatenate(([0], np.cumsum(per_face)[:-1]))
        face_of = np.repeat(np.arange(len(counts)), per_face)
        within = np.arange(total) - np.repeat(fan_start, per_face)
        first = face_start[face_of]
        matrix = np.asarray(xform_cache.GetLocalToWorldTransform(p), dtype=np.float64)
        world = np.asarray(points, dtype=np.float64) @ matrix[:3, :3] + matrix[3, :3]
        parts[0].append(world[indices[first]])
        parts[1].append(world[indices[first + within + 1]])
        parts[2].append(world[indices[first + within + 2]])
    if not parts[0]:
        return tuple(np.zeros((0, 3)) for _ in range(3))
    return tuple(np.concatenate(part) for part in parts)


def _fill_triangles(v0, v1, v2, spacing, rng):
    """Lattice points inside a closed triangle soup, by scanline parity along X.

    Every lattice line (y, z) is intersected with the triangles whose YZ bounds cover it, all
    pairs at once; a lattice point is inside when an odd number of crossings lie before it on
    its line. The lines are nudged off the lattice so they do not run exactly through vertices
    and edges, where a crossing would count twice.
    """
    if not len(v0) or spacing <= 0.0:
        return np.zeros((0, 3))
    corners = np.concatenate((v0, v1, v2))
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    nudge = spacing * np.asarray([0.0, 0.0137, 0.0291])
    axes = [np.arange(lo[a] + spacing * 0.5, hi[a], spacing) + nudge[a] for a in range(3)]
    if any(len(axis) == 0 for axis in axes):
        return np.zeros((0, 3))
    ny, nz = len(axes[1]), len(axes[2])

    tri_lo = np.minimum(np.minimum(v0, v1), v2)
    tri_hi = np.maximum(np.maximum(v0, v1), v2)
    y_first = np.clip(np.ceil((tri_lo[:, 1] - axes[1][0]) / spacing), 0, ny).astype(np.int64)
    y_last = np.clip(np.floor((tri_hi[:, 1] - axes[1][0]) / spacing), -1, ny - 1).astype(np.int64)
    z_first = np.clip(np.ceil((tri_lo[:, 2] - axes[2][0]) / spacing), 0, nz).astype(np.int64)
    z_last = np.clip(np.floor((tri_hi[:, 2] - axes[2][0]) / spacing), -1, nz - 1).astype(np.int64)
    span_y = np.maximum(y_last - y_first + 1, 0)
    span_z = np.maximum(z_last - z_first + 1, 0)
    per_tri = span_y * span_z
    total = int(per_tri.sum())
    if not total:
        return np.zeros((0, 3))

    tri = np.repeat(np.arange(len(v0)), per_tri)
    start = np.concatenate(([0], np.cumsum(per_tri)[:-1]))
    within = np.arange(total) - np.repeat(start, per_tri)
    wide = np.repeat(np.maximum(span_z, 1), per_tri)
    iy = np.repeat(y_first, per_tri) + within // wide
    iz = np.repeat(z_first, per_tri) + within % wide

    a, e1, e2 = v0[tri], v1[tri] - v0[tri], v2[tri] - v0[tri]
    dy = axes[1][iy] - a[:, 1]
    dz = axes[2][iz] - a[:, 2]
    det = e1[:, 1] * e2[:, 2] - e1[:, 2] * e2[:, 1]
    ok = np.abs(det) > 1e-14
    with np.errstate(divide="ignore", invalid="ignore"):
        u = (dy * e2[:, 2] - dz * e2[:, 1]) / det
        v = (e1[:, 1] * dz - e1[:, 2] * dy) / det
    ok &= (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0)
    x_cross = a[ok, 0] + u[ok] * e1[ok, 0] + v[ok] * e2[ok, 0]
    line = (iy[ok] * nz + iz[ok]).astype(np.float64)

    span_x = max(hi[0] - lo[0], 1e-12) * 1.001
    crossing_key = np.sort(line + (x_cross - lo[0]) / span_x)

    gx, gy, gz = np.meshgrid(np.arange(len(axes[0])), np.arange(ny), np.arange(nz), indexing="ij")
    gx, gy, gz = gx.ravel(), gy.ravel(), gz.ravel()
    point_line = (gy * nz + gz).astype(np.float64)
    point_key = point_line + (axes[0][gx] - lo[0]) / span_x
    before = np.searchsorted(crossing_key, point_key) - np.searchsorted(crossing_key, point_line)
    inside = (before % 2) == 1

    points = np.stack((axes[0][gx[inside]], axes[1][gy[inside]], axes[2][gz[inside]]), axis=1)
    # Anywhere in its own cell: a regular lattice survives into the result as visible stripes.
    return points + rng.uniform(-0.5, 0.5, points.shape) * spacing


def _ball_offsets(radius, spacing, rng):
    """Jittered lattice offsets filling a ball; never empty, so a tiny ball is still a particle."""
    steps = int(math.ceil(radius / spacing))
    axis = np.arange(-steps, steps + 1) * spacing
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
    grid = grid[np.einsum("ij,ij->i", grid, grid) <= radius * radius]
    if not len(grid):
        grid = np.zeros((1, 3))
    return grid + rng.uniform(-0.5, 0.5, grid.shape) * spacing


def _voxelize(v0, v1, v2, n_grid, rng):
    """Collider normals on the grid nodes near the surface, in domain units.

    Each node within `_COLLIDER_BAND` cells of a triangle gets that triangle's normal, flipped
    to point from the surface towards the node -- so a wall pushes back on whichever side the
    material is, and an open mesh (a bowl, a table top) works without being closed.

    Two per node, as (n_grid, n_grid, n_grid, 2, 3): the nearest surface, and the nearest one
    facing a different way. One was not enough where a wall meets a floor -- the node took
    whichever was closer and forgot the other, and material slid out through the seam.
    """
    out = np.zeros((n_grid, n_grid, n_grid, 2, 3), np.float32)
    if not len(v0):
        return out
    dx = 1.0 / n_grid
    band = _COLLIDER_BAND * dx

    tri_lo = np.minimum(np.minimum(v0, v1), v2)
    tri_hi = np.maximum(np.maximum(v0, v1), v2)
    near = np.all(tri_hi >= -band, axis=1) & np.all(tri_lo <= 1.0 + band, axis=1)
    v0, v1, v2 = v0[near], v1[near], v2[near]
    if not len(v0):
        return out
    e1, e2 = v1 - v0, v2 - v0
    cross = np.cross(e1, e2)
    length = np.linalg.norm(cross, axis=1)
    real = length > 1e-14
    v0, e1, e2, cross, length = v0[real], e1[real], e2[real], cross[real], length[real]
    normals = cross / length[:, None]

    # Sample every triangle at half-cell density, then keep one sample per half-cell.
    half = dx * 0.5
    per_tri = np.ceil(length / (half * half)).astype(np.int64) + 1
    budget = 4_000_000
    if per_tri.sum() > budget:
        per_tri = np.maximum(1, (per_tri * (budget / per_tri.sum())).astype(np.int64))
    tri = np.repeat(np.arange(len(v0)), per_tri)
    r1, r2 = rng.random(len(tri)), rng.random(len(tri))
    flip = r1 + r2 > 1.0
    r1[flip], r2[flip] = 1.0 - r1[flip], 1.0 - r2[flip]
    samples = np.concatenate((
        v0[tri] + r1[:, None] * e1[tri] + r2[:, None] * e2[tri],
        v0, v0 + e1, v0 + e2,
    ))
    sample_n = np.concatenate((normals[tri], normals, normals, normals))
    cell = np.floor(samples / half).astype(np.int64) + (1 << 20)
    key = (cell[:, 0] << 42) | (cell[:, 1] << 21) | cell[:, 2]
    _, first = np.unique(key, return_index=True)
    samples, sample_n = samples[first], sample_n[first]

    flat = out[..., 0, :].reshape(-1, 3)
    flat2 = out[..., 1, :].reshape(-1, 3)
    best = np.full(len(flat), np.inf)
    best2 = np.full(len(flat), np.inf)
    base = np.floor(samples * n_grid).astype(np.int64)
    for ox in (-1, 0, 1, 2):
        for oy in (-1, 0, 1, 2):
            for oz in (-1, 0, 1, 2):
                node = base + (ox, oy, oz)
                valid = np.all((node >= 0) & (node < n_grid), axis=1)
                offset = node * dx - samples
                dist = np.linalg.norm(offset, axis=1)
                valid &= dist <= band
                if not valid.any():
                    continue
                idx = (node[valid, 0] * n_grid + node[valid, 1]) * n_grid + node[valid, 2]
                d = dist[valid]
                side = np.where(np.einsum("ij,ij->i", offset[valid], sample_n[valid]) >= 0.0, 1.0, -1.0)
                facing = sample_n[valid] * side[:, None]
                order = np.lexsort((d, idx))
                idx, d, facing = idx[order], d[order], facing[order]
                keep = np.concatenate(([True], idx[1:] != idx[:-1]))
                idx, d, facing = idx[keep], d[keep], facing[keep]
                current, current_d = flat[idx], best[idx]
                turned = np.einsum("ij,ij->i", facing, current) < 0.5
                closer = d < current_d
                # A closer surface facing another way demotes the old nearest to second.
                demote = closer & turned & (current_d < best2[idx])
                flat2[idx[demote]] = current[demote]
                best2[idx[demote]] = current_d[demote]
                best[idx[closer]] = d[closer]
                flat[idx[closer]] = facing[closer]
                second = ~closer & turned & (d < best2[idx])
                flat2[idx[second]] = facing[second]
                best2[idx[second]] = d[second]
    out[..., 0, :] = flat.reshape(out[..., 0, :].shape)
    out[..., 1, :] = flat2.reshape(out[..., 1, :].shape)
    return out


def _surface_points(positions, up_idx, cell, spacing):
    """The particles with nothing above them, thinned to one per `spacing`, nudged on top."""
    if not len(positions) or cell <= 0.0:
        return np.zeros((0, 3))

    def pack(k):
        k = k + (1 << 20)
        return (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]

    keys = np.floor(positions / cell).astype(np.int64)
    above = keys.copy()
    above[:, up_idx] += 1
    surface = positions[~np.isin(pack(above), pack(keys))]
    if spacing > 0.0 and len(surface):
        # Highest first, so the point kept per patch is the one actually on top.
        surface = surface[np.argsort(-surface[:, up_idx], kind="stable")]
        _, first = np.unique(pack(np.floor(surface / spacing).astype(np.int64)), return_index=True)
        surface = surface[np.sort(first)]
    surface = surface.copy()
    surface[:, up_idx] += cell * 0.25
    return surface


# ---------------------------------------------------------------------------
# quaternions, w-x-y-z, batched
# ---------------------------------------------------------------------------


def _qmul(a, b):
    aw, ax, ay, az = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    bw, bx, by, bz = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    return np.stack((
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ), axis=-1)


def _qrot(q, v):
    """Rotate vectors v by unit quaternions q."""
    u = q[..., 1:]
    uv = np.cross(u, v)
    return v + 2.0 * (q[..., :1] * uv + np.cross(u, uv))


def _qconj(q):
    return q * np.asarray([1.0, -1.0, -1.0, -1.0])


def _quat_from_matrices(m):
    """Unit quaternions from (N, 3, 3) rotation matrices (column-vector convention)."""
    w = np.sqrt(np.maximum(0.0, 1.0 + m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2])) * 0.5
    x = np.sqrt(np.maximum(0.0, 1.0 + m[:, 0, 0] - m[:, 1, 1] - m[:, 2, 2])) * 0.5
    y = np.sqrt(np.maximum(0.0, 1.0 - m[:, 0, 0] + m[:, 1, 1] - m[:, 2, 2])) * 0.5
    z = np.sqrt(np.maximum(0.0, 1.0 - m[:, 0, 0] - m[:, 1, 1] + m[:, 2, 2])) * 0.5
    x = np.copysign(x, m[:, 2, 1] - m[:, 1, 2])
    y = np.copysign(y, m[:, 0, 2] - m[:, 2, 0])
    z = np.copysign(z, m[:, 1, 0] - m[:, 0, 1])
    q = np.stack((w, x, y, z), axis=1)
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def _blob_frames(positions, blob_ids, count):
    """Per-blob centroid and best-fit rotation, frame by frame, from shape matching.

    The rotation is the polar part of sum((x - c)(x0 - c0)^T): what a rigid body would have
    to do to line the blob's starting shape up with where its particles are now.
    """
    frames = len(positions)
    centres = np.zeros((frames, count, 3))
    rotations = np.zeros((frames, count, 4))
    rotations[..., 0] = 1.0
    if not count or not len(blob_ids):
        return centres, rotations
    weights = np.bincount(blob_ids, minlength=count).astype(np.float64)
    safe = np.maximum(weights, 1.0)[:, None]
    rest = positions[0].astype(np.float64)
    rest_c = np.stack([np.bincount(blob_ids, rest[:, a], count) for a in range(3)], axis=1) / safe
    rest_rel = rest - rest_c[blob_ids]
    for f in range(frames):
        now = positions[f].astype(np.float64)
        c = np.stack([np.bincount(blob_ids, now[:, a], count) for a in range(3)], axis=1) / safe
        rel = now - c[blob_ids]
        A = np.zeros((count, 3, 3))
        np.add.at(A, blob_ids, rel[:, :, None] * rest_rel[:, None, :])
        U, _S, Vt_ = np.linalg.svd(A)
        R = U @ Vt_
        flip = np.linalg.det(R) < 0.0
        if flip.any():
            U[flip, :, 2] *= -1.0
            R[flip] = U[flip] @ Vt_[flip]
        centres[f] = c
        rotations[f] = _quat_from_matrices(R)
    return centres, rotations


# ---------------------------------------------------------------------------
# reading the scene
# ---------------------------------------------------------------------------


def _proto_bounds(prim, time=Usd.TimeCode.Default()):
    """(extents, centres) of a PointInstancer's prototypes, one row each, in their own frames."""
    stage = prim.GetStage()
    bbox = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_])
    extents, centres = [], []
    for target in UsdGeom.PointInstancer(prim).GetPrototypesRel().GetTargets():
        ext, ctr = np.ones(3), np.zeros(3)
        proto = stage.GetPrimAtPath(target)
        if proto:
            rng = bbox.ComputeLocalBound(proto).ComputeAlignedRange()
            if not rng.IsEmpty():
                ext = np.asarray(rng.GetMax() - rng.GetMin(), dtype=np.float64)
                ctr = np.asarray((rng.GetMax() + rng.GetMin()) * 0.5, dtype=np.float64)
        extents.append(ext)
        centres.append(ctr)
    if not extents:
        extents, centres = [np.ones(3)], [np.zeros(3)]
    return np.asarray(extents), np.asarray(centres)


class _Instances:
    """A PointInstancer's instances as world-space balls, and the way back to its own arrays."""

    def __init__(self, prim, time=Usd.TimeCode.Default(), proto_bounds=None):
        inst = UsdGeom.PointInstancer(prim)
        matrix = UsdGeom.XformCache(time).GetLocalToWorldTransform(prim)
        self.prim = prim
        self.matrix = np.asarray(matrix, dtype=np.float64)
        self.inverse = np.asarray(matrix.GetInverse(), dtype=np.float64)
        q = matrix.RemoveScaleShear().ExtractRotationQuat()
        self.inst_q = np.asarray([q.GetReal(), *q.GetImaginary()], dtype=np.float64)

        positions = inst.GetPositionsAttr().Get(time) or []
        self.count = len(positions)
        local_pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
        orients = inst.GetOrientationsAttr().Get(time)
        local_q = np.tile([1.0, 0.0, 0.0, 0.0], (self.count, 1))
        if orients:
            n = min(len(orients), self.count)
            local_q[:n] = np.asarray(orients, dtype=np.float64)[:n][:, [3, 0, 1, 2]]  # buffer is x, y, z, w
        scales = inst.GetScalesAttr().Get(time)
        scale = np.ones((self.count, 3))
        if scales:
            n = min(len(scales), self.count)
            scale[:n] = np.asarray(scales, dtype=np.float64)[:n]
        indices = inst.GetProtoIndicesAttr().Get(time)
        proto_idx = np.zeros(self.count, dtype=np.int64)
        if indices:
            n = min(len(indices), self.count)
            proto_idx[:n] = np.asarray(indices, dtype=np.int64)[:n]

        extents, centres = proto_bounds if proto_bounds is not None else _proto_bounds(prim, time)
        proto_idx = np.where((proto_idx >= 0) & (proto_idx < len(extents)), proto_idx, 0)

        world_scale = float(np.linalg.norm(self.matrix[0, :3]))
        self.world_q = _qmul(np.broadcast_to(self.inst_q, local_q.shape), local_q)
        #: Instance origin to geometry centre, in the instance's own scaled frame.
        self.centre_offset = centres[proto_idx] * scale
        origins = local_pos @ self.matrix[:3, :3] + self.matrix[3, :3]
        self.centres = origins + _qrot(self.world_q, self.centre_offset)
        self.radii = (extents[proto_idx] * scale).max(axis=1) * 0.5 * world_scale

    def arrays_for(self, centres, delta_q):
        """Instancer-local positions and orientations for instances whose geometry centres
        moved to `centres` and turned by the world-space `delta_q`."""
        world_q = _qmul(delta_q, self.world_q)
        origins = centres - _qrot(world_q, self.centre_offset)
        local_pos = origins @ self.inverse[:3, :3] + self.inverse[3, :3]
        local_q = _qmul(np.broadcast_to(_qconj(self.inst_q), world_q.shape), world_q)
        return local_pos, local_q


def _time_varying(prim):
    """Whether this prim moves over time -- its own transform or any ancestor's."""
    p = prim
    while p and p.IsValid() and p.GetPath() != Sdf.Path.absoluteRootPath:
        xformable = UsdGeom.Xformable(p)
        if xformable and xformable.TransformMightBeTimeVarying():
            return True
        p = p.GetParent()
    return False


def _sphere_track(prim, start_frame, frame_count):
    """World-space (centres, radii) of a moving collider at every frame.

    An instancer -- the RBD Solver's usual bake -- is one sphere per instance. Anything else
    is one sphere around its bounds, which is coarse, but a rigid body that is simply falling
    into the material is the case this is for.
    """
    track = []
    # Measured once: the instances move, their prototypes do not. Per frame, 1,464 Fracture
    # prototypes cost 7.6 s here, all on the main thread.
    bounds = _proto_bounds(prim, Usd.TimeCode(start_frame)) if prim.IsA(UsdGeom.PointInstancer) else None
    for f in range(frame_count):
        tc = Usd.TimeCode(start_frame + f)
        if bounds is not None:
            inst = _Instances(prim, tc, bounds)
            track.append((inst.centres, inst.radii))
            continue
        rng = UsdGeom.BBoxCache(tc, [UsdGeom.Tokens.default_]).ComputeWorldBound(prim).ComputeAlignedRange()
        if rng.IsEmpty():
            track.append((np.zeros((0, 3)), np.zeros(0)))
            continue
        lo, hi = np.asarray(rng.GetMin()), np.asarray(rng.GetMax())
        track.append((((lo + hi) * 0.5)[None, :], np.asarray([(hi - lo).max() * 0.5])))
    return track


def _bounds_of(prim):
    rng = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]) \
        .ComputeWorldBound(prim).ComputeAlignedRange()
    if rng.IsEmpty():
        return None
    return np.asarray(rng.GetMin(), dtype=np.float64), np.asarray(rng.GetMax(), dtype=np.float64)


def _domain(source_lo, source_hi, collider_bounds, container, margin, up_idx, n_grid):
    """The world box the unit cube is mapped onto: (origin, size).

    Around the source, grown by Margin times its size, and reaching down to whatever colliders
    sit under it -- otherwise water poured over a table would land on the domain floor, in mid
    air. A Container prim overrides all of it. Made cubic (cells are cubes) by growing
    upwards and evenly sideways, then padded by the wall cells so the walls sit outside it.
    """
    if container is not None:
        lo, hi = container
    else:
        pad = margin * float((source_hi - source_lo).max())
        lo, hi = source_lo - pad, source_hi + pad
        lateral = [a for a in range(3) if a != up_idx]
        for c_lo, c_hi in collider_bounds:
            overlaps = all(c_hi[a] >= lo[a] and c_lo[a] <= hi[a] for a in lateral)
            if overlaps and c_hi[up_idx] <= source_lo[up_idx]:
                lo[up_idx] = min(lo[up_idx], max(c_lo[up_idx], c_hi[up_idx] - pad))
    size = float((hi - lo).max())
    size = max(size, 1e-6)
    origin = (lo + hi) * 0.5 - size * 0.5
    origin[up_idx] = lo[up_idx]
    wall = (_BOUND + 1) / n_grid
    padded = size / (1.0 - 2.0 * wall)
    origin = origin - (padded - size) * 0.5
    return origin, padded


# ---------------------------------------------------------------------------
# chunk glue
# ---------------------------------------------------------------------------

_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def _vec3(node, name):
    value = getattr(node.get_param(name), "value", None)
    out = [float(v) for v in (value or ())][:3]
    return np.asarray(out + [0.0] * (3 - len(out)))


def _path_list(value):
    return [p.strip() for p in (value or "").replace(",", "\n").split("\n") if p.strip()]


def _bind_material(stage, prim, material_path):
    """Bind a Material to `prim`, if one was named. displayColor is a viewport fallback; a
    renderer wants a real binding."""
    if not material_path:
        return
    material = UsdShade.Material(stage.GetPrimAtPath(material_path))
    if material:
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


def _author_sphere_instancer(stage, path, positions, radius, colour, material_path, start_frame):
    """The particles again, as instanced spheres.

    V-Ray draws a UsdGeom.Points cloud as a single point however wide the points are, and a
    material cannot fix that -- so anything meant to be rendered rather than looked at in
    Storm needs real geometry. One sphere prototype, instanced per particle, which is what
    every delegate here draws and shades properly.
    """
    instancer = UsdGeom.PointInstancer.Define(stage, path)
    proto_path = path.AppendChild("Prototypes").AppendChild("Particle")
    sphere = UsdGeom.Sphere.Define(stage, proto_path)
    sphere.GetRadiusAttr().Set(float(radius))
    sphere.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(-radius, -radius, -radius),
                                                 Gf.Vec3f(radius, radius, radius)]))
    UsdGeom.Primvar(sphere.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
    sphere.GetDisplayColorAttr().Set(Vt.Vec3fArray([colour]))
    _bind_material(stage, sphere.GetPrim(), material_path)
    instancer.CreatePrototypesRel().SetTargets([proto_path])
    instancer.CreateProtoIndicesAttr().Set(Vt.IntArray([0] * positions.shape[1]))
    frames = np.ascontiguousarray(positions, dtype=np.float32)
    sim_bake.write_samples(instancer.CreatePositionsAttr(),
                           ((start_frame + f, Vt.Vec3fArray.FromNumpy(frames[f])) for f in range(len(frames))),
                           default=Vt.Vec3fArray.FromNumpy(frames[-1]))
    sim_bake.write_samples(instancer.CreateExtentAttr(), (
        (start_frame + f, _extent(frames[f], radius)) for f in range(len(frames))
    ))


def _extent(points, pad):
    """The bounds of points, padded, as an extent value."""
    return Vt.Vec3fArray.FromNumpy(np.stack((points.min(axis=0) - pad, points.max(axis=0) + pad)).astype(np.float32))


def _emit(source, spacing, blob_radius, rng):
    """World-space starting particles, plus which blob each belongs to (-1 for none).

    Returns (particles, blob_ids, instances) -- `instances` set only for an instancer source.
    """
    if source.IsA(UsdGeom.PointInstancer):
        inst = _Instances(source)
        parts, ids = [], []
        for i in range(inst.count):
            ball = inst.centres[i] + _ball_offsets(max(inst.radii[i], spacing * 0.5), spacing, rng)
            parts.append(ball)
            ids.append(np.full(len(ball), i, dtype=np.int64))
        if not parts:
            return np.zeros((0, 3)), np.zeros(0, np.int64), inst
        return np.concatenate(parts), np.concatenate(ids), inst

    if source.IsA(UsdGeom.Points):
        cloud = UsdGeom.Points(source)
        points = cloud.GetPointsAttr().Get()
        if not points:
            return np.zeros((0, 3)), np.zeros(0, np.int64), None
        matrix = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(source), dtype=np.float64)
        world = np.asarray(points, dtype=np.float64) @ matrix[:3, :3] + matrix[3, :3]
        radius = max(blob_radius, spacing * 0.5)
        normals = cloud.GetNormalsAttr().Get()
        if normals and len(normals) == len(world):
            # A painted point is ON the surface. Centred there, half of every ball starts
            # inside whatever was painted on, and the collider traps it underneath.
            facing = np.asarray(normals, dtype=np.float64) @ matrix[:3, :3]
            facing /= np.maximum(np.linalg.norm(facing, axis=1, keepdims=True), 1e-12)
            world = world + facing * (radius + spacing)
        offsets = _ball_offsets(radius, spacing, rng)
        parts = [p + offsets for p in world]
        ids = np.repeat(np.arange(len(world)), len(offsets))
        return np.concatenate(parts), ids, None

    v0, v1, v2 = _world_triangles(source)
    particles = _fill_triangles(v0, v1, v2, spacing, rng)
    return particles, np.full(len(particles), -1, dtype=np.int64), None


def _auto_blob_radius(source, stage):
    """Half the brush radius the points were painted with, or 5 cm when nothing says.

    The brush records each dab's width as a quarter of its radius, so this ties the ball of
    material to the size of the brush you painted with, in whatever units the scene uses. A
    fixed 5 was 5 metres in a metre scene.
    """
    widths = UsdGeom.Points(source).GetWidthsAttr().Get() if source.IsA(UsdGeom.Points) else None
    if widths:
        return float(np.mean(np.asarray(widths, dtype=np.float64))) * 2.0
    return 0.05 / UsdGeom.GetStageMetersPerUnit(stage)


def _run(owner, notice, sender):
    node = owner.get_chunk()
    readout = node.get_param("readout")

    def report(text):
        if readout is not None:
            readout.value = text
        logger.info("[Particle Solver] %s", text.replace("\n", " | "))

    if not _HAS_TAICHI:
        report("taichi is not installed -- pip install taichi")
        return
    stage = owner.get_stage()
    layer = owner.get_edit_context()
    if stage is None or layer is None:
        return
    if sim_bake.is_running(node):
        report("already simulating -- wait, or press Cancel")
        return

    source_paths = node.get_param("source_prim").resolve_paths()
    if not source_paths:
        report("no source -- pick a mesh, a Scatter Brush's points or a Scatter instancer")
        return

    is_f, is_i, is_s = chunk.is_param_t(float), chunk.is_param_t(int), chunk.is_param_t(str)
    material = node.get_param("material", is_s).value
    grid_res = max(16, node.get_param("grid_res", is_i).value)
    frame_count = max(2, node.get_param("frame_count", is_i).value)
    start_frame = node.get_param("start_frame", is_i).value
    fps = max(1.0, node.get_param("fps", is_f).value)
    gravity_w = sim_bake.gravity(node.get_param("gravity", is_f).value, stage)
    seed = node.get_param("seed", is_i).value
    max_particles = max(1, node.get_param("max_particles", is_i).value)
    rng = np.random.default_rng(seed)

    # Read through the upstream composition, so what this chunk writes (and anything wired
    # after it) cannot feed back into its own inputs. The one exception is the instancer it
    # drives, which is written through the real stage below.
    upstream = chunk_stage.get_upstream_stage(stage, node.get_scene(), node)
    source = upstream.GetPrimAtPath(source_paths[0])
    if not source or not source.IsValid():
        if source_paths[0].endswith("/PaintedPoints"):
            report("nothing painted yet -- set the Scatter Brush's Output to Points and paint")
        else:
            report(f"source {source_paths[0]} not found")
        return
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2

    static_prims, moving_prims = [], []
    for path in _path_list(node.get_param("collision_path", is_s).value):
        prim = upstream.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            continue
        if prim.IsA(UsdGeom.PointInstancer) or _time_varying(prim):
            moving_prims.append(prim)
        else:
            static_prims.append(prim)

    container = None
    container_path = node.get_param("container_prim", is_s).value
    if container_path:
        container_prim = upstream.GetPrimAtPath(container_path)
        if container_prim and container_prim.IsValid():
            container = _bounds_of(container_prim)

    # The domain depends on the source's extent and the particle spacing on the domain, so
    # the source is sampled twice: once coarsely to find where it is, once at the real spacing.
    probe = _bounds_of(source)
    if probe is None:
        report("the source has no extent")
        return
    blob_radius = node.get_param("blob_radius", is_f).value or _auto_blob_radius(source, stage)
    if source.IsA(UsdGeom.Points):
        probe = (probe[0] - blob_radius, probe[1] + blob_radius)
    collider_bounds = [b for b in map(_bounds_of, static_prims) if b is not None]
    origin, size = _domain(probe[0], probe[1], collider_bounds, container,
                           node.get_param("margin", is_f).value, up_idx, grid_res)
    spacing = size / grid_res * 0.5
    logger.debug("domain origin %s size %.3f spacing %.4f source %s colliders %s",
                 origin, size, spacing, probe, collider_bounds)

    particles_w, blob_ids, source_instances = _emit(source, spacing, blob_radius, rng)
    subsampled = ""
    if len(particles_w) > max_particles:
        keep = np.sort(rng.choice(len(particles_w), max_particles, replace=False))
        subsampled = f" (thinned from {len(particles_w)})"
        particles_w, blob_ids = particles_w[keep], blob_ids[keep]
    if not len(particles_w):
        report("the source produced no particles -- is it a closed mesh?")
        return

    def to_domain(p):
        return (np.asarray(p, dtype=np.float64) - origin) / size

    solid = None
    if static_prims:
        tris = [_world_triangles(p) for p in static_prims]
        v0, v1, v2 = (np.concatenate([t[k] for t in tris]) for k in range(3))
        solid = _voxelize(to_domain(v0), to_domain(v1), to_domain(v2), grid_res, rng)

    sphere_frames = None
    if moving_prims:
        tracks = [_sphere_track(p, start_frame, frame_count) for p in moving_prims]
        sphere_frames = []
        for f in range(frame_count):
            centres = np.concatenate([t[f][0] for t in tracks]).reshape(-1, 3)
            radii = np.concatenate([t[f][1] for t in tracks])
            nxt = [t[min(f + 1, frame_count - 1)] for t in tracks]
            later = np.concatenate([t[0] for t in nxt]).reshape(-1, 3)
            vel = (later - centres) * fps if len(later) == len(centres) else np.zeros_like(centres)
            sphere_frames.append((to_domain(centres), radii / size, vel / size))

    drive_path = node.get_param("drive_instancer", is_s).value.strip()
    drive_prim = stage.GetPrimAtPath(drive_path) if drive_path else None
    if drive_prim and not drive_prim.IsA(UsdGeom.PointInstancer):
        drive_prim = None
    follow_blobs = bool(drive_prim and source_instances is not None
                        and drive_prim.GetPath() == source.GetPath())
    drive_instances = None
    tracers = None
    if drive_prim is not None:
        drive_instances = source_instances if follow_blobs else _Instances(upstream.GetPrimAtPath(drive_prim.GetPath()))
        if not follow_blobs:
            tracers = to_domain(drive_instances.centres)

    gravity_n = np.zeros(3)
    gravity_n[up_idx] = gravity_w / size
    job = dict(
        particles=to_domain(particles_w),
        velocity=_vec3(node, "initial_velocity") / size,
        material=material,
        n_grid=grid_res,
        stiffness=node.get_param("stiffness", is_f).value,
        friction=node.get_param("friction", is_f).value,
        sand_angle=node.get_param("sand_angle", is_f).value,
        gravity=gravity_n,
        solid_normals=solid,
        sphere_frames=sphere_frames,
        tracers=tracers,
        frame_count=frame_count,
        fps=fps,
        substeps=node.get_param("substeps", is_i).value,
        device=node.get_param("device", is_s).value,
    )

    def finish(result):
        """Write the bake. Always on the main thread: it is all USD authoring."""
        if node.get_edit_context() is None:
            # The chunk went away while it was simulating.
            return
        # Cleared now rather than at Run: the last bake stays on screen while this one is
        # worked out. Nothing above read this layer -- the inputs come from upstream.
        if lreg := chunk.get_layer_manager():
            lreg.clear_layer(layer.identifier)
        elapsed = result["elapsed"]
        positions = result["positions"].astype(np.float64) * size + origin
        velocities = result["velocities"].astype(np.float64) * size

        sim_path = Sdf.Path(node.get_param("sim_path", is_s).value or "/ParticleSim")
        particles_path = sim_path.AppendChild("Particles")
        surface_path = sim_path.AppendChild("SurfacePoints")
        width = spacing * node.get_param("point_scale", is_f).value
        colour = Gf.Vec3f(*_COLOURS[material])

        material_path = node.get_param("material_prim", is_s).value.strip()
        surface_spacing = node.get_param("surface_spacing", is_f).value or spacing * 4.0
        surface = _surface_points(positions[-1], up_idx, spacing * 2.0, surface_spacing)

        with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
            UsdGeom.Xform.Define(stage, sim_path)
            cloud = UsdGeom.Points.Define(stage, particles_path)
            cloud.SetWidthsInterpolation(UsdGeom.Tokens.constant)
            cloud.CreateWidthsAttr().Set(Vt.FloatArray([width]))
            UsdGeom.Primvar(cloud.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
            cloud.GetDisplayColorAttr().Set(Vt.Vec3fArray([colour]))
            # Every frame in one change block each (sim_bake.write_samples): a Set per frame
            # sent a change notice per frame, and the host's listeners ran for every one. The
            # settled state is the default too: Scatter Points reads a points input at
            # Default(), and a time-sampled-only attribute would give it nothing to read.
            pos32 = np.ascontiguousarray(positions, dtype=np.float32)
            vel32 = np.ascontiguousarray(velocities, dtype=np.float32)
            frames_at = range(start_frame, start_frame + frame_count)
            sim_bake.write_samples(cloud.CreatePointsAttr(),
                                   ((t, Vt.Vec3fArray.FromNumpy(pos32[f])) for f, t in enumerate(frames_at)),
                                   default=Vt.Vec3fArray.FromNumpy(pos32[-1]))
            sim_bake.write_samples(cloud.CreateVelocitiesAttr(),
                                   ((t, Vt.Vec3fArray.FromNumpy(vel32[f])) for f, t in enumerate(frames_at)))
            sim_bake.write_samples(cloud.CreateExtentAttr(),
                                   ((t, _extent(pos32[f], width)) for f, t in enumerate(frames_at)))
            _bind_material(stage, cloud.GetPrim(), material_path)

            if node.get_param("render_as", is_s).value == _AS_SPHERES:
                _author_sphere_instancer(stage, sim_path.AppendChild("ParticleSpheres"), positions,
                                         width * 0.5, colour, material_path, start_frame)

            top = UsdGeom.Points.Define(stage, surface_path)
            top.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(surface, dtype=np.float32)))
            up = [0.0, 0.0, 0.0]
            up[up_idx] = 1.0
            top.CreateNormalsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*up)] * len(surface)))
            top.SetWidthsInterpolation(UsdGeom.Tokens.constant)
            top.CreateWidthsAttr().Set(Vt.FloatArray([width * 2.0]))
            UsdGeom.Imageable(top).MakeInvisible()

            if drive_instances is not None and drive_instances.count:
                if follow_blobs:
                    centres, delta = _blob_frames(positions, blob_ids, drive_instances.count)
                    # Shape matching finds each blob's centroid; offset it back to the instance's
                    # own geometry centre so frame 0 does not jump.
                    centres = centres - centres[0] + drive_instances.centres
                else:
                    centres = result["tracer_positions"].astype(np.float64) * size + origin
                    delta = result["tracer_rotations"].astype(np.float64)
                inst = UsdGeom.PointInstancer(drive_prim)
                driven = [drive_instances.arrays_for(centres[f], delta[f]) for f in range(frame_count)]
                sim_bake.write_samples(inst.GetPositionsAttr(), (
                    (start_frame + f, Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(p, dtype=np.float32)))
                    for f, (p, _q) in enumerate(driven)
                ))
                sim_bake.write_samples(inst.GetOrientationsAttr(), (
                    (start_frame + f, Vt.QuathArray([Gf.Quath(*map(float, x)) for x in q]))
                    for f, (_p, q) in enumerate(driven)
                ))

        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.SetStartTimeCode(start_frame)
            stage.SetEndTimeCode(start_frame + frame_count - 1)
        if drive_instances is not None:
            sim_bake.promote(layer)
        # Kept before any param below is set: setting one cooks this chunk, the cook clears
        # this layer and puts the kept bake back -- which must already be this one.
        sim_bake.keep(node, layer)

        for name, value in (("particles_out", str(particles_path)), ("surface_out", str(surface_path))):
            param = node.get_param(name)
            if param is not None and param.value != value:
                param.value = value
        revision = node.get_param("revision")
        if revision is not None:
            revision.value = int(revision.value or 0) + 1

        try:
            scene = chunk_registry.ChunkRegistry().scene
            if scene is not None and hasattr(scene, "refresh_timeline"):
                scene.refresh_timeline(start_frame, start_frame + frame_count - 1)
        except Exception:  # noqa: BLE001
            pass

        driven = ""
        if drive_instances is not None:
            driven = f"\ndriving {drive_instances.count} instances " + \
                ("(each follows its own blob)" if follow_blobs else "(carried by the flow)")
        report(
            f"{len(particles_w)} particles{subsampled} · {material} · grid {grid_res}³ · "
            f"{result['substeps']} substeps/frame · {elapsed:.1f}s\n"
            f"{len(surface)} surface points{driven}"
        )
        node.unset_state(chunk.ChunkFlags.State.STALE)
        # In the cook cache for the next time these inputs come round, this session or after
        # a reopen. Stored last, so the readout above goes with it.
        if entry is not None:
            entry.store(node, layer)

    # A bake made from exactly these inputs before comes back from the cook cache instead.
    entry = sim_bake.cache_entry(node, sim_bake.digest(
        job, origin, size, spacing, blob_ids, follow_blobs, str(drive_path),
        vars(drive_instances) if drive_instances is not None else None,
    ))
    # Read into a scratch layer and kept before it goes in: restoring sets the output params,
    # and each cooks this chunk, which clears its layer and puts the kept bake back.
    read_back = Sdf.Layer.CreateAnonymous("particle_read_back")
    if entry is not None and entry.restore(node, read_back):
        sim_bake.keep(node, read_back)
        layer.TransferContent(read_back)
        _after_read_back(node, layer, stage, start_frame, frame_count, promote=drive_instances is not None)
        return
    if getattr(node, "_particle_cache_only", False):
        node.set_state(chunk.ChunkFlags.State.STALE)
        report("not simulated -- press Run Simulation")
        return

    def bake(ctx):
        """Solve in a separate process: Taichi holds the GIL while it computes, which from a
        thread still stopped the host for over a second at a time."""
        yield sim_bake.WORKER
        started = time.perf_counter()
        ctx.status("simulating... (the first run of a session compiles for about 10 s)")
        result = sim_bake.in_subprocess(ctx, simulate, **job)
        result["elapsed"] = time.perf_counter() - started
        yield sim_bake.MAIN
        return result

    sim_bake.run(node, bake, report=report, finish=finish, name="ParticleSolver")


def _after_read_back(node, layer, stage, start_frame, frame_count, promote):
    """What a bake does besides its layer, for one read back from the cook cache."""
    if promote:
        sim_bake.promote(layer)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)
    revision = node.get_param("revision")
    if revision is not None:
        revision.value = int(revision.value or 0) + 1  # downstream reads the points again
    readout = node.get_param("readout")
    if readout is not None:
        readout.value = f"{readout.value}\nread back from the cook cache"
    node.unset_state(chunk.ChunkFlags.State.STALE)
    try:
        scene = chunk_registry.ChunkRegistry().scene
        if scene is not None and hasattr(scene, "refresh_timeline"):
            scene.refresh_timeline(start_frame, start_frame + frame_count - 1)
    except Exception:  # noqa: BLE001 - a timeline refresh is a courtesy
        pass


def cache_key(node):
    """What the cook cache keys a bake on: its inputs, while Run asks (sim_bake)."""
    return sim_bake.input_key(node)


def execute(node):
    """Bake on cook headless; interactively, only read a cached bake back.

    The first cook of a session (opening the scene) reads the bake back from the cook cache if
    one was made from exactly these inputs, so a scene reopens simulated. Later cooks put the
    last bake back -- the framework clears this layer before each -- rather than simulate
    again on every tweak. Under ``usdchunks run`` nobody can press Run, so it bakes.
    """
    if sim_bake.is_unattended():
        _run(node, None, None)
        return
    if getattr(node, "_particle_cache_checked", False):
        sim_bake.restore(node, node.get_edit_context())
        return
    node._particle_cache_checked = True
    node._particle_cache_only = True
    try:
        _run(node, None, None)
    finally:
        node._particle_cache_only = False


def _cancel(owner, notice, sender):
    sim_bake.cancel(owner.get_chunk())


def _reset(owner, notice, sender):
    sim_bake.forget(owner.get_chunk())  # or the next cook would put it back
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)
    owner.get_chunk().set_state(chunk.ChunkFlags.State.STALE)


def _mark_stale(owner, notice, sender):
    owner.get_chunk().set_state(chunk.ChunkFlags.State.STALE)


def _add_colliders(owner, notice, sender):
    param = owner.get_chunk().get_param("collision_path")
    scene = chunk_registry.ChunkRegistry().scene
    selected = scene.get_selected_prims() if scene else []
    existing = _path_list(param.value)
    for prim in selected or ():
        path = str(prim.GetPath())
        if path and path != "/" and path not in existing:
            existing.append(path)
    param.value = "\n".join(existing)


def _clear_colliders(owner, notice, sender):
    owner.get_chunk().get_param("collision_path").value = ""


_NOT_INPUTS = {"run_sim", "reset_sim", "cancel_sim", "add_collision", "clear_collision", "readout",
               "particles_out", "surface_out", "revision", "cache_result"}


def main():
    chunk_api = chunk.Chunk("Particle Solver")

    chunk_api.add_param(chunk.LineEdit(
        "source_prim", "Source",
        flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Where the material starts.\n"
        "A mesh: its volume is filled (it must be closed).\n"
        "A Scatter Brush's Painted Points: a ball of Blob Radius at every dab.\n"
        "A Scatter Points instancer: a ball the size of every instance.",
    ))
    chunk_api.add_param(chunk.Menu(
        "material", "Material", menu_items=_MATERIALS, default_value=WATER,
        tooltip="Water flows and splashes. Sand piles up at its friction angle. Snow packs "
        "and breaks. Jelly wobbles and springs back.",
    ))
    run = chunk.Button("run_sim", "Run Simulation", tooltip="Simulate and bake every frame.")
    run.add_callback(_run)
    chunk_api.add_param(run)
    reset = chunk.Button("reset_sim", "Reset", tooltip="Clear the bake from this chunk's layer.")
    reset.add_callback(_reset)
    chunk_api.add_param(reset)
    cancel = chunk.Button("cancel_sim", "Cancel", tooltip="Stop the simulation that is running.")
    cancel.add_callback(_cancel)
    chunk_api.add_param(cancel)
    chunk_api.add_param(chunk.Label("readout", default_value="not simulated yet"))

    emit = chunk.Group("Emission", icon="sparkles", flags=_GF)
    emit.add_param(chunk.Spinbox(
        "blob_radius", "Blob Radius", default_value=0.0,
        tooltip="Radius of the ball of material at each painted point, in scene units. "
        "0 = half the brush radius the point was painted with.",
    ))
    emit.add_param(chunk.Vector(
        "initial_velocity", "Velocity", param=chunk.Spinbox(), length=3, suffixes="XYZ",
        default_value=(0.0, 0.0, 0.0),
        tooltip="Starting velocity of all the material, in scene units per second.",
    ))
    emit.add_param(chunk.Spinbox(
        "max_particles", "Max Particles", default_value=200000, is_int=True,
        tooltip="Upper limit. A source that fills to more is thinned at random, which makes "
        "the material lighter and softer -- lower Grid Resolution instead if you can.",
    ))
    emit.add_param(chunk.Spinbox("seed", "Seed", default_value=0, is_int=True))
    emit.set_state(chunk.ChunkFlags.State.COLLAPSED)
    chunk_api.add_param(emit)

    col = chunk.Group("Collision", icon="zap", flags=_GF)
    col.add_param(chunk.TextEdit(
        "collision_path", "Collider Prims", flags=_PF.LABELED,
        tooltip="One prim per line. Static meshes are walls and floors. A PointInstancer or "
        "an animated prim -- an RBD Solver bake -- moves through the material as spheres, "
        "so run the RBD Solver first.",
    ))
    add = chunk.Button("add_collision", "Add Selected")
    add.add_callback(_add_colliders)
    col.add_param(add)
    clear = chunk.Button("clear_collision", "Clear")
    clear.add_callback(_clear_colliders)
    col.add_param(clear)
    col.add_param(chunk.Slider(
        "friction", "Friction", min_value=0.0, max_value=2.0, default_value=0.4,
        tooltip="How much the material drags on colliders and the domain walls.",
    ))
    col.set_state(chunk.ChunkFlags.State.COLLAPSED)
    chunk_api.add_param(col)

    dom = chunk.Group("Domain", icon="grid", flags=_GF)
    container = chunk.LineEdit(
        "container_prim", "Container", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Optional. The simulation happens inside this prim's bounding box, whose "
        "walls are solid. Without one, the box is the source grown by Margin, reaching down "
        "to the colliders under it.",
    )
    dom.add_param(container)
    dom.add_param(chunk.Spinbox(
        "margin", "Margin", default_value=1.0,
        tooltip="Room around the source, as a multiple of its size, when there is no Container.",
    ))
    dom.add_param(chunk.Spinbox(
        "grid_res", "Grid Resolution", default_value=64, is_int=True,
        tooltip="Cells along the domain's longest side. Detail and cost both go up with the "
        "cube of this: 64 is a quick look, 128 a good one on a GPU.",
    ))
    dom.set_state(chunk.ChunkFlags.State.COLLAPSED)
    chunk_api.add_param(dom)

    mat = chunk.Group("MaterialSettings", "Material", icon="sphere", flags=_GF)
    mat.add_param(chunk.Spinbox(
        "stiffness", "Stiffness", default_value=0.0,
        tooltip="0 = the material's own default. Higher is firmer and slower to simulate.",
    ))
    mat.add_param(chunk.Slider(
        "sand_angle", "Sand Friction Angle", min_value=5.0, max_value=60.0, default_value=30.0,
        tooltip="Sand only. The steepest slope a pile will hold, in degrees.",
    ))
    mat.set_state(chunk.ChunkFlags.State.COLLAPSED)
    chunk_api.add_param(mat)

    out = chunk.Group("Output", icon="download", flags=_GF)
    out.add_param(chunk.LineEdit(
        "sim_path", "Output Path", default_value="/ParticleSim",
        tooltip="Where the particles are written: <path>/Particles (animated) and "
        "<path>/SurfacePoints (the final frame's top surface).",
    ))
    out.add_param(chunk.Spinbox(
        "point_scale", "Point Size", default_value=1.0,
        tooltip="Drawn size of each particle, relative to the particle spacing.",
    ))
    out.add_param(chunk.Menu(
        "render_as", "Render As", menu_items=_RENDER_MODES, default_value=_AS_POINTS,
        tooltip="Points is a UsdGeom.Points cloud -- cheap, and what Storm draws fastest.\n"
        "Instanced Spheres also writes a PointInstancer of real spheres, which is the only "
        "one V-Ray renders: it draws a Points cloud as a single point.",
    ))
    material = chunk.LineEdit(
        "material_prim", "Material", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Optional. A Material prim to bind to the particles. A renderer needs this -- "
        "displayColor is a viewport fallback, not a shader.",
    )
    out.add_param(material)
    out.add_param(chunk.Spinbox(
        "surface_spacing", "Surface Spacing", default_value=0.0,
        tooltip="Distance between Surface Points, in scene units. 0 = four particle spacings. "
        "Set it to your prototype's size before wiring into Scatter Points.",
    ))
    drive = chunk.LineEdit(
        "drive_instancer", "Drive Instancer", flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Optional. A Scatter Points instancer to animate with the material.\n"
        "The same instancer as Source: each instance follows its own ball -- jelly apples.\n"
        "Any other: the instances are carried along as debris -- leaves on water.",
    )
    out.add_param(drive)
    chunk_api.add_param(out)

    # A PRIM_SELECTION field fills itself from the viewport selection when it is built, which
    # is what Source wants and exactly wrong for these two optional ones: whatever was selected
    # when the chunk was created -- usually the surface just painted on -- became the Container
    # (the whole domain grew to its size and the material to a handful of particles) and the
    # Drive Instancer. They start empty; picking one is a decision.
    for optional in (container, drive, material):
        optional.value = ""

    sim = chunk.Group("Simulation", icon="waves", flags=_GF)
    sim.add_param(chunk.Spinbox("start_frame", "Start Frame", default_value=1, is_int=True))
    sim.add_param(chunk.Spinbox("frame_count", "Frame Count", default_value=72, is_int=True))
    sim.add_param(chunk.Spinbox("fps", "FPS", default_value=24.0))
    sim.add_param(chunk.Spinbox(
        "gravity", "Gravity", default_value=0.0,
        tooltip="Along the up axis, in scene units/s². 0 = Earth gravity in this stage's "
        "units (-981 in a centimetre scene, -9.81 in a metre one).",
    ))
    sim.add_param(chunk.Spinbox(
        "substeps", "Substeps", default_value=0, is_int=True,
        tooltip="Solver steps per frame. 0 = as few as stay stable, worked out from the "
        "material's stiffness and the grid.",
    ))
    sim.add_param(chunk.Menu(
        "device", "Device", menu_items=["GPU", "CPU"], default_value="GPU",
        tooltip="GPU falls back to CPU when there is no CUDA or Vulkan device.",
    ))
    sim.set_state(chunk.ChunkFlags.State.COLLAPSED)
    chunk_api.add_param(sim)

    # Published so the node graph has sockets: Surface Points wires into Scatter Points'
    # Painted Points; Particles into anything else that reads a point cloud.
    for name, label in (("particles_out", "Particles"), ("surface_out", "Surface Points")):
        chunk_api.add_param(chunk.LineEdit(name, label, flags=_PF.OUTPUT | _PF.LABELED))
    # Bumped by every bake. The paths above only change on the first one, so without this a
    # Scatter Points wired to Surface Points kept the first bake's points forever. Same trick
    # as the Scatter Brush's Revision: a param write is what makes downstream recook.
    chunk_api.add_param(chunk.Slider("revision", "Bake", default_value=0, is_int=True,
                                     flags=_PF.OUTPUT))

    for p in chunk_api.get_params():
        if getattr(p, "name", None) not in _NOT_INPUTS:
            p.add_callback(_mark_stale, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED,
                           send_on_create=False)
    chunk_api.set_state(chunk.ChunkFlags.State.STALE)
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.DYNAMICS,
        icon="waves",
        requirements=["taichi>=1.7,<2", "numpy>=2,<3"],
        cache_cook=True,
        description=(
            "Bakes an MLS-MPM simulation of water, sand, snow or jelly with Taichi (GPU when\n"
            "available). The material starts in a mesh's volume, at a Scatter Brush's painted\n"
            "points, or as a ball per Scatter Points instance. Static meshes collide; an RBD\n"
            "Solver bake pushes through it as moving spheres.\n"
            "\n"
            "Outputs a time-sampled Points prim, and Surface Points -- the settled top\n"
            "surface -- to wire into Scatter Points' Painted Points. Drive Instancer animates\n"
            "a Scatter Points instancer with the flow. Requires the 'taichi' package."
        ),
    )
