"""MuJoCo RBD Solver -- rigid body simulation via MuJoCo.

Requires: pxr (OpenUSD), usd_chunks, mujoco, numpy.
Optional: scipy -- smoother/denser heightfield colliders; falls back gracefully if absent.
"""

from __future__ import annotations

import collections
import importlib.util
import hashlib
import logging
import math
import random
import sys
from collections.abc import Generator
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt

from usd_chunks import chunk, chunk_registry, chunk_utils, cook_cache, sdf_authoring, sim_bake
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks import chunk_stage

try:
    import mujoco as _mj
    import numpy as _np

    _HAS_MUJOCO = True
except ImportError:
    _HAS_MUJOCO = False
    _np = None

logger = logging.getLogger(__name__)


def _force_phases(seed: int) -> tuple:
    """Six fixed phase offsets for the turbulence field, so a given Seed always gives the
    same gusts -- a re-run has to reproduce the take you just approved.
    """
    rnd = random.Random(int(seed))
    return tuple(rnd.uniform(0.0, 2.0 * math.pi) for _ in range(6))


def _turbulence_accel(pos, t: float, amp: float, scale: float, speed: float, phases) -> tuple:
    """Turbulence acceleration at a world point, as a smooth flow field.

    Each axis is driven by the OTHER two axes' positions, so neighbouring points get
    different pushes and a body tumbles through the field instead of being shoved
    uniformly (which is what a plain per-frame random force feels like -- jitter, not
    wind). Sampled analytically from position + time, so it costs nothing to evaluate,
    needs no grid, and is continuous however small the substep gets.

    *scale* is the world-space size of one gust, *speed* how fast the field drifts.
    """
    if amp <= 0.0:
        return (0.0, 0.0, 0.0)
    k = (2.0 * math.pi / scale) if scale > 1e-6 else 0.0
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    p = phases
    return (
        math.sin(k * y + speed * t + p[0]) * math.cos(k * z - speed * t + p[1]) * amp,
        math.sin(k * z + speed * t + p[2]) * math.cos(k * x - speed * t + p[3]) * amp,
        math.sin(k * x + speed * t + p[4]) * math.cos(k * y - speed * t + p[5]) * amp,
    )


def _apply_wind(model, data, body_ids, wind, amp: float, scale: float, speed: float, phases, t: float) -> None:
    """Push wind + turbulence onto each dynamic body for the coming step.

    Applied as a FORCE, deliberately not divided out by mass. A heavier body therefore
    accelerates less and shrugs the wind off, which is both the real behaviour of a
    constant force and the intuition to reach for ("make it lighter so the wind catches
    it"). It is also the only knob that gives Mass a visible job at all: gravity is
    mass-independent by nature, so on a plain drop Mass changes nothing no matter what
    it is set to.

    xfrc_applied persists across steps, so this rewrites it every substep rather than
    accumulating; with no wind and no turbulence it is never written and stays zero.
    """
    if amp <= 0.0 and not any(wind):
        return
    for bid in body_ids:
        fx, fy, fz = _turbulence_accel(data.xpos[bid], t, amp, scale, speed, phases)
        data.xfrc_applied[bid][0] = wind[0] + fx
        data.xfrc_applied[bid][1] = wind[1] + fy
        data.xfrc_applied[bid][2] = wind[2] + fz


#: What counts as "stopped moving", as the norm of the whole model's generalised velocity,
#: and how long a settle is allowed to chase it before giving up. The cap is what keeps a
#: contact that jitters forever (a sphere microsliding in a bowl) from hanging the bake --
#: it stops at whatever it had, which is no worse than the frame-count-limited result this
#: replaced.
_SETTLE_VEL_THRESH = 0.02
_SETTLE_MAX_EXTRA_FRAMES = 240


def _step_until_at_rest(model, data, substeps: int, sub_dt: float, sim_t: float,
                        apply_forces=None) -> float:
    """Keep stepping past the frame budget until the whole model stops moving.

    `frame_count` is an animation-preview length, not a promise that anything has settled:
    a body still dropping, a door still swinging or a pile still shuffling would otherwise
    be baked mid-motion and called rest, which is exactly why "Settle To Rest" looked like
    it only worked sometimes. Whether it did came down to whether the caller's frame count
    happened to be generous enough for that particular scene.

    Stepping is substep-accurate and keeps applying forces, so a settle under wind rests
    where the wind holds it rather than where it would have fallen without it.

    Returns the advanced sim time; the caller reads the settled pose off `data` itself.
    """
    extra = 0
    while (
        extra < _SETTLE_MAX_EXTRA_FRAMES
        and float(_np.linalg.norm(data.qvel)) > _SETTLE_VEL_THRESH
    ):
        for _ in range(substeps):
            if apply_forces is not None:
                apply_forces(sim_t)
            _mj.mj_step(model, data)
            sim_t += sub_dt
        extra += 1
    return sim_t


def _build_heightfield(cprim: Usd.Prim, up_idx: int, resolution: int = 64, min_cell: float = 0.0,
                       geometry: tuple | None = None) -> dict | None:
    """Sample the top surface of *cprim*'s meshes into a MuJoCo heightfield.

    A bowl/plate/terrain inner surface is single-valued in height, so a
    heightfield gives stable, smooth *concave* collision -- unlike a mesh geom,
    which MuJoCo only ever collides as its convex hull (this build has no
    generic mesh-SDF collider).

    *min_cell* caps the grid resolution so a cell never gets much smaller than
    the dynamic body: a sphere resting in a sub-cell-sized concave pit at the
    bowl bottom produces a singular/over-constrained contact that explodes
    (empirically once cell size drops below ~0.1x the body radius). Pass the
    body radius * ~0.15 so the bottom stays gentle.

    Returns a dict for spec.add_hfield() / a static body, or None if the prim
    can't be represented as a heightfield (degenerate / flat / no mesh).

    Limitation: only the upper surface is captured (no overhangs) -- fine for a bowl/plate/
    terrain (single-valued height), wrong for a cup with a lip curling back over itself.
    Falls back to a box collider when None.

    *geometry* is an already-extracted (verts, tris) for *cprim*, in world space -- exactly
    what _collider_triangles returns. The caller reads the collider once and hands the
    result to whichever collider this run ends up building.
    """
    if _np is None:
        return None

    pts_world, tris = geometry if geometry is not None else _collider_triangles(cprim)
    if len(pts_world) < 16:
        return None

    V = _np.asarray(pts_world, dtype=_np.float64)
    lat = [j for j in range(3) if j != up_idx]
    a0, a1 = lat[0], lat[1]
    min0, max0 = float(V[:, a0].min()), float(V[:, a0].max())
    min1, max1 = float(V[:, a1].min()), float(V[:, a1].max())
    if (max0 - min0) < 1e-6 or (max1 - min1) < 1e-6:
        return None

    # Resolution: clamp to a hard cap, then to a cell-size floor for stability.
    N = max(8, min(int(resolution), 72))
    if min_cell > 1e-6:
        span = min(max0 - min0, max1 - min1)
        N = max(8, min(N, int(span / min_cell) + 1))
    col = _np.clip(((V[:, a0] - min0) / (max0 - min0) * (N - 1)).round().astype(int), 0, N - 1)
    # Flipped (max1 - ...), not (... - min1): MuJoCo's hfield row 0 sits at local -Y, row N-1
    # at local +Y, but the -90-about-X quat below (needed to point local +Z, the height axis,
    # along world up) sends local +Y to world -Z, not +Z -- a real rotation can't avoid that
    # (X->X, Y->Z, Z->Y has determinant -1, a mirror, not a rotation). Flipping which end of
    # the sampled range row 0 starts from cancels that inversion so the baked collider actually
    # matches the source mesh's orientation instead of its Z-mirror.
    row = _np.clip(((max1 - V[:, a1]) / (max1 - min1) * (N - 1)).round().astype(int), 0, N - 1)

    H = _np.full((N, N), -_np.inf)
    up = V[:, up_idx]
    # Top surface = highest sampled point per cell.
    _np.maximum.at(H, (row, col), up)

    # Then the faces themselves, not just their corners. Binning vertices alone leaves a
    # cell empty wherever no vertex happens to land in it, and the nearest-cell fill below
    # then borrows whatever height was nearest -- for a big flat low-poly slab (a tabletop:
    # all its vertices around the perimeter/details, none across the middle) the nearest
    # sampled height is the slab's own UNDERSIDE, so the collider would grow a
    # several-unit-deep phantom trench straight through the middle of the table, and
    # anything rolling across it would sink into that trench. Rasterising each triangle across the cells it actually
    # covers gives the real surface height there instead.
    #
    # Only triangles big enough to OWN a cell need this. A cell takes its height from the
    # nearest vertex that rounds into it, so a triangle can only be responsible for a cell
    # no vertex reached if it is wide enough to cover that cell's centre while all three of
    # its own corners round elsewhere -- which needs a footprint of at least half a cell.
    # Anything smaller is already described by its own corners above.
    #
    # This is the difference between a dense collider being usable and not. The test used
    # to be `ceil(max) - floor(min) > 1 cell`, which is true for almost any triangle that
    # merely straddles a cell boundary -- so a 200k-triangle terrain ran the loop below
    # 200k times to re-derive heights its own vertices had already supplied, at ~40 us
    # each. Measuring the real footprint instead skips essentially all of them: the loop
    # now runs only for the genuinely big faces it was written for (a tabletop's one-quad
    # top, a terrain's coarse base) and a dense mesh falls straight through it.
    if tris:
        TRI = _np.asarray(tris, dtype=_np.int64)
        tc = (V[TRI][:, :, a0] - min0) / (max0 - min0) * (N - 1)
        tr = (max1 - V[TRI][:, :, a1]) / (max1 - min1) * (N - 1)
        tu = V[TRI][:, :, up_idx]
        c_lo = _np.clip(_np.floor(tc.min(axis=1)).astype(int), 0, N - 1)
        c_hi = _np.clip(_np.ceil(tc.max(axis=1)).astype(int), 0, N - 1)
        r_lo = _np.clip(_np.floor(tr.min(axis=1)).astype(int), 0, N - 1)
        r_hi = _np.clip(_np.ceil(tr.max(axis=1)).astype(int), 0, N - 1)
        spans_a_cell = (tc.max(axis=1) - tc.min(axis=1) >= 0.5) | (
            tr.max(axis=1) - tr.min(axis=1) >= 0.5
        )
        multi_cell = _np.nonzero(spans_a_cell & ((c_hi - c_lo + 1) * (r_hi - r_lo + 1) > 1))[0]
        for t in multi_cell:
            x1, y1, x2, y2, x3, y3 = tc[t, 0], tr[t, 0], tc[t, 1], tr[t, 1], tc[t, 2], tr[t, 2]
            den = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            if abs(den) < 1e-12:  # degenerate seen from above (a vertical wall) -- no top surface
                continue
            cc, rr = _np.meshgrid(
                _np.arange(c_lo[t], c_hi[t] + 1), _np.arange(r_lo[t], r_hi[t] + 1)
            )
            l1 = ((y2 - y3) * (cc - x3) + (x3 - x2) * (rr - y3)) / den
            l2 = ((y3 - y1) * (cc - x3) + (x1 - x3) * (rr - y3)) / den
            l3 = 1.0 - l1 - l2
            inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
            if not inside.any():
                continue
            z = l1 * tu[t, 0] + l2 * tu[t, 1] + l3 * tu[t, 2]
            block = H[r_lo[t]:r_hi[t] + 1, c_lo[t]:c_hi[t] + 1]
            _np.maximum(block, _np.where(inside, z, -_np.inf), out=block)

    filled = _np.isfinite(H)
    if not filled.any():
        return None
    if not filled.all():
        # Fill empty cells from the nearest sampled cell so the grid is dense.
        try:
            from scipy.ndimage import distance_transform_edt

            idx = distance_transform_edt(~filled, return_distances=False, return_indices=True)
            H = H[tuple(idx)]
        except Exception:
            H[~filled] = float(H[filled].min())

    # Light smoothing removes binning staircase so the body rolls cleanly.
    try:
        from scipy.ndimage import gaussian_filter

        H = gaussian_filter(H, sigma=0.6, mode="nearest")
    except Exception:
        pass

    h0 = float(H.min())
    elev = float(H.max() - h0)
    if elev < 1e-6:
        return None
    heights = ((H - h0) / elev).astype(_np.float32).flatten()

    pos = [0.0, 0.0, 0.0]
    pos[a0] = (min0 + max0) / 2.0
    pos[a1] = (min1 + max1) / 2.0
    pos[up_idx] = h0

    # Orient the hfield so its local +Z (height axis) points along world up.
    if up_idx == 1:  # Y-up: -90° about X
        a = math.pi / 4
        quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
    else:  # Z-up: identity (X-up falls back to box before reaching here)
        quat = [1.0, 0.0, 0.0, 0.0]

    # size[3] is how far MuJoCo extends solid material BELOW the sampled surface -- what
    # actually stops a body rather than letting it through. It was a hard-coded 1.0, which
    # is not a length that means anything on its own: this is whatever unit the stage is
    # authored in, so 1.0 is a centimetre on a cm-scale kitchen set. It only ever held up
    # because a collider's grid minimum usually sits well below the surface a body rests
    # on (a table's edge trim samples ~8 units under its own top), leaving a thick slab by
    # accident. A bowl has no such margin -- the lowest point of the grid IS the interior
    # floor the body lands on -- so once the grid became accurate (see the rasterisation
    # pass above) the solid under that floor was 1.0 thick and an apple dropped into the
    # bowl punched straight through it to the catch plane below. Scale it with the
    # collider instead, so it is genuinely solid at any scene scale.
    base = max(elev, 0.1 * max(max0 - min0, max1 - min1))
    size = [(max0 - min0) / 2.0, (max1 - min1) / 2.0, elev, base]
    return {"nrow": N, "ncol": N, "size": size, "heights": heights, "pos": pos, "quat": quat}


def _geom_bounds(prim: Usd.Prim) -> Gf.Range3d | None:
    """World-space bounds of *prim*'s geometry, or None when it has none."""
    bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    rng = bc.ComputeWorldBound(prim).ComputeAlignedRange()
    return None if rng.IsEmpty() else rng


def _collider_triangles(cprim: Usd.Prim, world: bool = True) -> tuple[list, list]:
    """(verts, tris) for every mesh under *cprim*, in world space."""
    xc = UsdGeom.XformCache()
    verts: list = []
    tris: list = []
    for p in Usd.PrimRange(cprim):
        mesh = UsdGeom.Mesh(p)
        if not mesh:
            continue
        pts = mesh.GetPointsAttr().Get()
        if not pts:
            continue
        wxf = xc.GetLocalToWorldTransform(p)
        base = len(verts)
        for pt in pts:
            w = wxf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2])) if world else Gf.Vec3d(pt[0], pt[1], pt[2])
            verts.append((float(w[0]), float(w[1]), float(w[2])))
        counts = mesh.GetFaceVertexCountsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not counts or not indices:
            continue
        k = 0
        for c in counts:
            if c >= 3 and k + c <= len(indices):
                i0 = base + indices[k]
                for t in range(1, c - 1):
                    tris.append((i0, base + indices[k + t], base + indices[k + t + 1]))
            k += c
    return verts, tris


def _add_triangle_soup(
    spec: _mj.MjSpec,
    body,
    cprim: Usd.Prim,
    name: str,
    thickness: float,
    friction: float,
    contact_tc: float,
    xform_inv: Gf.Matrix4d | None = None,
    geometry: tuple | None = None,
    progress=None,
) -> int:
    """Add one thin convex prism geom per collider triangle. Returns the geom count.

    This is the collider that works for *any* shape. MuJoCo collides a mesh geom as its
    convex hull, so a single mesh geom turns a cup into a solid plug; a heightfield can
    only describe one height per cell, so it turns the cup's walls into cliffs a body
    penetrates and sticks in. Extruding each triangle backwards along its own normal
    gives a set of small convex solids whose union *is* the surface -- concave, two-sided,
    exact, and every contact is convex-convex, which is the pair MuJoCo is most robust at.

    Cost is one geom per triangle. Measured on a 608-triangle cup: 0.09 s to compile,
    43 us/step against a convex-hull body (vs 8 us for a single hull geom) -- the whole
    120-frame sim in half a second. Triangle budgets in the caller keep a 200k-triangle
    terrain from going down this path.

    *thickness* is how far each prism extends behind its triangle: the depth of solid
    material a fast body has to be stopped by within one substep. Too thin and a body
    tunnels through between substeps; too thick and opposite walls of a thin-shelled
    object overlap each other.

    *xform_inv* expresses the prisms in some frame other than world -- used for an animated
    collider, whose geoms have to live in its own local frame so the mocap body can be
    driven with the prim's absolute world pose each frame.

    *geometry* is an already-extracted (verts, tris) for *cprim*. The caller has to walk
    the collider's meshes anyway to decide whether the triangle count fits its budget, and
    on a production collider that walk is the whole mesh -- so it hands the result down
    rather than making this re-read every point and face a second time.

    *progress*, if given, is called with (triangles done, total) every few hundred: a big
    collider takes minutes, and that is also where a Cancel is noticed.
    """
    verts, tris = geometry if geometry is not None else _collider_triangles(cprim)
    if not tris:
        return 0

    if xform_inv is not None:
        verts = [tuple(xform_inv.Transform(Gf.Vec3d(*v))) for v in verts]

    V = _np.asarray(verts, dtype=_np.float64)
    count = 0
    for i, (a, b, c) in enumerate(tris):
        if progress is not None and i % 250 == 0:
            progress(i, len(tris))
        v0, v1, v2 = V[a], V[b], V[c]
        n = _np.cross(v1 - v0, v2 - v0)
        ln = float(_np.linalg.norm(n))
        if ln < 1e-12:
            continue  # sliver/degenerate triangle: no volume to extrude, skip it
        back = (-n / ln) * thickness
        prism = _np.array(
            [v0, v1, v2, v0 + back, v1 + back, v2 + back],
            dtype=_np.float32,
        )
        ma = spec.add_mesh()
        ma.name = f"{name}_t{i}"
        ma.uservert = prism.flatten()
        g = body.add_geom()
        g.type = _mj.mjtGeom.mjGEOM_MESH
        g.meshname = ma.name
        g.friction = [friction, 0.005, 0.0001]
        g.solref = [contact_tc, 1.0]
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3
        count += 1
    return count


class _ColliderRead:
    """What the collider build needs from one collision prim, read on the main thread.

    The build itself -- one MuJoCo geom per triangle for a Mesh collider, quadratic in their
    count -- runs on sim_bake's worker, where the stage must not be touched. A 225k-triangle
    rock cluster set to Mesh used to build there on the main thread, and usdview hung.
    """

    __slots__ = ("prim", "path", "geometry", "bounds", "is_anim", "rest", "friction")

    def __init__(self, prim: Usd.Prim):
        self.prim = prim
        self.path = str(prim.GetPath())
        self.geometry = _collider_triangles(prim)
        self.bounds = _geom_bounds(prim)
        self.is_anim = any(op.GetNumTimeSamples() > 1 for op in UsdGeom.Xformable(prim).GetOrderedXformOps())
        self.rest = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
        # Its bound physics material's friction, or None for the chunk's Friction.
        self.friction = _material_friction(prim)

    @property
    def bounds_key(self) -> tuple | None:
        """The world bounds as a plain tuple, for the cache key."""
        if self.bounds is None:
            return None
        lo, hi = self.bounds.GetMin(), self.bounds.GetMax()
        return tuple(float(v) for v in lo) + tuple(float(v) for v in hi)


def _read_colliders(colliders) -> list[_ColliderRead]:
    """Each collider as a _ColliderRead: prims are read now, reads are passed through."""
    return [c if isinstance(c, _ColliderRead) else _ColliderRead(c) for c in (colliders or [])]


def _add_colliders(
    spec: _mj.MjSpec,
    collision_prims: list[Usd.Prim] | None,
    up_idx: int,
    friction: float,
    contact_tc: float,
    dyn_radius: float,
    hfield_res: int,
    floor_drop: float = 0.0,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    tri_budget: int = 20000,
    geometry: list | None = None,
    progress=None,
) -> list[tuple[str, Usd.Prim]]:
    """Add static/kinematic collision bodies to *spec*.

    No collision prims â†’ infinite ground plane. Otherwise each prim becomes, by mode:

    * ``Mesh`` -- one thin convex prism per triangle (see _add_triangle_soup): exact for
      any shape, concave or thin-walled, and the only option that lets a body go *inside*
      a cup or a bottle. Animated colliders get the same thing on a mocap body.
    * ``Heightfield`` -- a 2.5D elevation grid: cheap for terrain and huge meshes, but only
      one surface height per cell, so walls become cliffs.
    * ``Box`` -- the collider's bounding box.
    * ``Auto`` (default) -- Mesh while the collider's triangle count fits *tri_budget*,
      else Heightfield, else Box.

    *dyn_radius* sizes the heightfield cell-size cap to the dynamic body. *geometry* is
    one already-extracted (verts, tris) per entry of *collision_prims*; every branch below
    reads from it, so a collider's meshes are walked once per run however it ends up
    being collided.

    *collision_prims* may be prims or _ColliderRead entries; given reads, nothing here
    touches the stage, so it can run on a worker thread. *progress* goes to
    _add_triangle_soup.

    Returns mocap_col_names: list of (body_name, prim) needing per-frame updates.
    """
    reads = _read_colliders(collision_prims)
    collision_prims = [r.prim for r in reads]
    if geometry is None:
        geometry = [r.geometry for r in reads]
    mocap_col_names = []

    if not collision_prims:
        # Infinite ground plane, oriented to the scene's up axis.
        plane_body = spec.worldbody.add_body()
        plane_body.name = "ground"
        if floor_drop:
            ppos = [0.0, 0.0, 0.0]
            ppos[up_idx] = -floor_drop
            plane_body.pos = ppos
        if up_idx == 1:  # Y-up: rotate plane so its normal points +Y
            a = math.pi / 4  # half-angle of -90Â° rotation around X
            plane_body.quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
        pg = plane_body.add_geom()
        pg.type = _mj.mjtGeom.mjGEOM_PLANE
        pg.size = [0.0, 0.0, 0.01]
        pg.friction = [friction, 0.005, 0.0001]
        pg.condim = 3
        return mocap_col_names

    # Catch-all ground plane at the lowest point of the colliders -- the "table"
    # the bowl sits on. An infinite plane is a rock-solid (non-singular) contact,
    # so it reliably catches anything a collider ejects AND any body that
    # rolls/spills off the rim, instead of letting it fall forever.
    ground_up = None
    for read in reads:
        cr = read.bounds
        if cr is not None:
            b = float(cr.GetMin()[up_idx])
            ground_up = b if ground_up is None else min(ground_up, b)
    if ground_up is not None:
        gp_body = spec.worldbody.add_body()
        gp_body.name = "catch_ground"
        gpos = [0.0, 0.0, 0.0]
        gpos[up_idx] = ground_up - floor_drop
        gp_body.pos = gpos
        if up_idx == 1:  # Y-up: orient plane normal to +Y
            a = math.pi / 4
            gp_body.quat = [math.cos(a), -math.sin(a), 0.0, 0.0]
        gpg = gp_body.add_geom()
        gpg.type = _mj.mjtGeom.mjGEOM_PLANE
        gpg.size = [0.0, 0.0, 0.01]
        gpg.friction = [friction, 0.005, 0.0001]
        gpg.solref = [contact_tc, 1.0]
        gpg.condim = 3

    default_friction = friction
    for i, read in enumerate(reads):
        cp, is_anim, bounds = read.prim, read.is_anim, read.bounds
        # The collider's own material wins; the catch plane above keeps the chunk's.
        friction = default_friction if read.friction is None else read.friction

        # --- exact triangle-soup collider (the general case) ---
        soup = collider_mode in ("Auto", "Mesh")
        if soup and bounds is not None:
            tris = geometry[i][1]
            if not tris or (collider_mode == "Auto" and len(tris) > tri_budget):
                soup = False
            else:
                thickness = collider_thickness
                if thickness <= 0.0:
                    # Auto thickness. It has to be thick enough that a body cannot cross it
                    # between substeps, yet thin enough that the prism behind one face does
                    # not poke out of the opposite face -- which on a flat slab would raise
                    # the surface bodies rest on, and in a thin-walled vessel would eat into
                    # the cavity. Take the smallest of three bounds: half the dynamic body,
                    # a quarter of the collider's thinnest dimension, and 2% of its size.
                    sz = bounds.GetSize()
                    diag = float(Gf.Vec3d(sz).GetLength())
                    min_extent = min(abs(float(sz[0])), abs(float(sz[1])), abs(float(sz[2])))
                    thickness = max(
                        min(0.5 * dyn_radius, 0.25 * min_extent, 0.02 * diag),
                        1e-6,
                    )
                cbody = spec.worldbody.add_body()
                cbody.name = f"col_{i}"
                xform_inv = None
                if is_anim:
                    # Mocap body: MuJoCo re-poses it from data.mocap_* each frame, so an
                    # animated collider keeps its real shape instead of degrading to a box.
                    # Its prisms have to be expressed in the prim's own frame for that pose
                    # to mean anything, hence xform_inv.
                    rest = read.rest
                    xform_inv = rest.GetInverse()
                    rt = rest.ExtractTranslation()
                    rq = rest.RemoveScaleShear().ExtractRotation().GetQuat()
                    rqi = rq.GetImaginary()
                    cbody.pos = [float(rt[0]), float(rt[1]), float(rt[2])]
                    cbody.quat = [rq.GetReal(), float(rqi[0]), float(rqi[1]), float(rqi[2])]
                    cbody.mocap = True
                    mocap_col_names.append((f"col_{i}", cp))
                n = _add_triangle_soup(
                    spec, cbody, cp, f"col_{i}", thickness, friction, contact_tc, xform_inv,
                    geometry=geometry[i], progress=progress,
                )
                if n:
                    continue
                soup = False

        # --- concave collision via heightfield (static, non-X-up prims only) ---
        hf = None
        if collider_mode in ("Auto", "Heightfield") and not is_anim and up_idx != 0:
            # Keep hfield cells from getting much smaller than the body, or
            # the contact at the bowl bottom goes singular and explodes.
            hf = _build_heightfield(
                cp, up_idx, hfield_res, min_cell=0.15 * dyn_radius, geometry=geometry[i]
            )

        if hf is not None:
            hname = f"hf_{i}"
            mh = spec.add_hfield()
            mh.name = hname
            mh.nrow = hf["nrow"]
            mh.ncol = hf["ncol"]
            mh.size = hf["size"]
            mh.userdata = hf["heights"]

            col_body = spec.worldbody.add_body()
            col_body.name = f"col_{i}"
            col_body.pos = hf["pos"]
            col_body.quat = hf["quat"]
            bg = col_body.add_geom()
            bg.type = _mj.mjtGeom.mjGEOM_HFIELD
            bg.hfieldname = hname
            bg.friction = [friction, 0.005, 0.0001]
            bg.solref = [contact_tc, 1.0]
            bg.solimp = [0.95, 0.99, 0.001, 0.5, 2]
            # condim=3 (normal + sliding friction). condim=6 adds torsional/
            # rolling constraints that over-constrain the singular contact at
            # a bowl's concave bottom vertex and explode. The catch-all ground
            # plane above backstops anything the concave bottom ejects.
            bg.condim = 3
            continue

        # Fallback: bbox-fitted box (animated prims, or non-heightfieldable).
        bname = f"col_{i}"
        col_body = spec.worldbody.add_body()
        col_body.name = bname
        # Centre the body on the collider's GEOMETRY, not on its transform origin: a prim
        # from a production asset commonly carries geometry authored far from that origin
        # (the kitchen set's cup has its points 255 units away from it), and a box placed
        # at the origin then collides with empty space while bodies fall through the visible
        # mesh.
        if bounds is not None:
            ctr = (bounds.GetMin() + bounds.GetMax()) * 0.5
            col_body.pos = [float(ctr[0]), float(ctr[1]), float(ctr[2])]
        else:
            col_t = read.rest.ExtractTranslation()
            col_body.pos = [float(col_t[0]), float(col_t[1]), float(col_t[2])]
        if is_anim:
            col_body.mocap = True
            mocap_col_names.append((bname, cp))

        if bounds is not None:
            csz = bounds.GetMax() - bounds.GetMin()
            half = [csz[0] / 2, csz[1] / 2, csz[2] / 2]
            # A flat collider (grid/plane) has a zero extent on one axis, which MuJoCo
            # rejects ("size N must be positive in geom"). Give any degenerate axis a
            # small thickness so it becomes a thin slab the bodies can rest on.
            min_half = max(1e-3, 1e-3 * max(half))
            half = [max(h, min_half) for h in half]
            bg = col_body.add_geom()
            bg.type = _mj.mjtGeom.mjGEOM_BOX
            bg.size = half
            bg.friction = [friction, 0.005, 0.0001]
            bg.solref = [contact_tc, 1.0]
            bg.condim = 3

    return mocap_col_names


#: Built collider specs, keyed by the collider geometry and the settings that shape it.
#: Small on purpose -- a handful of entries covers "the artist is re-running the same
#: scene", which is the whole point, and each entry holds a full MjSpec.
_COLLIDER_SPEC_CACHE: collections.OrderedDict[bytes, tuple] = collections.OrderedDict()
_COLLIDER_SPEC_CACHE_MAX = 4


def _collider_cache_key(geometry, *settings) -> bytes:
    """A digest of the collider geometry plus everything that changes its shape.

    Hashing the extracted triangles rather than watching USD for edits is what makes this
    safe: the vertices are already in world space, so a moved collider, an edited mesh, a
    swapped reference and a changed prototype all fall out as a different digest with no
    invalidation logic to get wrong. Reading them costs ~0.3s on a 200k-triangle collider
    against the ~60s of building it, and the read has to happen anyway.

    What is deliberately NOT in here: friction and the contact timeconstant. They are the
    two an artist re-runs to tune, they do not change a single vertex, and they are patched
    onto the copy instead -- see _apply_contact_params.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(repr(settings).encode())
    for verts, tris in geometry:
        va = _np.asarray(verts, dtype=_np.float64)
        ta = _np.asarray(tris, dtype=_np.int64)
        h.update(_np.asarray(va.shape + ta.shape, dtype=_np.int64).tobytes())
        h.update(va.tobytes())
        h.update(ta.tobytes())
    return h.digest()


def _bounds_key(prim: Usd.Prim) -> tuple | None:
    """The collider's world bounds as a plain tuple, for the cache key."""
    rng = _geom_bounds(prim)
    if rng is None:
        return None
    lo, hi = rng.GetMin(), rng.GetMax()
    return tuple(float(v) for v in lo) + tuple(float(v) for v in hi)


def _apply_contact_params(spec, friction: float, contact_tc: float, own: dict | None = None) -> None:
    """Set friction and contact stiffness on every geom of a collider-only spec.

    Every collider geom the builder makes -- prism, heightfield, box, catch plane -- gets
    exactly these two, so re-setting them on a cached copy reproduces a fresh build without
    rebuilding it. Only ever called on a spec that holds nothing but colliders; the dynamic
    bodies are added afterwards and set their own.

    *own* maps a collider's body name (``col_<i>``) to the friction of its bound physics
    material, which outranks *friction* for that collider's geoms, as in the build.
    """
    for g in spec.geoms:
        g.friction = [friction, 0.005, 0.0001]
        g.solref = [contact_tc, 1.0]
    for name, value in (own or {}).items():
        body = spec.body(name)
        if body is not None:
            for g in body.geoms:
                g.friction = [value, 0.005, 0.0001]


def _collider_spec(
    collision_prims: list[Usd.Prim] | None,
    up_idx: int,
    friction: float,
    contact_tc: float,
    dyn_radius: float,
    hfield_res: int,
    floor_drop: float = 0.0,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    tri_budget: int = 20000,
    progress=None,
) -> tuple:
    """A fresh MjSpec holding this run's colliders, reusing a built one where possible.

    Returns (spec, mocap_col_names). The caller sets its options on the returned spec and
    adds its dynamic bodies to it -- so the colliders keep coming first, as the body-id
    lookups downstream assume.

    Why this exists: a Mesh collider is one MuJoCo geom per triangle, and MjSpec
    construction is quadratic in element count (measured: 5k prisms 3.6s, 10k 14.5s, 20k
    61s -- and MJCF XML measures the same, so it is MuJoCo's, not ours). Copying a built
    spec is 0.02s and compiling the copy 0.08s, whatever its size. So the expensive build
    happens once and every re-run after it -- a friction tweak, a longer frame range, a
    different drop height -- copies.
    """
    reads = _read_colliders(collision_prims)
    prims = [r.prim for r in reads]
    if not prims:
        # No colliders: an infinite ground plane, which costs nothing to build. Caching it
        # would only add a digest of nothing to compare.
        spec = _mj.MjSpec()
        return spec, _add_colliders(
            spec, prims, up_idx, friction, contact_tc, dyn_radius, hfield_res,
            floor_drop, collider_mode, collider_thickness, tri_budget, geometry=[],
        )

    geometry = [r.geometry for r in reads]
    # Two things about a collider shape the spec without showing up in its triangles:
    #
    #  * whether it is animated, which decides between a mocap body and a static one. The
    #    triangles are read at Default(), so keying a collider and then animating it in
    #    place would otherwise hit the static entry it built the first time;
    #  * its bounds, which position the catch-all ground plane and size the box fallback.
    #    A collider with no Mesh under it at all -- a UsdGeom.Cube, say -- contributes no
    #    triangles whatsoever, so its bounds are the only thing that distinguishes it.
    shape_state = [(r.path, r.is_anim, r.bounds_key) for r in reads]
    key = _collider_cache_key(
        geometry, up_idx, dyn_radius, hfield_res, floor_drop, collider_mode,
        collider_thickness, tri_budget, shape_state,
    )

    entry = _COLLIDER_SPEC_CACHE.pop(key, None)
    if entry is None:
        built = _mj.MjSpec()
        mocap = _add_colliders(
            built, reads, up_idx, friction, contact_tc, dyn_radius, hfield_res,
            floor_drop, collider_mode, collider_thickness, tri_budget, geometry=geometry,
            progress=progress,
        )
        # Indices, not prims: a cached Usd.Prim handle expires with its stage, and the
        # caller has the live prims anyway.
        entry = (built, [prims.index(cp) for _, cp in mocap])
    _COLLIDER_SPEC_CACHE[key] = entry
    while len(_COLLIDER_SPEC_CACHE) > _COLLIDER_SPEC_CACHE_MAX:
        _COLLIDER_SPEC_CACHE.popitem(last=False)

    cached, mocap_idx = entry
    # Always a copy, on the miss as well: the caller is about to add dynamic bodies to
    # what it gets back, and the cached spec has to stay collider-only for the next run.
    spec = cached.copy()
    own = {f"col_{i}": r.friction for i, r in enumerate(reads) if r.friction is not None}
    _apply_contact_params(spec, friction, contact_tc, own)
    return spec, [(f"col_{i}", prims[i]) for i in mocap_idx]


def _collider_poses(reads, start_frame: int, frame_count: int) -> dict:
    """Each animated collider's world pose at every frame, read from USD up front.

    Read before the bake leaves the main thread: the collider build and the stepping run on a
    worker (sim_bake), where the stage must not be touched.

    Returns:
        {collider path: [(position, wxyz quaternion) per frame]}.
    """
    poses = {}
    for read in reads:
        if not read.is_anim:
            continue
        track = []
        for frame in range(frame_count):
            kxf = UsdGeom.XformCache(Usd.TimeCode(start_frame + frame)).GetLocalToWorldTransform(read.prim)
            kt = kxf.ExtractTranslation()
            kq = kxf.RemoveScaleShear().ExtractRotation().GetQuat()  # strip scale: a scaled matrix
            ki = kq.GetImaginary()                                     # gives a wrong rotation
            track.append(([float(kt[0]), float(kt[1]), float(kt[2])],
                          [kq.GetReal(), float(ki[0]), float(ki[1]), float(ki[2])]))
        poses[read.path] = track
    return poses


def _mocap_tracks(mocap_ids, poses: dict) -> list:
    """[(mocap id, poses per frame)] for the moving colliders, from _collider_poses."""
    return [(mid, poses[str(cprim.GetPath())]) for mid, cprim in mocap_ids]


def _set_mocap(data, tracks, frame: int) -> None:
    for mid, poses in tracks:
        data.mocap_pos[mid], data.mocap_quat[mid] = poses[frame]


def _sim_prim_gen_mj(
    prim: Usd.Prim,
    collision_prims: list[Usd.Prim] | None,
    frame_count: int,
    gravity: float,
    friction: float,
    restitution: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    dt: float,
    up_idx: int,
    shape_type: str,
    collision_margin: float,
    solver_iters: int,
    translate: Gf.Vec3d,
    rot: Gf.Rotation,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    hull_max_verts: int = 0,
    **kwargs,
) -> Generator[int, None, list[Gf.Matrix4d] | None]:
    """MuJoCo rigid body simulation of one prim -- yields frame index, returns all_mats.

    The one-body case of _sim_prims_gen_mj; see there.
    """
    body = dict(prim=prim, translate=translate, rot=rot, mass=mass, friction=friction,
                restitution=restitution, shape_type=shape_type)
    results = yield from _sim_prims_gen_mj(
        [body], collision_prims, frame_count, gravity, friction, substeps, init_vel, ang_vel,
        start_frame, dt, up_idx, collision_margin, solver_iters, lin_damping, ang_damping,
        hull_max_verts, **kwargs,
    )
    return results[0] if results else None


def _body_frame(prim: Usd.Prim, translate: Gf.Vec3d, rot: Gf.Rotation, shape_type: str,
                hull_max_verts: int) -> dict:
    """Where one dynamic body sits and what its collider is made of (main thread: reads USD)."""
    # The MuJoCo body frame goes on the GEOMETRY, not on the prim's transform origin. A prim
    # from a production asset routinely has its points authored far from that origin
    # (hundreds of units away, in shared model space), and a free joint that far
    # from the mass it carries turns every contact impulse into a huge angular one -- the body
    # is launched across the scene on first touch. Placing the frame on the geometry also
    # makes the box/sphere colliders land on the mesh instead of beside it, and gives a tight
    # box for a rotated prim (world-AABB half-extents would grow with rotation).
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    local_rng = bbox_cache.ComputeUntransformedBound(prim).ComputeAlignedRange()
    world_xf = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
    r0, r1, r2 = world_xf.GetRow3(0), world_xf.GetRow3(1), world_xf.GetRow3(2)
    world_scale = Gf.Vec3d(r0.GetLength(), r1.GetLength(), r2.GetLength())
    world_rot_quat = rot.GetQuat()  # before any user rotation offset is folded in

    half_extents = [0.5, 0.5, 0.5]
    body_centre = Gf.Vec3d(translate[0], translate[1], translate[2])
    if not local_rng.IsEmpty():
        lmin, lmax = local_rng.GetMin(), local_rng.GetMax()
        lsz = lmax - lmin
        half_extents = [
            max(abs(float(lsz[0])) * float(world_scale[0]) * 0.5, 1e-6),
            max(abs(float(lsz[1])) * float(world_scale[1]) * 0.5, 1e-6),
            max(abs(float(lsz[2])) * float(world_scale[2]) * 0.5, 1e-6),
        ]
        body_centre = world_xf.Transform((lmin + lmax) * 0.5)

    # Rest pose of the body frame: the prim's own rotation, centred on its geometry. Baked
    # results are composed against this (see the loop in _sim_prims_gen_mj), so the prim
    # keeps its authored transform structure however far its points sit from its origin.
    rest_mat = Gf.Matrix4d(1.0)
    rest_mat.SetRotate(world_rot_quat)
    rest_mat.SetTranslateOnly(body_centre)

    # Body frame the geoms are expressed in (includes the user's rotation offset).
    body_mat = Gf.Matrix4d(1.0)
    body_mat.SetRotate(rot.GetQuat())
    body_mat.SetTranslateOnly(body_centre)
    body_inv = body_mat.GetInverse()

    # --- collect convex hull vertices if needed ---
    mesh_pts = None
    if shape_type == "Convex Hull":
        _xc = UsdGeom.XformCache()
        hull_verts = []
        for _child in Usd.PrimRange(prim):
            _mesh = UsdGeom.Mesh(_child)
            _pts = _mesh.GetPointsAttr().Get()
            if not _pts:
                continue
            _cxf = _xc.GetLocalToWorldTransform(_child)
            for _pt in _pts:
                _wp = _cxf.Transform(Gf.Vec3d(_pt[0], _pt[1], _pt[2]))
                # Transform world point into the body frame (centred on the geometry).
                _lp = body_inv.Transform(_wp)
                hull_verts.append([float(_lp[0]), float(_lp[1]), float(_lp[2])])
        if hull_verts:
            if hull_max_verts > 0 and len(hull_verts) > hull_max_verts:
                step = max(1, len(hull_verts) // hull_max_verts)
                hull_verts = hull_verts[::step][:hull_max_verts]
            mesh_pts = _np.array(hull_verts, dtype=_np.float32)
        else:
            logger.warning("Convex Hull: no mesh data on %s, falling back to Box", prim.GetPath())

    gq = rot.GetQuat()
    gi = gq.GetImaginary()
    return dict(
        half_extents=half_extents,
        centre=body_centre,
        rest_inv=rest_mat.GetInverse(),
        # The prim's world transform without scale -- the writer re-applies the prim's own
        # scale, so carrying it here as well would square it.
        world_rest=world_xf.RemoveScaleShear(),
        mesh_pts=mesh_pts,
        # MuJoCo quaternion convention matches USD: [w, x, y, z]
        quat=[gq.GetReal(), float(gi[0]), float(gi[1]), float(gi[2])],
    )


def _add_dynamic_body(spec, name: str, joint_name: str, hull_name: str, b: dict, f: dict,
                      contact_tc: float, collision_margin: float, lat, hull_max_verts: int):
    """One free rigid body, as the rigid path makes it: its frame on the geometry centre, a
    free joint, and its shape, mass and contact. Shared with the articulated path, which
    adds the free bodies an articulated asset touches to its own model."""
    half_extents = f["half_extents"]
    centre = f["centre"]
    dyn_body = spec.worldbody.add_body()
    dyn_body.name = name
    # The body frame sits on the geometry centre.
    dyn_body.pos = [float(centre[0]), float(centre[1]), float(centre[2])]
    dyn_body.quat = f["quat"]

    free_jnt = dyn_body.add_joint()
    free_jnt.type = _mj.mjtJoint.mjJNT_FREE
    free_jnt.name = joint_name

    dyn_geom = dyn_body.add_geom()
    dyn_geom.friction = [b["friction"], 0.005, 0.0001]
    dyn_geom.mass = b["mass"]
    # Map restitution (0-1) to solref: lower dampratio = bouncier.
    # solref[0] = timeconst (kept >= 2*timestep for stability), solref[1] = dampratio
    dyn_geom.solref = [contact_tc, 1.0 - max(0.0, min(1.0, b["restitution"])) * 0.9]
    # solimp: [dmin, dmax, width, midpoint, power]
    dyn_geom.solimp = [0.95, 0.99, 0.001, 0.5, 2]
    # condim=3 (normal + sliding friction); condim=6's torsional/rolling
    # constraints destabilise the singular contact at a bowl's concave bottom.
    dyn_geom.condim = 3

    shape_type = b["shape_type"]
    mesh_pts = f["mesh_pts"]
    if shape_type == "Sphere":
        sphere_r = max(half_extents[lat[0]], half_extents[lat[1]])
        radius = max(sphere_r + collision_margin, 0.001)
        dyn_geom.type = _mj.mjtGeom.mjGEOM_SPHERE
        dyn_geom.size = [radius, 0.0, 0.0]

    elif shape_type == "Convex Hull" and mesh_pts is not None:
        hull_ma = spec.add_mesh()
        hull_ma.name = hull_name
        hull_ma.uservert = mesh_pts.flatten()
        # MuJoCo builds a convex hull internally from the vertex cloud --
        # maxhullvert caps the output vertex count for the collision hull.
        hull_ma.maxhullvert = max(hull_max_verts, 64) if hull_max_verts > 0 else 1024
        if collision_margin != 0.0:
            max_ext = max(half_extents) if max(half_extents) > 1e-6 else 1.0
            ms = (max_ext + collision_margin) / max_ext
            hull_ma.scale = [ms, ms, ms]
        dyn_geom.type = _mj.mjtGeom.mjGEOM_MESH
        dyn_geom.meshname = hull_ma.name

    else:
        adj = [max(h + collision_margin, 0.001) for h in half_extents]
        dyn_geom.type = _mj.mjtGeom.mjGEOM_BOX
        dyn_geom.size = adj


def _sim_prims_gen_mj(
    bodies: list[dict],
    collision_prims: list[Usd.Prim] | None,
    frame_count: int,
    gravity: float,
    friction: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    dt: float,
    up_idx: int,
    collision_margin: float,
    solver_iters: int,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    hull_max_verts: int = 0,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    floor_drop: float = 0.0,
    wind: tuple | list = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
    settle_only: bool = False,
    tri_budget: int = 20000,
    progress=None,
) -> Generator[int, None, list[list[Gf.Matrix4d]] | None]:
    """MuJoCo rigid body simulation of several prims in one world -- yields frame index,
    returns each body's world matrices per frame, in the order of `bodies`.

    Each entry of `bodies` is a dict: prim, translate, rot (its world placement), and its
    own mass, friction, restitution and shape_type. They share one world, so they collide
    with each other as well as with the colliders. `friction` is the colliders'.

    Uses MjSpec to build the scene programmatically from USD mesh data (no file I/O).
    Concave static colliders use heightfields (see _build_heightfield); this
    build of MuJoCo has no generic mesh-SDF collider.
    MuJoCo quaternions are [w,x,y,z] matching USD -- no reordering needed.
    """
    frames = [_body_frame(b["prim"], b["translate"], b["rot"], b["shape_type"], hull_max_verts)
              for b in bodies]

    # --- build MjSpec scene ---
    sub_dt = dt / substeps  # per-step timestep
    # Contact stiffness timeconst: must be >= 2*timestep for stability. A stiff
    # 0.01 floor keeps impact penetration tiny (~0.1cm vs ~1.4cm at 0.04), so
    # fast impacts at -981 don't visibly clip. Stability under pile-ups comes
    # from the catch-all ground plane (see _add_colliders), not from softening.
    contact_tc = max(2.0 * sub_dt, 0.01)

    # --- static / kinematic collision bodies ---
    # The spec comes back with the colliders already in it, reused from the last run where
    # nothing about them changed; the dynamic bodies are added below, so colliders still come
    # first and the body-id lookups downstream still hold.
    dyn_radius = max(max(f["half_extents"]) for f in frames) + max(collision_margin, 0.0)
    colliders = _read_colliders(collision_prims)
    poses = _collider_poses(colliders, start_frame, frame_count)
    # Everything below is MuJoCo and Gf only -- the collider build included, which on a big
    # Mesh collider is minutes: in a UI it runs on a worker (sim_bake).
    yield sim_bake.WORKER
    spec, mocap_col_names = _collider_spec(
        colliders,
        up_idx,
        friction,
        contact_tc,
        dyn_radius,
        hfield_res,
        floor_drop,
        collider_mode,
        collider_thickness,
        tri_budget,
        progress=progress,
    )
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.noslip_iterations = 3
    # implicitfast integrates contacts/damping more stably than the default Euler.
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST
    if len(bodies) > 1:
        # Several bodies resting on a collider overflow MuJoCo's default 16 MB arena ("Insufficient
        # arena memory for the number of constraints generated"): a hull on a heightfield makes
        # up to ~50 contacts, so eight chess pieces on their board are ~1,400 constraint rows.
        # With few DOFs MuJoCo picks a DENSE Jacobian, and noslip then works on a matrix that
        # grows with the square of the rows -- 19 MB for those eight pieces. A sparse Jacobian
        # is the same solve in ~4 MB. The arena is also sized the way the instancer path sizes
        # it, for the contacts that grow with the body count.
        spec.option.jacobian = _mj.mjtJacobian.mjJAC_SPARSE
        spec.memory = max(64 * 1024 * 1024, len(bodies) * 2 * 1024 * 1024)

    lat = [j for j in range(3) if up_idx != j]

    # --- dynamic bodies --- (the first keeps the names a single body always had)
    def _name(base: str, i: int) -> str:
        return base if i == 0 else f"{base}_{i}"

    for i, (b, f) in enumerate(zip(bodies, frames)):
        _add_dynamic_body(spec, _name("dynamic", i), _name("free_joint", i), _name("hull_dyn", i), b, f,
                          contact_tc, collision_margin, lat, hull_max_verts)

    # --- compile ---
    try:
        model = spec.compile()
        if len(bodies) > 1:
            # Two targets that already overlap at frame 0 (pieces modelled touching, a prop
            # sunk into its neighbour) would be shoved apart by a separating impulse and
            # launched. Leave only those pairs out; targets that meet later still collide.
            names = {_name("dynamic", i) for i in range(len(bodies))}
            if _exclude_initial_overlaps(spec, model, only=names):
                model = spec.compile()
    except Exception as _ce:
        logger.warning("Scene compile failed: %s", _ce)
        return

    data = _mj.MjData(model)

    bids = []
    for i in range(len(bodies)):
        # Apply linear/angular damping on the freejoint DOFs directly on the compiled model.
        # DOFs 0-2 are translational, 3-5 are rotational.
        jnt_id = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, _name("free_joint", i))
        if jnt_id >= 0:
            dof0 = model.jnt_dofadr[jnt_id]
            model.dof_damping[dof0 : dof0 + 3] = lin_damping
            model.dof_damping[dof0 + 3 : dof0 + 6] = ang_damping
            # Set initial velocity
            data.qvel[dof0 : dof0 + 3] = init_vel[:3]
            data.qvel[dof0 + 3 : dof0 + 6] = ang_vel[:3]
        bids.append(_mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, _name("dynamic", i)))

    # Resolve mocap body ids for kinematic collision updates
    mocap_ids = []
    for bname, cprim in mocap_col_names:
        mbid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, bname)
        mid = int(model.body_mocapid[mbid])
        mocap_ids.append((mid, cprim))

    logger.debug("Compiled - %d bodies, %d geoms, %d dynamic", model.nbody, model.ngeom, len(bodies))

    # Initialize forward kinematics so xpos/xquat are valid on frame 0
    _mj.mj_forward(model, data)

    def _world_mats():
        mats = []
        for bid, f in zip(bids, frames):
            pos = data.xpos[bid]
            qmj = data.xquat[bid]  # [w, x, y, z] -- same convention as USD, no reordering needed
            body_now = Gf.Matrix4d()
            body_now.SetRotate(Gf.Quatd(float(qmj[0]), float(qmj[1]), float(qmj[2]), float(qmj[3])))
            body_now.SetTranslateOnly(Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2])))
            # The body frame sits on the geometry, not on the prim's origin, so the prim's
            # world transform is its rest pose carried by the body's rigid motion since frame 0:
            #   W(t) = W(0) . B(0)^-1 . B(t)
            # This is exact wherever the prim's origin happens to be relative to its points,
            # and collapses to plain B(t) when they coincide.
            mats.append(f["world_rest"] * f["rest_inv"] * body_now)
        return mats

    # --- simulation loop ---
    phases = _force_phases(turb_seed)
    sim_t = 0.0
    results: list[list[Gf.Matrix4d]] = [[] for _ in bodies]
    tracks = _mocap_tracks(mocap_ids, poses)
    for frame in range(frame_count):
        _set_mocap(data, tracks, frame)

        # Sample state before stepping (frame N shows where the bodies were at the start of step N)
        for mats, mat in zip(results, _world_mats()):
            mats.append(mat)

        for _ in range(substeps):
            _apply_wind(model, data, bids, wind, turbulence, turb_scale, turb_speed, phases, sim_t)
            _mj.mj_step(model, data)
            sim_t += sub_dt

        yield frame

    if settle_only:
        # Same contract as the articulated path: run on until the bodies have actually
        # stopped, then append that pose as the final entry, which is the one
        # _write_prim_results takes for a settle.
        sim_t = _step_until_at_rest(
            model, data, substeps, sub_dt, sim_t,
            lambda t: _apply_wind(model, data, bids, wind, turbulence,
                                  turb_scale, turb_speed, phases, t),
        )
        for mats, mat in zip(results, _world_mats()):
            mats.append(mat)

    return results

def _bake_scale(prim: Usd.Prim, xform_cache) -> tuple:
    """The scale the bake has to re-apply: the prim's WORLD scale, not its local one.

    The bake converts a world-space result into local space with parent_world_inv, which
    divides out every ancestor's scale -- and the simulation result is rotation and
    translation only, so nothing puts that back. Reading the LOCAL scale compensates for the
    wrong thing: a mesh under a parent scaled by 100 has a local scale of 1, so nothing
    would be re-applied and the baked op would come out at 0.01, exactly the 1/100 the
    parent contributes.

    It only ever worked because of where it was tested. A prim at the ROOT has the same
    local and world scale, so the two coincide and the mistake is invisible -- which is
    exactly why simulating /food_apple_01_4k was fine and simulating its mesh was not.
    """
    world = xform_cache.GetLocalToWorldTransform(prim)
    return tuple(world.GetRow3(i).GetLength() for i in range(3))


def _upstream_xform(prim: Usd.Prim, op_name: str,
                    time: Usd.TimeCode = Usd.TimeCode.Default()) -> Gf.Matrix4d:
    """Everything applying to `prim` before this chunk's own op.

    Read as the composed local transform with this chunk's own contribution divided back
    out, so it reflects what is actually in effect -- muting and layer strength included --
    rather than a re-composition of the op list by hand.

    Ordering, verified rather than assumed: for xformOpOrder [rot, translate] USD composes
    T * R, so the last op multiplies on the left. An appended op therefore gives
    local = mine * upstream, hence upstream = mine^-1 * local.
    """
    xformable = UsdGeom.Xformable(prim)
    local = xformable.GetLocalTransformation(time)
    if isinstance(local, tuple):
        local = local[0]
    mine = Gf.Matrix4d(1.0)
    attr = prim.GetAttribute(op_name)
    if attr and attr.IsValid():
        current = attr.Get(time)
        if current is not None:
            mine = Gf.Matrix4d(current)
    return mine.GetInverse() * local


def _write_prim_results(
    stage: Usd.Stage,
    prim: Usd.Prim,
    layer: Sdf.Layer,
    parent_world_inv: Gf.Matrix4d,
    all_mats: list[Gf.Matrix4d],
    start_frame: int,
    scale_xyz: tuple[float, float, float] | None = None,
    node=None,
    settle_only: bool = False,
) -> None:
    """Write single-prim simulation results to USD (main-thread only).

    settle_only writes the resting pose as the attribute's default with no time samples,
    for the same reason the articulated path does (see _write_articulated_results): a prim
    carrying PhysicsRigidBodyAPI *and* animated transforms is contradictory, and a
    downstream simulator ends up fighting the keyframes.
    """
    if scale_xyz and any(abs(s - 1.0) > 1e-6 for s in scale_xyz):
        sx, sy, sz = scale_xyz
        # Build a scale matrix to left-multiply each sim RT frame (row-vector convention:
        # scale_mat * mat scales rows 0,1,2 by sx,sy,sz while preserving translation row 3).
        scale_mat = Gf.Matrix4d()
        scale_mat.SetScale(Gf.Vec3d(sx, sy, sz))

        def _local(mat):
            return scale_mat * mat * parent_world_inv
    else:

        def _local(mat):
            return mat * parent_world_inv

    # Bake into THIS chunk's own named op, and derive the order the same way every other
    # transform-authoring chunk does.
    #
    # Authoring the plain xformOp:transform and hard-setting the order to just that would
    # be wrong: the solver sits downstream, so its one-item list would outrank everyone
    # else's and exclude an upstream Transform's op from the order entirely -- placing a
    # prim with a Transform would stop working the moment a simulation existed.
    op_name = chunk_utils.xform_op_name(node) if node is not None else "xformOp:transform"
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        order = (
            chunk_gizmo.xform_op_order(node, prim, op_name)
            if node is not None
            else [op_name]
        )
        order_attr = prim.GetAttribute("xformOpOrder")
        if not order_attr:
            order_attr = prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray)
        order_attr.Set(order)
        xf_attr = prim.GetAttribute(op_name)
        if not xf_attr:
            xf_attr = prim.CreateAttribute(op_name, Sdf.ValueTypeNames.Matrix4d)
        # Bake this chunk's SHARE, not the absolute placement.
        #
        # _local() gives where the body should end up in its parent's space. The bake
        # composes on top of the upstream ops, so writing that absolute value in would apply
        # whatever placed the body (a Transform upstream of the solver, say) a second time.
        #
        # Same conversion the gizmo write-back does, for the same reason: an absolute answer
        # arriving at something that contributes a share has to have the share divided out.
        upstream = _upstream_xform(prim, op_name)
        inv_upstream = upstream.GetInverse()
        if settle_only:
            # The settled pose is the last entry -- the generator appends it after stepping
            # on past frame_count. Default only: no samples to fight, and nothing that
            # depends on the timeline being parked anywhere in particular.
            xf_attr.Set(_local(all_mats[-1]) * inv_upstream)
        else:
            # An animated upstream -- an Attach carrying the body along a keyed hand -- is
            # divided out at each frame's own time, as the articulated path does. Its
            # default-time value left the animation in, on top of the simulated motion.
            # Read into a list before writing: write_samples authors inside a change block.
            animated = UsdGeom.Xformable(prim).TransformMightBeTimeVarying()
            samples = [
                (start_frame + i, _local(mat) * (
                    _upstream_xform(prim, op_name, Usd.TimeCode(start_frame + i)).GetInverse()
                    if animated else inv_upstream))
                for i, mat in enumerate(all_mats)
            ]
            sim_bake.write_samples(xf_attr, samples, default=_local(all_mats[0]) * inv_upstream)
    sim_bake.keep(node, layer)
    if settle_only:
        logger.debug("Settled %s", prim.GetPath())
        return
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + len(all_mats) - 1)
    logger.debug("Baked %d frames on %s", len(all_mats), prim.GetPath())


def simulate_mujoco(*args, **kwargs):
    """Simulate one prim as a rigid body and bake it, straight through. Returns the world
    matrices per frame.

    See _prim_steps for the arguments; sim_bake.run runs those steps with the solve on a
    worker instead.
    """
    return sim_bake.drive(_prim_steps(*args, **kwargs))


def _prim_steps(
    stage: Usd.Stage,
    prim: Usd.Prim,
    layer: Sdf.Layer,
    frame_count: int,
    fps: float,
    gravity: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    parent_world_inv: Gf.Matrix4d,
    collision_prims: list[Usd.Prim] | None = None,
    friction: float = 0.5,
    restitution: float = 0.1,
    scale_xyz: tuple[float, float, float] | None = None,
    shape_type: str = "Convex Hull",
    **kwargs,
) -> list[Gf.Matrix4d] | None:
    """Run MuJoCo simulation of one prim: the one-target case of _prims_steps.

    `node` is the chunk itself, needed only so the bake lands in this chunk's own named
    xformOp rather than the shared one -- see _write_prim_results.
    """
    target = dict(prim=prim, parent_world_inv=parent_world_inv, scale_xyz=scale_xyz, mass=mass,
                  friction=friction, restitution=restitution, shape_type=shape_type)
    results = yield from _prims_steps(
        stage, [target], layer, frame_count, fps, gravity, substeps, init_vel, ang_vel,
        start_frame, collision_prims=collision_prims, friction=friction, **kwargs,
    )
    return results[0] if results else None


def _prims_steps(
    stage: Usd.Stage,
    targets: list[dict],
    layer: Sdf.Layer,
    frame_count: int,
    fps: float,
    gravity: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    collision_prims: list[Usd.Prim] | None = None,
    friction: float = 0.5,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    collision_margin: float = 0.0,
    solver_iters: int = 30,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    tri_budget: int = 20000,
    floor_drop: float = 0.0,
    wind: tuple | list = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
    node=None,
    settle_only: bool = False,
    progress=None,
) -> list[list[Gf.Matrix4d]] | None:
    """Run one MuJoCo simulation of several prims together and bake each.

    Each target is a dict: prim, parent_world_inv, scale_xyz, and its own mass, friction,
    restitution and shape_type. They share one world, so they collide with each other.
    `friction` is the colliders'. Returns each target's world matrices per frame.
    """
    if not _HAS_MUJOCO:
        logger.warning("mujoco not installed. Run: pip install mujoco")
        return None

    # Paths, not prims, for the write: a prim handle can expire while the solve runs.
    paths = [t["prim"].GetPath() for t in targets]
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    bodies = []
    for t in targets:
        world_xf = xform_cache.GetLocalToWorldTransform(t["prim"])
        # Strip scale/shear first: ExtractRotation() on a scaled matrix returns
        # garbage (e.g. a 100x-scaled -90deg matrix decodes as a 360deg identity),
        # which would bake the wrong orientation onto the prim and desync the
        # collision shape from the visual mesh.
        bodies.append(dict(
            t,
            translate=world_xf.ExtractTranslation(),
            rot=world_xf.RemoveScaleShear().ExtractRotation(),
        ))

    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2

    results = yield from _sim_prims_gen_mj(
        bodies,
        collision_prims,
        frame_count,
        gravity,
        friction,
        substeps,
        init_vel,
        ang_vel,
        start_frame,
        1.0 / fps,
        up_idx,
        collision_margin,
        solver_iters,
        lin_damping,
        ang_damping,
        hfield_res=hfield_res,
        # Both of these were accepted by this function and then dropped on the floor, so the
        # chunk's Collider Mode and Collider Thickness did nothing at all for a single rigid
        # body -- every collider was built as if Auto, whatever the UI said. The instancer
        # and articulated paths forwarded them all along, which is why it went unnoticed.
        collider_mode=collider_mode,
        collider_thickness=collider_thickness,
        floor_drop=floor_drop,
        wind=wind,
        turbulence=turbulence,
        turb_scale=turb_scale,
        turb_speed=turb_speed,
        turb_seed=turb_seed,
        settle_only=settle_only,
        tri_budget=tri_budget,
        progress=progress,
    )
    yield sim_bake.MAIN

    if not results:
        logger.warning("Simulation produced no results")
        return None

    # Write results using the proven writer. The prims are looked up again: the stage may
    # have changed while the solve ran on a worker.
    for t, path, all_mats in zip(targets, paths, results):
        prim = stage.GetPrimAtPath(path)
        _write_prim_results(stage, prim, layer, t["parent_world_inv"], all_mats, start_frame,
                            t["scale_xyz"], node, settle_only)
    if not settle_only:
        logger.debug("Baked %d frames on %d prims", frame_count, len(targets))
    return results

def simulate_mujoco_articulated(*args, **kwargs) -> dict | None:
    """Simulate an articulated asset and bake a transform per link, straight through.

    See _articulated_steps for the arguments; sim_bake.run runs those steps with the solve
    on a worker instead.
    """
    return sim_bake.drive(_articulated_steps(*args, **kwargs))


def _articulated_steps(
    stage: Usd.Stage,
    roots,
    layer: Sdf.Layer,
    frame_count: int,
    fps: float,
    gravity: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    collision_prims: list[Usd.Prim] | None = None,
    friction: float = 0.5,
    restitution: float = 0.1,
    solver_iters: int = 30,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    floor_drop: float = 0.0,
    free_root: bool = True,
    settle_only: bool = False,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    collision_margin: float = 0.0,
    node=None,
    wind: tuple | list = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
    progress=None,
    cache: dict | None = None,
    extra: list | None = None,
) -> dict | None:
    """Run an articulated MuJoCo simulation and bake a transform per link.

    `extra`: plain rigid bodies to simulate in the same world, as _run sorts them (path,
    parent_world_inv, scale_xyz, mass, friction, restitution, shape_type) -- what the robot
    handles. Each is written as the rigid path writes it.

    The counterpart of simulate_mujoco for an asset that is a mechanism rather than a
    single rigid body. Each link keeps its own baked transform, so a door's swing lands on
    the door prim instead of being flattened into the body.
    """
    if not _HAS_MUJOCO:
        logger.warning("mujoco not installed. Run: pip install mujoco")
        return None

    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    # The free bodies' starting placement, as _prims_steps reads it.
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    free = []
    for t in extra or ():
        prim = stage.GetPrimAtPath(t["path"])
        if not prim or not prim.IsValid():
            continue
        world_xf = xform_cache.GetLocalToWorldTransform(prim)
        free.append(dict(t, prim=prim, translate=world_xf.ExtractTranslation(),
                         rot=world_xf.RemoveScaleShear().ExtractRotation()))
    gen = _sim_articulated_gen_mj(
        roots,
        frame_count=frame_count,
        gravity=gravity,
        friction=friction,
        restitution=restitution,
        mass=mass,
        substeps=substeps,
        start_frame=start_frame,
        dt=1.0 / fps,
        up_idx=up_idx,
        solver_iters=solver_iters,
        collision_prims=collision_prims,
        hfield_res=hfield_res,
        collider_mode=collider_mode,
        collider_thickness=collider_thickness,
        floor_drop=floor_drop,
        free_root=free_root,
        init_vel=init_vel,
        ang_vel=ang_vel,
        lin_damping=lin_damping,
        ang_damping=ang_damping,
        collision_margin=collision_margin,
        wind=wind,
        turbulence=turbulence,
        turb_scale=turb_scale,
        turb_speed=turb_speed,
        turb_seed=turb_seed,
        settle_only=settle_only,
        progress=progress,
        cache=cache,
        extra=free,
    )

    results = yield from gen
    yield sim_bake.MAIN

    if not results:
        logger.warning("Articulated simulation produced no results")
        return None

    _write_articulated_results(stage, roots, layer, results, start_frame, settle_only, node)
    for i, t in enumerate(free):
        # Looked up again: the stage may have changed while the solve ran on a worker.
        prim = stage.GetPrimAtPath(t["path"])
        mats = results.get(f"__free_{i}")
        if prim and prim.IsValid() and mats:
            _write_prim_results(stage, prim, layer, t["parent_world_inv"], mats, start_frame,
                                t["scale_xyz"], node, settle_only)
    logger.debug("Baked %d frames across %d links and %d free bodies", frame_count, len(results), len(free))
    return results


def _proto_hull_verts(stage: Usd.Stage, proto_path, cap: int = 200) -> _np.ndarray | None:
    """One prototype's mesh vertices, for its convex-hull collider (extracted once).

    Points are gathered in the prototype's world-bound space -- the same space the
    caller measures proto extent/centre in -- so per-instance scaling stays
    consistent with the sphere/box path. Subsampled to *cap* points to keep the
    per-asset hull build cheap. Returns None if the prototype has no mesh data.
    """
    if _np is None or not proto_path:
        return None
    proto_prim = stage.GetPrimAtPath(proto_path)
    if not proto_prim:
        return None
    xc = UsdGeom.XformCache()
    verts = []
    for c in Usd.PrimRange(proto_prim):
        mesh = UsdGeom.Mesh(c)
        pts = mesh.GetPointsAttr().Get()
        if not pts:
            continue
        cxf = xc.GetLocalToWorldTransform(c)
        for p in pts:
            w = cxf.Transform(Gf.Vec3d(p[0], p[1], p[2]))
            verts.append([float(w[0]), float(w[1]), float(w[2])])
    if len(verts) < 4:
        return None
    arr = _np.asarray(verts, dtype=_np.float32)
    if len(arr) > cap:
        arr = arr[:: max(1, len(arr) // cap)][:cap]
    return arr


def simulate_mujoco_instancer(*args, **kwargs) -> None:
    """Simulate a PointInstancer's instances as rigid bodies and bake them, straight through.

    See _instancer_steps for the arguments; sim_bake.run runs those steps with the solve on a
    worker instead.
    """
    sim_bake.drive(_instancer_steps(*args, **kwargs))


def _instancer_steps(
    stage: Usd.Stage,
    prim: Usd.Prim,
    layer: Sdf.Layer,
    collision_prims: list[Usd.Prim] | None,
    frame_count: int,
    fps: float,
    gravity: float,
    mass: float,
    substeps: int,
    init_vel: list[float],
    ang_vel: list[float],
    start_frame: int,
    friction: float = 0.5,
    restitution: float = 0.1,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    shape_type: str = "Sphere",
    collision_margin: float = 0.0,
    solver_iters: int = 30,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    tri_budget: int = 20000,
    floor_drop: float = 0.0,
    wind: tuple | list = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
    settle_only: bool = False,
    progress=None,
) -> None:
    """Run MuJoCo multi-body simulation for a UsdGeom.PointInstancer using MjSpec.

    Each instance gets a free joint and collides with the other instances and the
    same collider stack as the single-prim path (concave heightfield + backstop
    for a bowl, else a ground plane). *shape_type* picks the per-instance collider:
    "Sphere" (fast, most stable in dense piles), "Box", or "Convex Hull" (accurate,
    heavier -- the shared prototype hull is built once and reused per distinct
    scale). A sphere is rotationally symmetric, so the rotation offset and each
    instance's own orientation only influence the physics for Box / Convex Hull.
    """
    if not _HAS_MUJOCO:
        logger.warning("mujoco not installed. Run: pip install mujoco")
        return

    instancer = UsdGeom.PointInstancer(prim)
    positions_attr = instancer.GetPositionsAttr()
    orientations_attr = instancer.GetOrientationsAttr()
    positions = positions_attr.Get()
    orientations = orientations_attr.Get()
    if not positions:
        logger.warning("No positions on PointInstancer")
        return

    num_instances = len(positions)
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    world_inv = world_xf.GetInverse()
    # Instancer orientations are in instancer-local space; convert to/from world
    # with the instancer's own rotation (scale stripped -- see note in simulate_mujoco).
    inst_rot = world_xf.RemoveScaleShear().ExtractRotation()
    inst_rot_inv = inst_rot.GetInverse()

    # Each prototype's UNSCALED bound (extents + centre), measured once per prototype.
    # The scatter tool clears the prototype's transform and bakes the real scale into
    # per-instance scales, so the collider must multiply the proto extent by each
    # instance's scale -- otherwise the spheres are ~100x too small and the instances
    # clip straight through everything.
    #
    # Per PROTOTYPE, not just the first one: a scatter of mixed assets (the usual case --
    # a rock set, a leaf set) otherwise gives every instance the shape of prototype 0, so
    # the big ones sink into the ground and the small ones hover above it.
    proto_targets = instancer.GetPrototypesRel().GetTargets()
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    proto_exts: list[Gf.Vec3d] = []
    proto_ctrs: list[Gf.Vec3d] = []
    for tgt in proto_targets:
        ext, ctr = Gf.Vec3d(1.0, 1.0, 1.0), Gf.Vec3d(0.0, 0.0, 0.0)
        proto_prim = stage.GetPrimAtPath(tgt)
        if proto_prim:
            # Local, NOT world: the instancer's world transform (e.g. a Transform node feeding it)
            # is already applied to the body positions via world_xf. Using ComputeWorldBound here
            # would double-count it in the collider centre/extent -- offsetting and rescaling the
            # collision geometry away from the visible instance. The two agree when the
            # instancer is untransformed.
            rng = bbox_cache.ComputeLocalBound(proto_prim).ComputeAlignedRange()
            if not rng.IsEmpty():
                ext = rng.GetMax() - rng.GetMin()
                ctr = (rng.GetMin() + rng.GetMax()) * 0.5
        proto_exts.append(ext)
        proto_ctrs.append(ctr)
    if not proto_exts:
        proto_exts.append(Gf.Vec3d(1.0, 1.0, 1.0))
        proto_ctrs.append(Gf.Vec3d(0.0, 0.0, 0.0))

    scales = instancer.GetScalesAttr().Get()
    proto_indices = instancer.GetProtoIndicesAttr().Get()

    def _proto_index(idx: int) -> int:
        """Which prototype instance *idx* is. An out-of-range index falls back to 0 rather
        than raising: a half-authored instancer is a state the graph passes through.
        """
        if proto_indices is None or idx >= len(proto_indices):
            return 0
        pi = int(proto_indices[idx])
        return pi if 0 <= pi < len(proto_exts) else 0

    def _instance_scale(idx: int) -> tuple[float, float, float]:
        if scales and idx < len(scales):
            s = scales[idx]
            return float(s[0]), float(s[1]), float(s[2])
        return 1.0, 1.0, 1.0

    def _instance_extent(idx: int) -> tuple[float, float, float]:
        """The instance's world-size box: its own prototype's extent times its own scale."""
        sx, sy, sz = _instance_scale(idx)
        ext = proto_exts[_proto_index(idx)]
        return float(ext[0]) * sx, float(ext[1]) * sy, float(ext[2]) * sz

    def _instance_centre(idx: int) -> Gf.Vec3d:
        """Offset from the instance's origin to its own geometry centre, scaled."""
        sx, sy, sz = _instance_scale(idx)
        ctr = proto_ctrs[_proto_index(idx)]
        return Gf.Vec3d(float(ctr[0]) * sx, float(ctr[1]) * sy, float(ctr[2]) * sz)

    def _instance_radius(idx: int) -> float:
        return max(_instance_extent(idx)) / 2.0

    # Representative radius (largest instance) for the heightfield cell-size cap.
    max_radius = max([0.5] + [_instance_radius(i) for i in range(num_instances)]) + max(collision_margin, 0.0)

    dt = 1.0 / fps

    # --- Build MjSpec ---
    sub_dt = dt / substeps
    contact_tc = max(2.0 * sub_dt, 0.01)

    # Same colliders as the single-prim path (bowl heightfield + backstop, etc.), and the
    # spec they come back in is the one the instances are then added to -- so the colliders
    # still precede them, as the body-id lookups below assume.
    # Convex hull: extract EACH prototype's geometry once, then build one mesh asset per
    # (prototype, distinct instance scale) pair -- MuJoCo computes the hull per asset.
    hull_bases: list = []
    if shape_type == "Convex Hull":
        for pi, tgt in enumerate(proto_targets):
            verts = _proto_hull_verts(stage, tgt)
            if verts is None:
                logger.warning(
                    "Convex Hull: prototype %s has no mesh data, falling back to Sphere for it", tgt
                )
            else:
                # Recentre on that prototype's geometry centre: the body frame sits there, so
                # the hull has to be expressed about it rather than about the proto's origin.
                ctr = proto_ctrs[pi]
                verts = verts - _np.asarray(
                    [float(ctr[0]), float(ctr[1]), float(ctr[2])], dtype=_np.float32
                )
            hull_bases.append(verts)
    hull_bases += [None] * (len(proto_exts) - len(hull_bases))

    colliders = _read_colliders(collision_prims)
    poses = _collider_poses(colliders, start_frame, frame_count)
    # Everything below until MAIN is MuJoCo and Gf only -- the collider build included, which
    # on a big Mesh collider is minutes: in a UI it runs on a worker (sim_bake).
    yield sim_bake.WORKER
    spec, mocap_col_names = _collider_spec(
        colliders,
        up_idx,
        friction,
        contact_tc,
        max_radius,
        hfield_res,
        floor_drop,
        collider_mode,
        collider_thickness,
        tri_budget,
        progress=progress,
    )
    # Many piled bodies generate many contacts/constraints; the default solver
    # arena (~16 MB) overflows. Scale the allocation with the instance count.
    spec.memory = max(64 * 1024 * 1024, num_instances * 2 * 1024 * 1024)
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.noslip_iterations = 3
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST

    dyn_dampratio = 1.0 - max(0.0, min(1.0, restitution)) * 0.9

    off_rot = None  # no rotation offset on this chunk any more; see main()

    hull_mesh_cache: dict[tuple[int, float, float, float], str] = {}

    def _hull_mesh_for(pi: int, sx: float, sy: float, sz: float) -> str:
        key = (pi, round(sx, 5), round(sy, 5), round(sz, 5))
        name = hull_mesh_cache.get(key)
        if name is not None:
            return name
        name = f"hull{len(hull_mesh_cache)}"
        ma = spec.add_mesh()
        ma.name = name
        ma.uservert = hull_bases[pi].flatten()
        msx, msy, msz = sx, sy, sz
        if collision_margin != 0.0:
            # Grow the hull outward by ~collision_margin via a uniform scale bump
            # (same approximation the single-prim path uses).
            ext = proto_exts[pi]
            max_ext = max(float(ext[0]) * sx, float(ext[1]) * sy, float(ext[2]) * sz) / 2.0
            if max_ext > 1e-6:
                mf = (max_ext + collision_margin) / max_ext
                msx, msy, msz = sx * mf, sy * mf, sz * mf
        ma.scale = [msx, msy, msz]
        ma.maxhullvert = 64
        hull_mesh_cache[key] = name
        return name

    # Per-instance bodies
    for i in range(num_instances):
        lp = positions[i]
        pos_world = world_xf.Transform(Gf.Vec3d(float(lp[0]), float(lp[1]), float(lp[2])))
        px = float(pos_world[0])
        py = float(pos_world[1])
        pz = float(pos_world[2])

        if orientations and i < len(orientations):
            q = orientations[i]
            qi = q.GetImaginary()
            local_q = Gf.Quatd(float(q.GetReal()), float(qi[0]), float(qi[1]), float(qi[2]))
        else:
            local_q = Gf.Quatd(1.0, 0.0, 0.0, 0.0)
        # to world: instancer rotation * instance-local orientation * offset
        world_rot = inst_rot * Gf.Rotation(local_q)
        if off_rot is not None:
            world_rot = world_rot * off_rot
        wq = world_rot.GetQuat()
        wqi = wq.GetImaginary()

        pi = _proto_index(i)
        sx, sy, sz = _instance_scale(i)
        ext_x, ext_y, ext_z = _instance_extent(i)
        radius_i = max(_instance_radius(i) + collision_margin, 1e-4)
        # Offset from the instance's own origin to its geometry centre, scaled. The body
        # frame goes on the geometry (see the single-prim path): a prototype whose points are
        # authored away from its origin -- anything referenced out of a production asset --
        # otherwise gives every instance a long lever arm between its free joint and its mass,
        # and the pile detonates on first contact.
        ctr_world = world_rot.TransformDir(_instance_centre(i))

        body = spec.worldbody.add_body()
        body.name = f"b{i}"
        body.pos = [px + float(ctr_world[0]), py + float(ctr_world[1]), pz + float(ctr_world[2])]
        body.quat = [wq.GetReal(), float(wqi[0]), float(wqi[1]), float(wqi[2])]
        jnt = body.add_joint()
        jnt.type = _mj.mjtJoint.mjJNT_FREE
        g = body.add_geom()
        if hull_bases[pi] is not None:
            # This instance's OWN prototype hull, recentred on the geometry, scaled per instance.
            g.type = _mj.mjtGeom.mjGEOM_MESH
            g.meshname = _hull_mesh_for(pi, sx, sy, sz)
        elif shape_type == "Box":
            g.type = _mj.mjtGeom.mjGEOM_BOX
            g.size = [
                max(ext_x / 2.0 + collision_margin, 1e-4),
                max(ext_y / 2.0 + collision_margin, 1e-4),
                max(ext_z / 2.0 + collision_margin, 1e-4),
            ]
        else:
            g.type = _mj.mjtGeom.mjGEOM_SPHERE
            g.size = [radius_i, 0.0, 0.0]
        g.mass = mass
        g.friction = [friction, 0.005, 0.0001]
        g.solref = [contact_tc, dyn_dampratio]
        g.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        g.condim = 3

    # Compile
    try:
        model = spec.compile()
    except Exception as e:
        logger.warning("Instancer compile failed: %s", e)
        return

    data = _mj.MjData(model)

    # Resolve body ids by name (collider bodies precede the instances now) and
    # apply damping + initial velocity on each instance's own DOFs.
    bids = []
    for i in range(num_instances):
        bid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, f"b{i}")
        bids.append(bid)
        dof0 = int(model.jnt_dofadr[int(model.body_jntadr[bid])])
        model.dof_damping[dof0 : dof0 + 3] = lin_damping
        model.dof_damping[dof0 + 3 : dof0 + 6] = ang_damping
        if init_vel:
            data.qvel[dof0 : dof0 + 3] = init_vel[:3]
        if ang_vel:
            data.qvel[dof0 + 3 : dof0 + 6] = ang_vel[:3]

    # Resolve mocap collider ids for per-frame kinematic updates
    mocap_ids = []
    for bname, cprim in mocap_col_names:
        mbid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, bname)
        mocap_ids.append((int(model.body_mocapid[mbid]), cprim))

    _mj.mj_forward(model, data)

    # Simulate
    phases = _force_phases(turb_seed)
    sim_t = 0.0
    all_positions = []
    all_orientations = []

    def _snapshot():
        """The instancer's own arrays for the state the bodies are in right now."""
        frame_pos = Vt.Vec3fArray(num_instances)
        frame_ori = Vt.QuathArray(num_instances)
        for i in range(num_instances):
            bid = bids[i]
            pos = data.xpos[bid]
            qmj = data.xquat[bid]

            # world orientation back to instancer-local
            wq = Gf.Quatd(float(qmj[0]), float(qmj[1]), float(qmj[2]), float(qmj[3]))
            body_rot = Gf.Rotation(wq)
            # The body frame is on the instance's geometry centre; the instancer wants the
            # instance ORIGIN, so undo that offset with the body's current rotation.
            ctr_now = body_rot.TransformDir(_instance_centre(i))
            world_pos = Gf.Vec3d(
                float(pos[0]) - float(ctr_now[0]),
                float(pos[1]) - float(ctr_now[1]),
                float(pos[2]) - float(ctr_now[2]),
            )
            local_pos = world_inv.Transform(world_pos)
            frame_pos[i] = Gf.Vec3f(float(local_pos[0]), float(local_pos[1]), float(local_pos[2]))
            lq = (inst_rot_inv * body_rot).GetQuat()
            li = lq.GetImaginary()
            frame_ori[i] = Gf.Quath(float(lq.GetReal()), float(li[0]), float(li[1]), float(li[2]))
        return frame_pos, frame_ori

    tracks = _mocap_tracks(mocap_ids, poses)
    for frame in range(frame_count):
        _set_mocap(data, tracks, frame)

        frame_pos, frame_ori = _snapshot()
        all_positions.append(frame_pos)
        all_orientations.append(frame_ori)

        for _ in range(substeps):
            _apply_wind(model, data, bids, wind, turbulence, turb_scale, turb_speed, phases, sim_t)
            _mj.mj_step(model, data)
            sim_t += sub_dt
        yield frame

    if settle_only:
        # A pile is the case this matters most for: instances shuffle against each other
        # long after a body dropped on its own would have stopped, so the frame count that
        # looked settled for one prop rarely is for fifty.
        _step_until_at_rest(
            model, data, substeps, sub_dt, sim_t,
            lambda t: _apply_wind(model, data, bids, wind, turbulence,
                                  turb_scale, turb_speed, phases, t),
        )
        settled_pos, settled_ori = _snapshot()
        yield sim_bake.MAIN
        # Defaults, not samples, and deliberately not just the strongest opinion: Scatter
        # Points authors its layout as a default too, so leaving this as time samples only
        # (as an animated bake does) means anything reading the instancer at Default() --
        # rather than at a frame -- resolves straight past the simulation to the raw
        # scattered layout underneath.
        with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
            positions_attr.Set(settled_pos)
            orientations_attr.Set(settled_ori)
        logger.debug("Settled PointInstancer %s (%d bodies)", prim.GetPath(), num_instances)
        return

    yield sim_bake.MAIN
    # Time samples, all in one change block (sim_bake.write_samples).
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        sim_bake.write_samples(positions_attr, ((start_frame + f, v) for f, v in enumerate(all_positions)))
        sim_bake.write_samples(orientations_attr, ((start_frame + f, v) for f, v in enumerate(all_orientations)))

    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)

    logger.debug("Baked %d frames on PointInstancer %s (%d bodies)", frame_count, prim.GetPath(), num_instances)


# ---------------------------------------------------------------------------
# Chunk UI
# ---------------------------------------------------------------------------


def _vec3(node, name: str) -> list[float]:
    """Read a 3-component Vector param as plain floats."""
    param = node.get_param(name)
    value = getattr(param, "value", None) if param is not None else None
    if value is None:
        return [0.0, 0.0, 0.0]
    out = [float(v) for v in value][:3]
    return out + [0.0] * (3 - len(out))


#: Above this many triangles a Mesh collider takes long enough to build that the readout
#: warns. MuJoCo builds one geom per triangle, quadratic in their count (see _collider_spec).
_SLOW_MESH_TRIANGLES = 20_000


def _triangle_count(prim: Usd.Prim) -> int:
    """How many triangles the meshes under prim make, from their face counts alone."""
    total = 0
    for p in Usd.PrimRange(prim):
        if p.IsA(UsdGeom.Mesh):
            counts = UsdGeom.Mesh(p).GetFaceVertexCountsAttr().Get()
            if counts:
                total += int(_np.maximum(_np.asarray(counts, dtype=_np.int64) - 2, 0).sum())
    return total


def _mesh_collider_warning(collision_prims, collider_mode: str, tri_budget: int) -> str:
    """A readout line when the colliders will be built as a big Mesh, with how long it takes.

    The settings are used as they are -- a long build may be what the user wants -- but a
    225k-triangle rock cluster set to Mesh is some two hours, and that should be said up
    front, not discovered. The build runs on the worker, so usdview stays usable and Cancel
    stops it.
    """
    if collider_mode not in ("Mesh", "Auto") or not collision_prims:
        return ""
    counts = [_triangle_count(p) for p in collision_prims]
    if collider_mode == "Auto":
        counts = [n for n in counts if n <= tri_budget]
    total = sum(counts)
    if total <= _SLOW_MESH_TRIANGLES:
        return ""
    # Measured: 20k triangles take about a minute, and the cost grows with the square.
    seconds = 61.0 * (total / 20_000) ** 2
    if seconds < 90:
        took = f"about {seconds:.0f} s"
    elif seconds < 90 * 60:
        took = f"about {seconds / 60:.0f} min"
    else:
        took = f"about {seconds / 3600:.1f} h"
    return (f"Mesh collider of {total:,} triangles: building it takes {took}. "
            "Poly Reduce it, or use Heightfield, to be quicker.")


def _parse_collision_prims(value: str, stage: Usd.Stage | None) -> list[Usd.Prim]:
    """Parse a newline-or-comma-separated path list into valid Usd.Prim objects."""
    result = []
    if not stage:
        return result
    for path in (value or "").replace(",", "\n").split("\n"):
        path = path.strip()
        if not path:
            continue
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            result.append(prim)
    return result


def _clear_chunk_bake(owner) -> None:
    if (node := owner.get_chunk()) is not None:
        sim_bake.forget(node)  # or the next cook would put it back
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)


def _promote_bake_layer(layer: Sdf.Layer | None, node=None) -> None:
    """Make the bake layer the strongest chunk layer for its target prim (sim_bake.promote).

    When the solver animates a prim another chunk authored (a Scatter Points instancer), that
    chunk's static values would otherwise hide the baked samples. With node, also keeps a copy
    of the bake on it, for execute() to put back after the next cook clears the layer.
    """
    if node is not None:
        sim_bake.keep(node, layer)
    sim_bake.promote(layer)


def reset(owner, notice, sender) -> None:
    owner.get_chunk()._rbd_bake_key = None
    _clear_chunk_bake(owner)
    owner.get_chunk().set_state(chunk.ChunkFlags.State.STALE)
    logger.info("Reset - bake cleared.")


def add_collision(owner, notice, sender) -> None:
    """Append the viewport-selected prim(s) to the collision list (comma-joined
    so get_prims() picks them all up).
    """
    param = owner.get_chunk().get_param("collision_path")
    scene = chunk_registry.ChunkRegistry().scene
    if not scene:
        return
    selected = scene.get_selected_prims()
    if not selected:
        logger.warning("No prim selected in viewport")
        return
    existing = [p.strip() for p in (param.value or "").replace(",", "\n").split("\n") if p.strip()]
    added = 0
    for prim in selected:
        path = str(prim.GetPath())
        if path and path != "/" and path not in existing:
            existing.append(path)
            added += 1
    param.value = "\n".join(existing)
    logger.info("Added %d collision prim(s); %d total", added, len(existing))


def clear_collision(owner, notice, sender) -> None:
    owner.get_chunk().get_param("collision_path").value = ""
    logger.info("Cleared collision prims")


# UsdPhysics collider approximation -> this chunk's Shape Type. Only the tokens with a
# single-geom MuJoCo equivalent are here: convexDecomposition and sdf need several geoms or
# a signed-distance field, so they keep whatever the param says rather than being silently
# downgraded to something that simulates differently. (An articulated asset's link colliders
# do honour convexDecomposition: _collider_shape splits them, _convex_pieces.)
_APPROXIMATION_TO_SHAPE = {
    "convexHull": "Convex Hull",
    "convexDecomposition": None,
    "meshSimplification": "Convex Hull",
    "boundingCube": "Box",
    "boundingSphere": "Sphere",
    "sdf": None,
    "none": "Convex Hull",
}


def _physics_material(prim: Usd.Prim):
    """The PhysicsMaterialAPI bound to prim through the 'physics' material purpose, or None.

    Physics materials bind the way shading materials do, on their own purpose, so one prim
    can carry both a look and a set of contact properties.
    """
    if not prim or not prim.IsValid():
        return None
    material, _rel = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial("physics")
    if not material:
        return None
    mat_prim = material.GetPrim()
    if not mat_prim or not mat_prim.HasAPI(UsdPhysics.MaterialAPI):
        return None
    return UsdPhysics.MaterialAPI(mat_prim)


def _authored(attr):
    """An attribute's value if it was actually authored, else None.

    HasAuthoredValue is the point: every UsdPhysics attribute has a schema fallback, so
    Get() always returns something. Trusting that would let a schema default silently
    outrank the chunk's param, which inverts the precedence this is here to establish.
    """
    if attr and attr.HasAuthoredValue():
        return attr.Get()
    return None


def _material_friction(prim: Usd.Prim) -> float | None:
    """The friction of the physics material bound to prim (dynamic, else static), or None.

    USD is the one place a scene's physics is stated: PhysX reads the same material off
    the same collider, so the bake and a PhysX run use the same friction. None leaves
    it to the chunk's Friction.
    """
    material = _physics_material(prim)
    if material is None:
        return None
    friction = _authored(material.GetDynamicFrictionAttr())
    if friction is None:
        friction = _authored(material.GetStaticFrictionAttr())
    return None if friction is None else float(friction)


def _scene_prim(stage: Usd.Stage):
    """The stage's first UsdPhysics.Scene, or None."""
    if not stage:
        return None
    for prim in stage.Traverse():
        if prim.IsA(UsdPhysics.Scene):
            return prim
    return None


def _scene_gravity(stage: Usd.Stage, fallback: float):
    """Gravity from a UsdPhysics.Scene on the stage, as the signed scalar this chunk uses.

    The schema states a magnitude and a direction; the solver applies a signed scalar along
    the stage up axis, so only the component along up survives the translation.
    """
    prim = _scene_prim(stage)
    if prim is None:
        return fallback, None
    scene = UsdPhysics.Scene(prim)
    magnitude = _authored(scene.GetGravityMagnitudeAttr())
    if magnitude is None:
        return fallback, None
    direction = _authored(scene.GetGravityDirectionAttr())
    up_index = {"X": 0, "Y": 1, "Z": 2}.get(UsdGeom.GetStageUpAxis(stage), 2)
    sign = -1.0
    if direction is not None and abs(direction[up_index]) > 1e-9:
        sign = 1.0 if direction[up_index] > 0.0 else -1.0
    return sign * abs(magnitude), str(prim.GetPath())


def _scene_substeps(stage: Usd.Stage, fps: float, fallback: int) -> int:
    """Substeps from the physics scene's step rate (physxScene:timeStepsPerSecond), so the
    bake steps as PhysX does on the same file; the chunk's Substeps when none is authored."""
    prim = _scene_prim(stage)
    rate = _authored(prim.GetAttribute("physxScene:timeStepsPerSecond")) if prim is not None else None
    if not rate or fps <= 0.0:
        return fallback
    return max(1, int(round(float(rate) / fps)))


def _authored_physics(stage: Usd.Stage, prim: Usd.Prim, defaults: dict) -> dict:
    """Physics for this run, preferring what the asset authors over the chunk's params.

    A sim-ready asset already states its mass, contact properties and collider
    approximation. Re-deriving those from a global slider throws away the part its author
    was careful about, and is why the solver could not usefully consume a library asset.

    Anything not authored falls through to *defaults*, so a scene carrying no UsdPhysics
    simulates exactly as it did before.

    Values are taken in stage units and used as-is, deliberately. The solver already feeds
    USD geometry to MuJoCo unscaled, and the Gravity param exists to absorb the difference
    (its tooltip suggests ~100 on a centimetre scene). Converting only mass to SI while
    lengths stayed in stage units would make that worse, not better -- real unit handling
    means scaling the geometry too, which is a change to the model build, not to reading.

    Args:
        stage (Usd.Stage): The stage being simulated
        prim (Usd.Prim): The prim being simulated
        defaults (dict): The chunk's param values, used wherever nothing is authored

    Returns:
        dict: gravity, mass, friction, restitution, shape_type, kinematic
    """
    out = dict(defaults)
    out["kinematic"] = False
    found = []

    if prim and prim.IsValid():
        if prim.HasAPI(UsdPhysics.MassAPI):
            mass = _authored(UsdPhysics.MassAPI(prim).GetMassAttr())
            if mass is not None and mass > 0.0:
                out["mass"] = mass
                found.append(f"mass={mass:g}")

        # A kinematic body is driven rather than solved. The solver already has that notion
        # for animated colliders (mocap bodies), so honouring the flag keeps the asset's
        # own intent instead of dropping a conveyor belt into free fall.
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            if _authored(UsdPhysics.RigidBodyAPI(prim).GetKinematicEnabledAttr()):
                out["kinematic"] = True
                found.append("kinematic")

        material = _physics_material(prim)
        if material is not None:
            friction = _material_friction(prim)
            if friction is not None:
                out["friction"] = friction
                found.append(f"friction={friction:g}")
            restitution = _authored(material.GetRestitutionAttr())
            if restitution is not None:
                out["restitution"] = restitution
                found.append(f"restitution={restitution:g}")

        # The approximation is the asset author's decision about how this shape should be
        # collided -- exactly what Shape Type otherwise has to guess.
        if prim.HasAPI(UsdPhysics.MeshCollisionAPI):
            approximation = _authored(UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr())
            shape = _APPROXIMATION_TO_SHAPE.get(approximation)
            if shape:
                out["shape_type"] = shape
                found.append(f"shape={shape}")
            elif approximation:
                logger.debug(
                    "approximation '%s' has no single-geom MuJoCo equivalent -- "
                    "keeping Shape Type '%s'", approximation, out["shape_type"],
                )

    gravity, scene_path = _scene_gravity(stage, out["gravity"])
    if scene_path:
        out["gravity"] = gravity
        found.append(f"gravity={gravity:g} ({scene_path})")

    if found:
        logger.debug("using authored UsdPhysics: %s", ", ".join(found))
    return out


# --- articulation -----------------------------------------------------------
#
# A sim-ready asset describes a mechanism as flat sibling links joined by UsdPhysics
# joints -- the tree is in the joint relationships, not in the prim hierarchy. The
# joint prim itself lives inside the child link, and physics:body0 is the parent while
# physics:body1 is the child. A joint with no body0 is anchored to the world.
#
# Only the three joint types that carry a real mechanism are handled: revolute,
# prismatic and fixed. Together they were 98% of the joints in the corpus this was
# built against (682 revolute, 640 prismatic, 316 fixed, 30 generic). A generic
# PhysicsJoint has no single MuJoCo equivalent and is treated as fixed rather than
# guessed at.

_AXIS_VECTORS = {"X": Gf.Vec3d(1, 0, 0), "Y": Gf.Vec3d(0, 1, 0), "Z": Gf.Vec3d(0, 0, 1)}

_JOINT_REVOLUTE = "PhysicsRevoluteJoint"
_JOINT_PRISMATIC = "PhysicsPrismaticJoint"
_JOINT_FIXED = "PhysicsFixedJoint"


class _Link:
    """One rigid body in an articulated asset, and the joint attaching it to its parent."""

    __slots__ = ("prim", "parent", "joint", "children", "name", "anchored")

    def __init__(self, prim):
        self.prim = prim
        self.parent = None
        self.joint = None
        self.children = []
        self.name = prim.GetName()
        # Fixed to the world by a joint with no body0: a robot bolted to its mount (URDF
        # Import's Fixed Base), or an appliance's sim-ready anchor.
        self.anchored = False


class _Joint:
    """A parsed UsdPhysics joint, in the terms MuJoCo needs."""

    __slots__ = ("kind", "axis", "pos", "lower", "upper", "damping", "stiffness", "friction",
                 "target", "max_force", "offset", "armature")

    def __init__(self, kind, axis, pos, lower, upper, damping, stiffness, friction,
                 target=None, max_force=None, offset=0.0, armature=None):
        self.kind = kind
        self.axis = axis
        self.pos = pos
        self.lower = lower
        self.upper = upper
        self.damping = damping
        self.stiffness = stiffness
        self.friction = friction
        # The drive's targetPosition attribute, when it has one and a stiffness to chase it
        # with: the joint is a motor. Kept as the attribute, not a value, because an
        # animated target is read frame by frame (_motor_tracks).
        self.target = target
        self.max_force = max_force
        # How far the joint is posed from rest, in MuJoCo units: its drive's default target,
        # which Pose Articulation writes with the pose. The model is built from
        # the posed links, so its zero is there, and the limits move with it.
        self.offset = offset
        # The motor's rotor inertia seen at the joint (physxJoint:armature). A stiff drive on a
        # light link rings faster than the solver steps without it: a joint at ~1000 N m/rad
        # on ~1e-4 kg m^2 of link blew the solve apart.
        self.armature = armature

    @property
    def motor(self) -> bool:
        return self.target is not None and self.kind in ("revolute", "prismatic")


def _joint_target(rel):
    """First target path of a joint's body relationship, or None if unauthored."""
    if not rel:
        return None
    targets = rel.GetTargets()
    return targets[0] if targets else None


def _parse_joint(joint_prim):
    """Read one joint into MuJoCo's terms, or None if it is a type we do not handle.

    The axis is a token naming an axis of the joint frame, and that frame is expressed
    in the *child* body's space by localRot1 -- so the axis MuJoCo wants is the token
    rotated by that quaternion. Getting this wrong hinges the door about the right
    point in the wrong plane, which looks plausible and is not.

    Rotational limits are authored in DEGREES; MuJoCo hinge ranges are radians.
    """
    type_name = str(joint_prim.GetTypeName())
    if type_name == _JOINT_REVOLUTE:
        kind = "revolute"
    elif type_name == _JOINT_PRISMATIC:
        kind = "prismatic"
    else:
        # Fixed, or a generic PhysicsJoint we decline to guess at.
        kind = "fixed"

    axis_attr = joint_prim.GetAttribute("physics:axis")
    axis_token = axis_attr.Get() if axis_attr and axis_attr.HasAuthoredValue() else "X"
    axis_local = _AXIS_VECTORS.get(str(axis_token), Gf.Vec3d(1, 0, 0))

    rot_attr = joint_prim.GetAttribute("physics:localRot1")
    if rot_attr and rot_attr.HasAuthoredValue():
        q = rot_attr.Get()
        axis_world = Gf.Rotation(Gf.Quatd(float(q.real), Gf.Vec3d(*[float(c) for c in q.imaginary]))).TransformDir(axis_local)
    else:
        axis_world = axis_local

    pos_attr = joint_prim.GetAttribute("physics:localPos1")
    pos = pos_attr.Get() if pos_attr and pos_attr.HasAuthoredValue() else Gf.Vec3f(0, 0, 0)

    lower = _authored(joint_prim.GetAttribute("physics:lowerLimit"))
    upper = _authored(joint_prim.GetAttribute("physics:upperLimit"))
    if kind == "revolute":
        # Degrees in USD, radians in MuJoCo.
        lower = math.radians(lower) if lower is not None else None
        upper = math.radians(upper) if upper is not None else None

    # Drives. Without a target the drive is passive -- what a settle or a destruction sim
    # wants, a door that swings and comes to rest. With a targetPosition and a stiffness it
    # is a motor: the joint is pulled toward the target (Pose Articulation, inverse kinematics), so
    # an animated robot follows its animation with mass, gravity and contacts.
    damping = stiffness = target = max_force = None
    for prefix in ("drive:X:physics:", "drive:angular:physics:", "drive:linear:physics:",
                   "drive:rotX:physics:", "drive:rotY:physics:", "drive:rotZ:physics:",
                   "drive:transX:physics:"):
        d = _authored(joint_prim.GetAttribute(prefix + "damping"))
        k = _authored(joint_prim.GetAttribute(prefix + "stiffness"))
        attr = joint_prim.GetAttribute(prefix + "targetPosition")
        has_target = bool(attr) and (attr.HasAuthoredValue() or attr.GetNumTimeSamples() > 0)
        if d is not None or k is not None or has_target:
            damping, stiffness = d, k
            if has_target and k is not None and k > 0.0:
                target = attr
                max_force = _authored(joint_prim.GetAttribute(prefix + "maxForce"))
            break

    friction = _authored(joint_prim.GetAttribute("physxJoint:jointFriction"))

    # The pose the joint is in: its drive's default target, a motor's or not (Pose
    # Articulation writes it with the pose either way). Degrees in USD, radians in MuJoCo.
    offset = 0.0
    for prefix in ("drive:angular:physics:", "drive:linear:physics:", "drive:X:physics:",
                   "drive:rotX:physics:", "drive:rotY:physics:", "drive:rotZ:physics:",
                   "drive:transX:physics:"):
        value = _authored(joint_prim.GetAttribute(prefix + "targetPosition"))
        if value is not None:
            offset = math.radians(float(value)) if kind == "revolute" else float(value)
            break

    return _Joint(
        kind,
        [float(axis_world[0]), float(axis_world[1]), float(axis_world[2])],
        [float(pos[0]), float(pos[1]), float(pos[2])],
        lower, upper, damping, stiffness, friction, target, max_force, offset,
        _authored(joint_prim.GetAttribute("physxJoint:armature")),
    )


def _unique_link_names(roots) -> None:
    """Give each link a name no other link has: the solver names MuJoCo's bodies, joints and
    motors after them, and two copies of one asset share every link name.
    The first keeps its name; a repeat gets _2, _3... Only the solver reads `name`; the prim
    each link writes to is `prim`."""
    seen = set()
    for link in (link for root in roots for link in _iter_links(root)):
        name, n = link.name, 1
        while name in seen:
            n += 1
            name = f"{link.name}_{n}"
        link.name = name
        seen.add(name)


def read_articulations(root_prim, keep_colliderless=False):
    """Parse an articulated asset under root_prim into link trees.

    Returns a list of root _Links, one per independent mechanism. Empty when the subtree
    holds no joints at all, which is the signal to use the plain rigid-body path.

    More than one root is normal rather than exceptional: 28 of the 243 articulated assets
    in the corpus this was built against have several -- an appliance plus its unjointed
    removable parts, and in one case fourteen of them. Simulating only the first would
    silently drop the rest.

    `keep_colliderless` keeps jointed links that have no collider. A solver cannot build
    those (a massless body), but posing only needs the joint -- a drawer authored with a
    body and no collision mesh is still a drawer.
    """
    if not root_prim or not root_prim.IsValid():
        return []

    links = {}
    joints = []
    for prim in Usd.PrimRange(root_prim):
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            links[prim.GetPath()] = _Link(prim)
        if "Joint" in str(prim.GetTypeName()):
            joints.append(prim)

    if not joints or not links:
        return []

    def owning_body(path):
        # A joint may name a collision mesh below the body rather than the body itself
        # (SimReady authors body1 = .../drawer_obj/drawer_mesh with the RigidBodyAPI on
        # drawer_obj), so walk up to the nearest prim that is a link.
        while path is not None and not path.IsAbsoluteRootPath():
            if path in links:
                return path
            path = path.GetParentPath()
        return None

    for joint_prim in joints:
        body0 = _joint_target(joint_prim.GetRelationship("physics:body0"))
        body1 = _joint_target(joint_prim.GetRelationship("physics:body1"))
        body0, body1 = owning_body(body0), owning_body(body1)
        child = links.get(body1)
        if child is None:
            continue
        if body0 is None:
            # No body0 means anchored to the world; the link stays a root.
            if str(joint_prim.GetTypeName()) == "PhysicsFixedJoint":
                child.anchored = True
            continue
        parent = links.get(body0)
        if parent is None or parent is child:
            continue
        child.parent = parent
        child.joint = _parse_joint(joint_prim)
        parent.children.append(child)

    # A link with no collider anywhere beneath it and nothing hanging off it is not a
    # physical body. These assets apply PhysicsRigidBodyAPI to annotation scopes -- the
    # `Sites` prims holding manipulation target markers -- which compile as massless
    # bodies and take the whole model down with an error that names only the scope.
    for path, link in list(links.items()):
        if link.children or (keep_colliderless and link.joint is not None):
            continue
        has_collider = any(
            pr.HasAPI(UsdPhysics.CollisionAPI) for pr in Usd.PrimRange(link.prim)
        )
        if not has_collider:
            if link.parent is not None and link in link.parent.children:
                link.parent.children.remove(link)
            del links[path]

    roots = [lk for lk in links.values() if lk.parent is None]
    if not roots:
        # Every link claims a parent: a closed loop, which MuJoCo's tree cannot express.
        logger.warning("articulation has no root link (closed kinematic loop) -- skipping")
    return roots


def _iter_links(root):
    """Depth-first walk of a link tree, parents before children."""
    stack = [root]
    while stack:
        link = stack.pop()
        yield link
        stack.extend(reversed(link.children))


def _prim_scale(matrix):
    """Per-axis scale of a transform, as the lengths of its basis rows."""
    return [matrix.GetRow3(i).GetLength() for i in range(3)]


#: Pieces a convexDecomposition collider is split into: enough that a hull no longer fills
#: a concave part's inside, few enough to keep contacts cheap.
_DECOMPOSITION_PIECES = 8


def _convex_pieces(mesh, matrix, pieces=_DECOMPOSITION_PIECES):
    """A mesh split into spatially compact groups of faces, each group's vertices (in the
    frame `matrix` takes them to) to be hulled on its own: an approximate convex
    decomposition, for UsdPhysics' convexDecomposition.

    One convex hull of a concave part fills its hollows -- a curved part becomes a solid
    wedge, and whatever sits in the hollow is pushed against invisible faces.
    The faces are grouped by k-means on their centres (seeded from the farthest-apart
    centres, so the same mesh always splits the same way); a group's hull hugs its stretch of
    the surface. Empty when the mesh has no faces to group.
    """
    points = _np.asarray(mesh.GetPointsAttr().Get(), dtype=_np.float64)
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if points.size == 0 or not counts or not indices:
        return []
    indices = _np.asarray(indices, dtype=_np.int64)
    # The grouping depends on the mesh alone, and it is most of a solve's cost (a robot's link
    # meshes run to tens of thousands of points; a bake rebuilt them every time). Kept by the
    # mesh's content -- an edited mesh is a new key -- with `matrix` applied after.
    key = hashlib.blake2b(points.tobytes() + _np.asarray(counts, dtype=_np.int64).tobytes()
                          + indices.tobytes() + str(pieces).encode(), digest_size=16).digest()
    local = _PIECES_CACHE.get(key)
    if local is None:
        local = _group_pieces(points, counts, indices, pieces)
        if len(_PIECES_CACHE) >= _PIECES_CACHE_SIZE:
            _PIECES_CACHE.pop(next(iter(_PIECES_CACHE)))
        _PIECES_CACHE[key] = local
    return [(pts @ matrix[:3, :3] + matrix[3, :3]).astype(_np.float32) for pts in local]


#: _convex_pieces' groupings by mesh content (oldest dropped first past _PIECES_CACHE_SIZE).
_PIECES_CACHE: dict = {}
_PIECES_CACHE_SIZE = 256


def _group_pieces(points, counts, indices, pieces):
    """_convex_pieces' grouping, in the mesh's own frame: each group's vertices (at most 256)."""
    starts = _np.concatenate(([0], _np.cumsum(counts)[:-1]))
    faces = [indices[s:s + n] for s, n in zip(starts, counts)]
    centres = _np.array([points[f].mean(axis=0) for f in faces])
    k = max(1, min(pieces, len(faces)))
    # Farthest-point seeds: deterministic, and spread over the whole part.
    seeds = [0]
    dist = _np.linalg.norm(centres - centres[0], axis=1)
    for _ in range(1, k):
        seeds.append(int(dist.argmax()))
        dist = _np.minimum(dist, _np.linalg.norm(centres - centres[seeds[-1]], axis=1))
    means = centres[seeds]
    for _ in range(12):
        label = _np.linalg.norm(centres[:, None, :] - means[None, :, :], axis=2).argmin(axis=1)
        for j in range(k):
            members = centres[label == j]
            if len(members):
                means[j] = members.mean(axis=0)
    out = []
    for j in range(k):
        used = _np.unique(_np.concatenate([faces[i] for i in _np.flatnonzero(label == j)] or [[]]).astype(_np.int64))
        if len(used) < 4:
            continue
        pts = points[used]
        if len(pts) > 256:
            pts = pts[:: max(1, len(pts) // 256)][:256]
        out.append(pts)
    return out


def _collider_shape(prim, to_link):
    """(mjGeom type, size, pos, quat) for one collision prim, in its link's frame.

    Sizes come from the gprim's own attributes rather than its `extent`, deliberately.
    The corpus this was built against authors `extent` as the *final* size while also
    carrying a scale, and leaves `size` unauthored -- so it defaults to 2. A bound-based
    reader multiplies the two and gets a collider five times too small, with no error
    anywhere. Reading size/radius/height and applying the transform's scale once is both
    correct per the schema and immune to that.
    """
    local = to_link  # prim-local -> link space
    scale = _prim_scale(local)
    pos = local.ExtractTranslation()
    quat = local.RemoveScaleShear().ExtractRotationQuat()
    qi = quat.GetImaginary()
    quat_mj = [quat.GetReal(), float(qi[0]), float(qi[1]), float(qi[2])]
    pos_mj = [float(pos[0]), float(pos[1]), float(pos[2])]

    type_name = str(prim.GetTypeName())
    if type_name == "Cube":
        half = UsdGeom.Cube(prim).GetSizeAttr().Get() * 0.5
        size = [max(half * scale[i], 1e-6) for i in range(3)]
        return _mj.mjtGeom.mjGEOM_BOX, size, pos_mj, quat_mj

    if type_name == "Sphere":
        radius = float(UsdGeom.Sphere(prim).GetRadiusAttr().Get() or 1.0)
        return _mj.mjtGeom.mjGEOM_SPHERE, [max(radius * max(scale), 1e-6), 0.0, 0.0], pos_mj, quat_mj

    if type_name == "Cylinder":
        cyl = UsdGeom.Cylinder(prim)
        radius = float(cyl.GetRadiusAttr().Get() or 1.0)
        height = float(cyl.GetHeightAttr().Get() or 2.0)
        axis = str(cyl.GetAxisAttr().Get() or "Z")
        idx = {"X": 0, "Y": 1, "Z": 2}.get(axis, 2)
        lateral = [scale[i] for i in range(3) if i != idx]
        size = [max(radius * max(lateral), 1e-6), max(height * 0.5 * scale[idx], 1e-6), 0.0]
        # MuJoCo cylinders run along their local Z; rotate when the prim says otherwise.
        if axis == "X":
            quat_mj = [0.7071068, 0.0, 0.7071068, 0.0]
        elif axis == "Y":
            quat_mj = [0.7071068, -0.7071068, 0.0, 0.0]
        return _mj.mjtGeom.mjGEOM_CYLINDER, size, pos_mj, quat_mj

    if type_name == "Mesh":
        points = UsdGeom.Mesh(prim).GetPointsAttr().Get()
        if not points:
            return None
        approximation = prim.GetAttribute("physics:approximation").Get()
        if approximation == "convexDecomposition":
            pieces = _convex_pieces(UsdGeom.Mesh(prim), _np.array(local, dtype=_np.float64))
            if pieces:
                return "meshes", pieces, [0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]
        # Thinned BEFORE transforming, then transformed as one array: the same 256 points,
        # but an STL mesh's tens of thousands of vertices each went through Gf in a Python
        # loop first -- 0.16 s a link, every build, which made a live settle of a meshy
        # robot take seconds per move.
        pts = _np.asarray(points, dtype=_np.float64)
        if len(pts) > 256:
            pts = pts[:: max(1, len(pts) // 256)][:256]
        m = _np.array(local, dtype=_np.float64)
        verts = pts @ m[:3, :3] + m[3, :3]
        # Position and rotation are already folded into the transformed vertices.
        return "mesh", verts.astype(_np.float32), [0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]

    return None


def _add_link_geoms(spec, body, link_prim, link_world_inv, friction, contact_tc,
                    restitution, collision_margin=0.0):
    """Attach a link's authored collision prims to its MuJoCo body.

    Sim-ready assets ship purpose-built collision proxies under a Collisions scope --
    primitive cubes and cylinders, not the render mesh. Using them is both more accurate
    and far cheaper than hulling the visual geometry, and it is the part of the asset its
    author was most deliberate about.

    Those proxies carry `purpose = guide` so they never render. A BBoxCache built for the
    default purpose alone returns an EMPTY bound for every one of them without complaining,
    which reads as "this link has no colliders", leaves the body massless, and fails the
    compile with a message about mjMINVAL that says nothing about purposes.
    """
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    count = 0
    for prim in Usd.PrimRange(link_prim):
        if not prim.HasAPI(UsdPhysics.CollisionAPI):
            continue
        to_link = xform_cache.GetLocalToWorldTransform(prim) * link_world_inv
        shape = _collider_shape(prim, to_link)
        if shape is None:
            continue
        geom_type, size, pos, quat = shape
        # The collider's bound physics material, else the chunk's Friction.
        own = _material_friction(prim)
        mu = friction if own is None else own

        if geom_type in ("mesh", "meshes"):
            # One hull, or one per piece of a convex decomposition.
            for verts in (size if geom_type == "meshes" else [size]):
                geom = body.add_geom()
                mesh_spec = spec.add_mesh()
                # The body's name where it has one: unique in the model, where two copies of a
                # robot share every prim name.
                mesh_spec.name = f"hull_{body.name or link_prim.GetName()}_{count}"
                mesh_spec.uservert = verts.flatten()
                mesh_spec.maxhullvert = 64
                geom.type = _mj.mjtGeom.mjGEOM_MESH
                geom.meshname = mesh_spec.name
                geom.friction = [mu, 0.005, 0.0001]
                geom.solref = [contact_tc, 1.0 - max(0.0, min(1.0, restitution)) * 0.9]
                geom.solimp = [0.95, 0.99, 0.001, 0.5, 2]
                geom.condim = 3
                count += 1
            continue

        geom = body.add_geom()
        geom.friction = [mu, 0.005, 0.0001]
        geom.solref = [contact_tc, 1.0 - max(0.0, min(1.0, restitution)) * 0.9]
        geom.solimp = [0.95, 0.99, 0.001, 0.5, 2]
        geom.condim = 3
        geom.type = geom_type
        geom.size = [v + collision_margin if v > 0.0 else v for v in size]
        geom.pos = pos
        geom.quat = quat
        count += 1
    return count


_SETTLE_VEL_THRESH = 0.02
_SETTLE_MAX_EXTRA_FRAMES = 240


def _sim_articulated_gen_mj(
    roots,
    frame_count: int,
    gravity: float,
    friction: float,
    restitution: float,
    mass: float,
    substeps: int,
    start_frame: int,
    dt: float,
    up_idx: int,
    solver_iters: int,
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    collision_prims: list | None = None,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    floor_drop: float = 0.0,
    free_root: bool = True,
    init_vel: list | None = None,
    ang_vel: list | None = None,
    collision_margin: float = 0.0,
    wind: tuple | list = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
    settle_only: bool = False,
    progress=None,
    cache: dict | None = None,
    extra: list | None = None,
) -> Generator[int, None, dict | None]:
    """Simulate an articulated asset, yielding frame index and returning per-link matrices.

    `extra`: free rigid bodies in the same world, as dicts the rigid path takes (prim,
    translate, rot, mass, friction, restitution, shape_type). Their world matrices come back
    under "__free_<i>".

    Bodies are nested to mirror the joint tree, in each link's own prim frame rather than
    on its geometry centre: joint anchors are authored in prim space, and an articulated
    asset is authored as a coherent mechanism, so the far-from-origin problem the rigid
    path guards against does not arise the same way.
    """
    if not roots:
        return None
    # Read now, on the main thread: everything after the first yield may run on a worker.
    extra_pairs = [(b, _body_frame(b["prim"], b["translate"], b["rot"], b["shape_type"], 0))
                   for b in (extra or ())]
    if extra_pairs:
        cache = None  # a kept model has no free bodies in it
    # A live settle keeps the compiled model between cooks (`cache`, a dict the caller
    # holds): rebuilding it -- colliders, hulls, compile -- was most of every move.
    key = entry = None
    if cache is not None:
        key = _live_model_key(roots, collision_prims, (
            gravity, friction, restitution, mass, substeps, dt, up_idx, solver_iters,
            hfield_res, collider_mode, collider_thickness, floor_drop, free_root,
            collision_margin))
        entry = cache.get("entry")
        if entry is not None and (key is None or entry["key"] != key):
            entry = None
    colliders = _read_colliders(collision_prims)
    # An animated collider (a door swinging shut, a paddle sweeping a table) is a mocap body:
    # its pose every frame, read here on the main thread, re-poses it as the rigid path does.
    poses = _collider_poses(colliders, start_frame, frame_count)
    if entry is not None:
        model, zeros, mocap_names = entry["model"], entry["zeros"], entry["mocap"]
        ordered = [link for root in roots for link in _iter_links(root)]
        data = _mj.MjData(model)
        _mj.mj_forward(model, data)
    else:
        # The colliders build on the worker (the slow part on a big Mesh collider); the links
        # read the stage as they go, so the rest of the build comes back to the main thread.
        yield sim_bake.WORKER
        collider_spec, mocap_names = _articulated_colliders(
            colliders, up_idx, friction, substeps, dt, hfield_res, floor_drop, collider_mode,
            collider_thickness, progress=progress,
        )
        yield sim_bake.MAIN
        built = build_articulated_spec(
            roots, gravity, friction, restitution, mass, substeps, dt, up_idx,
            solver_iters, collision_prims, hfield_res, collider_mode, collider_thickness,
            floor_drop, free_root, collision_margin, collider_spec=collider_spec,
            extra=extra_pairs,
        )
        if built is None:
            return None
        model, data, ordered = built
        zeros = None
        if cache is not None:
            # The model's zero is the pose it was built in, so the targets it was built at
            # are what a later pose's targets are measured from (_motor_tracks).
            zeros = _motor_zeros(ordered)
            cache["entry"] = None if key is None else {"key": key, "model": model, "zeros": zeros,
                                                       "mocap": mocap_names}
    collider_tracks = _mocap_tracks(
        [(int(model.body_mocapid[_mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, name)]), prim)
         for name, prim in mocap_names], poses)
    if free_root and (init_vel or ang_vel):
        for root in roots:
            jid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, f"{root.name}_free")
            if jid < 0:
                continue
            dof0 = model.jnt_dofadr[jid]
            if init_vel:
                data.qvel[dof0:dof0 + 3] = init_vel[:3]
            if ang_vel:
                data.qvel[dof0 + 3:dof0 + 6] = ang_vel[:3]
        _mj.mj_forward(model, data)

    ids = {}
    for link in ordered:
        bid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, link.name)
        if bid >= 0:
            ids[link.name] = bid

    # Linear/angular damping on each free root's DOFs, as the rigid path does.
    root_bids = []
    for root in roots:
        jid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, f"{root.name}_free")
        if jid < 0:
            continue
        dof0 = model.jnt_dofadr[jid]
        model.dof_damping[dof0:dof0 + 3] = lin_damping
        model.dof_damping[dof0 + 3:dof0 + 6] = ang_damping
        bid = ids.get(root.name)
        if bid is not None:
            root_bids.append(bid)

    # The free bodies are recorded beside the links, and damped as the rigid path damps them.
    for i in range(len(extra_pairs)):
        ids[f"__free_{i}"] = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, f"free_body_{i}")
        jid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, f"free_body_{i}_joint")
        if jid >= 0:
            dof0 = model.jnt_dofadr[jid]
            model.dof_damping[dof0:dof0 + 3] = lin_damping
            model.dof_damping[dof0 + 3:dof0 + 6] = ang_damping

    # Motors: each one's target per frame. The joints start where frame 0 asks, not at the
    # rest pose -- a robot animated from a reach would otherwise snap there on frame 1.
    act_ids, qpos_adr, tracks = _motor_tracks(model, ordered, start_frame, frame_count, zeros)
    if tracks is not None:
        data.qpos[qpos_adr] = tracks[0]
        data.ctrl[act_ids] = tracks[0]
        _mj.mj_forward(model, data)
    # A set-down goal closes the loop on a carried body: each frame it is held, the joints
    # are shifted so it will land on the goal (_set_down_step).
    goals = [] if tracks is None else _set_down_goals(
        roots, ordered, model, act_ids, [str(b["prim"].GetPath()) for b, _f in extra_pairs],
        start_frame, frame_count, up_idx)
    offsets = [_np.zeros(len(g["columns"])) for g in goals]
    scratch = _mj.MjData(model) if goals else None

    phases = _force_phases(turb_seed)
    sim_t = 0.0
    sub_dt = dt / substeps

    results = {name: [] for name in ids}
    # Everything below is MuJoCo and Gf only: in a UI it runs on a worker (sim_bake).
    yield sim_bake.WORKER
    for frame in range(frame_count):
        _set_mocap(data, collider_tracks, frame)
        if tracks is not None:
            here, there = tracks[frame], tracks[frame + 1]
            for goal, dq in zip(goals, offsets):
                if goal["first"] <= frame < goal["until"]:
                    dq[:] = _set_down_step(model, data, scratch, goal, tracks[goal["until"]], qpos_adr, dq)
            shift = _np.zeros(len(act_ids))
            for goal, dq in zip(goals, offsets):
                if frame >= goal["first"]:          # kept after release, so the arm doesn't swing back
                    shift[goal["columns"]] += dq
        for name, bid in ids.items():
            pos = data.xpos[bid]
            quat = data.xquat[bid]
            mat = Gf.Matrix4d(1.0)
            mat.SetRotate(Gf.Quatd(float(quat[0]), Gf.Vec3d(float(quat[1]), float(quat[2]), float(quat[3]))))
            mat.SetTranslateOnly(Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2])))
            results[name].append(mat)
        for sub in range(substeps):
            if tracks is not None:
                # Eased from this frame's target to the next one's, so a motor is not
                # yanked once a frame -- a stair-stepped target rings at the frame rate.
                data.ctrl[act_ids] = here + (there - here) * ((sub + 1) / substeps) + shift
            # Wind pushes the free roots. A link inside a mechanism inherits the motion
            # through its joint rather than being blown independently, which is both
            # cheaper and what a hinged door on a tumbling appliance actually does.
            if root_bids:
                _apply_wind(model, data, tuple(root_bids), wind, turbulence,
                            turb_scale, turb_speed, phases, sim_t)
            _mj.mj_step(model, data)
            sim_t += sub_dt
        yield frame

    if settle_only:
        # Keep going until the mechanism is actually at rest, then append that pose as the
        # final frame -- _write_articulated_results always takes mats[-1] for settle_only.
        sim_t = _step_until_at_rest(
            model, data, substeps, sub_dt, sim_t,
            (lambda t: _apply_wind(model, data, tuple(root_bids), wind, turbulence,
                                   turb_scale, turb_speed, phases, t)) if root_bids else None,
        )
        for name, bid in ids.items():
            pos = data.xpos[bid]
            quat = data.xquat[bid]
            mat = Gf.Matrix4d(1.0)
            mat.SetRotate(Gf.Quatd(float(quat[0]), Gf.Vec3d(float(quat[1]), float(quat[2]), float(quat[3]))))
            mat.SetTranslateOnly(Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2])))
            results[name].append(mat)

    # A free body's frame sits on its geometry: its prim's world placement is its rest pose
    # carried by the body's motion since the start, as the rigid path writes it.
    for i, (_b, f) in enumerate(extra_pairs):
        key = f"__free_{i}"
        results[key] = [f["world_rest"] * f["rest_inv"] * m for m in results[key]]
    return results


_SET_DOWN = "setDown:"
_SET_DOWN_CLAMP = math.radians(10.0)    # the most a set-down correction moves a joint


def _set_down_goals(roots, ordered, model, act_ids, free_paths, start_frame: int, frame_count: int,
                    up_idx: int) -> list[dict]:
    """The set-down goals on the robots (``setDown:*`` on a root prim or an ancestor, as an
    upstream chunk writes them), in MuJoCo's terms. Main thread: they are read off the stage.

    A goal names a goal point, the box, the hand link holding it, the joints to correct, the
    frames to correct over (from the grip shutting until it opens) and a gain. A goal naming
    a joint that is not a motor here, or a body not in this world, is skipped with a warning.
    """
    links = {str(link.prim.GetPath()): link for link in ordered}
    seen, goals = set(), []
    for root in roots:
        prim = root.prim
        while prim and not prim.IsPseudoRoot():
            path = str(prim.GetPath())
            goal = prim.GetAttribute(_SET_DOWN + "goal")
            if path not in seen and goal and goal.HasValue():
                seen.add(path)
                if (found := _set_down_goal(prim, goal, links, model, act_ids, free_paths)) is None:
                    logger.warning("set-down goal on %s names a joint or body not simulated here: skipped", path)
                else:
                    found.update(up=up_idx,
                                 first=max(0, int(prim.GetAttribute(_SET_DOWN + "from").Get()) - start_frame),
                                 until=min(frame_count, int(prim.GetAttribute(_SET_DOWN + "until").Get()) - start_frame))
                    goals.append(found)
            prim = prim.GetParent()
    return goals


def _set_down_goal(prim, goal, links, model, act_ids, free_paths) -> dict | None:
    """One set-down goal: motor columns, hand and box body ids, the goal point and gain; None
    when something it names is not here."""
    stage = prim.GetStage()

    def body_id(path):
        if (link := links.get(path)) is not None:
            return _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, link.name)
        if path in free_paths:
            return _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, f"free_body_{free_paths.index(path)}")
        return -1

    columns = []
    for path in prim.GetRelationship(_SET_DOWN + "joints").GetTargets():
        joint = stage.GetPrimAtPath(path)
        child = _joint_target(joint.GetRelationship("physics:body1")) if joint else None
        link = links.get(str(child)) if child else None
        aid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_ACTUATOR, f"{link.name}_joint_motor") if link else -1
        if aid < 0 or aid not in act_ids:
            return None
        columns.append(act_ids.index(aid))
    hand = [str(p) for p in prim.GetRelationship(_SET_DOWN + "hand").GetTargets()]
    box = [str(p) for p in prim.GetRelationship(_SET_DOWN + "box").GetTargets()]
    hand_id = body_id(hand[0]) if hand else -1
    box_id = body_id(box[0]) if box else -1
    if not columns or hand_id < 0 or box_id < 0:
        return None
    return {"columns": _np.array(columns), "hand": hand_id, "box": box_id,
            "goal": _np.array(goal.Get(), dtype=float), "gain": float(prim.GetAttribute(_SET_DOWN + "gain").Get())}


def _set_down_step(model, data, scratch, goal: dict, release: _np.ndarray, qpos_adr, dq: _np.ndarray) -> _np.ndarray:
    """One frame of a set-down correction: the motor offsets `dq` moved a `gain` share toward
    landing the box on the goal (across the floor; its height is the move's). The box is
    taken where it sits in the hand now and carried to the arm's pose at release (`release`
    plus `dq`) by forward kinematics; its Jacobian over the joints comes from nudging each."""
    hand, box = goal["hand"], goal["box"]
    held = data.xmat[hand].reshape(3, 3).T @ (data.xpos[box] - data.xpos[hand])

    def landing(motors):
        scratch.qpos[:] = data.qpos
        scratch.qpos[qpos_adr] = motors
        _mj.mj_kinematics(model, scratch)
        return scratch.xpos[hand] + scratch.xmat[hand].reshape(3, 3) @ held

    q = release.copy()
    q[goal["columns"]] += dq
    here = landing(q)
    jac = _np.zeros((3, len(dq)))
    for k, c in enumerate(goal["columns"]):
        nudged = q.copy()
        nudged[c] += 1e-3
        jac[:, k] = (landing(nudged) - here) / 1e-3
    miss = goal["goal"] - here
    miss[goal["up"]] = 0.0
    step = jac.T @ _np.linalg.solve(jac @ jac.T + 1e-4 * _np.eye(3), miss)
    return _np.clip(dq + goal["gain"] * step, -_SET_DOWN_CLAMP, _SET_DOWN_CLAMP)


def _articulated_colliders(colliders, up_idx, friction, substeps, dt, hfield_res, floor_drop,
                           collider_mode, collider_thickness, progress=None):
    """The collider spec an articulated asset is built on, and its animated colliders' mocap
    bodies as [(body name, prim)] -- re-posed every frame, or they would stand still."""
    contact_tc = max(2.0 * (dt / substeps), 0.01)
    return _collider_spec(
        colliders, up_idx, friction, contact_tc,
        1.0, hfield_res, floor_drop, collider_mode, collider_thickness, progress=progress,
    )


def build_articulated_spec(
    roots,
    gravity: float,
    friction: float,
    restitution: float,
    mass: float,
    substeps: int,
    dt: float,
    up_idx: int,
    solver_iters: int,
    collision_prims: list | None = None,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    floor_drop: float = 0.0,
    free_root: bool = True,
    collision_margin: float = 0.0,
    collider_spec=None,
    extra=None,
):
    """Compile an articulated asset into a MuJoCo model.

    Split out of the simulate generator so the model can be built, inspected and rendered
    without running a bake -- which is what makes the joint frames checkable at all.

    *collider_spec* is the colliders already built (_articulated_colliders), so the slow
    part can run off the main thread; without it they are built here.

    Returns (model, data, ordered_links), or None if the model would not compile.
    """
    if not roots:
        return None
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    sub_dt = dt / substeps
    contact_tc = max(2.0 * sub_dt, 0.01)
    spec = collider_spec if collider_spec is not None else _articulated_colliders(
        collision_prims, up_idx, friction, substeps, dt, hfield_res, floor_drop, collider_mode,
        collider_thickness,
    )[0]
    # MuJoCo's compiler defaults to interpreting angles as DEGREES. Joint limits are
    # converted to radians when the USD joint is parsed, so leaving this alone applies the
    # conversion twice and a 90-degree door limit compiles as 1.6 degrees -- a hinge that
    # is present, correctly placed, and barely moves.
    spec.compiler.degree = False
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST

    bodies = {}
    link_world = {}
    ordered = [link for root in roots for link in _iter_links(root)]
    motorised = {root.name for root in roots
                 if any(lk.joint is not None and lk.joint.motor for lk in _iter_links(root))}

    for link in ordered:
        world = xform_cache.GetLocalToWorldTransform(link.prim).RemoveScaleShear()
        link_world[link.name] = world
        if link.parent is None:
            parent_body = spec.worldbody
            rel = world
        else:
            parent_body = bodies[link.parent.name]
            rel = world * link_world[link.parent.name].GetInverse()

        body = parent_body.add_body()
        body.name = link.name
        t = rel.ExtractTranslation()
        q = rel.ExtractRotationQuat()
        qi = q.GetImaginary()
        body.pos = [float(t[0]), float(t[1]), float(t[2])]
        body.quat = [q.GetReal(), float(qi[0]), float(qi[1]), float(qi[2])]
        bodies[link.name] = body

        if link.parent is None:
            # A sim-ready asset usually pins its base to the world with a FixedJoint, which
            # is right for manipulation training -- a microwave on a counter stays put. An
            # RBD solver is normally asked the opposite question (drop this and see what
            # happens), so the root gets a free joint unless the caller wants the asset's
            # own anchoring honoured.
            #
            # Except a robot: anchored AND motorised is an arm on its mount, moved by its
            # motors, and freed it falls over as it reaches. A robot with motors and no
            # anchor -- something that walks -- keeps its free root.
            if free_root and not (link.anchored and link.name in motorised):
                root_jnt = body.add_joint()
                root_jnt.type = _mj.mjtJoint.mjJNT_FREE
                root_jnt.name = f"{link.name}_free"
        else:
            j = link.joint
            # A fixed joint means no degree of freedom: the child is welded to its parent,
            # which in MuJoCo is simply a body with no joint at all.
            jnt = None
            if j.kind in ("revolute", "prismatic"):
                jnt = body.add_joint()
                jnt.name = f"{link.name}_joint"
                jnt.type = (_mj.mjtJoint.mjJNT_HINGE if j.kind == "revolute"
                            else _mj.mjtJoint.mjJNT_SLIDE)

            if jnt is not None:
                jnt.pos = j.pos
                jnt.axis = j.axis
                if j.lower is not None and j.upper is not None and j.upper > j.lower:
                    # A limit far beyond a full turn is an asset's way of saying "free
                    # spinning" (a microwave turntable); treat it as unlimited.
                    span = j.upper - j.lower
                    if j.kind != "revolute" or span < 2.0 * math.pi:
                        jnt.limited = True
                        # Measured from the posed links the body is built from, not from
                        # rest: unshifted, a pose that jumped to another solution was
                        # clamped somewhere else entirely, its end 28 cm off.
                        jnt.range = [j.lower - j.offset, j.upper - j.offset]
                if j.motor:
                    _add_motor(spec, jnt, j)
                else:
                    # MjSpec sizes damping and stiffness for a ball joint's three DOFs; a
                    # hinge or slide uses the first slot only.
                    if j.damping is not None:
                        jnt.damping = [max(float(j.damping), 0.0), 0.0, 0.0]
                    if j.stiffness is not None:
                        jnt.stiffness = [max(float(j.stiffness), 0.0), 0.0, 0.0]
                if j.friction is not None:
                    jnt.frictionloss = max(float(j.friction), 0.0)
                if j.armature is not None:
                    jnt.armature = max(float(j.armature), 0.0)

        link_mass = _authored(UsdPhysics.MassAPI(link.prim).GetMassAttr()) if link.prim.HasAPI(UsdPhysics.MassAPI) else None
        n = _add_link_geoms(spec, body, link.prim, world.GetInverse(), friction,
                            contact_tc, restitution, collision_margin)
        if n and link_mass:
            for g in body.geoms:
                g.mass = float(link_mass) / n
        elif n:
            for g in body.geoms:
                g.mass = mass / n
        else:
            _explicit_inertia(body, link.prim, link_mass or mass)

    # Free rigid bodies in the same world (`extra`, (body, frame) pairs as the rigid path
    # makes them): what the robot handles. In a solve of their own the gripper could never
    # touch them; here a cube it closes on is held by friction alone.
    lat = [j for j in range(3) if j != up_idx]
    if extra:
        # Held by friction, a body creeps out of a grip: MuJoCo's soft contacts slide a
        # little even inside the friction cone, and a cube held between two links slid out
        # within a second. The no-slip pass stops that, as the rigid path's does.
        spec.option.noslip_iterations = 3
    for i, (b, f) in enumerate(extra or ()):
        _add_dynamic_body(spec, f"free_body_{i}", f"free_body_{i}_joint", f"free_body_{i}_hull",
                          b, f, contact_tc, collision_margin, lat, 0)

    try:
        model = spec.compile()
    except Exception as exc:  # noqa: BLE001 - a compile failure must not take the chunk down
        logger.warning("Articulated scene compile failed: %s", exc)
        return None

    # Only pairs of the mechanism's own links. The colliders sit in bodies of their own, not
    # the world body, so left unfiltered a link that starts inside one -- an animated collider
    # at its rest pose, a link posed into the table -- lost that contact for the whole solve.
    excluded = _exclude_initial_overlaps(spec, model, only={link.name for link in ordered})
    if excluded:
        try:
            model = spec.compile()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Recompile after exclusions failed: %s", exc)
            return None

    data = _mj.MjData(model)
    _mj.mj_forward(model, data)
    logger.debug(
        "Compiled articulation - %d bodies, %d joints, %d geoms%s",
        model.nbody, model.njnt, model.ngeom,
        f", {excluded} contact pair(s) excluded" if excluded else "",
    )
    return model, data, ordered


def _explicit_inertia(body, prim, mass: float) -> None:
    """Give a link with no collider shape its mass and inertia directly.

    A link whose colliders could not be read -- mesh colliders that never loaded, say --
    gets no geoms, so MuJoCo infers a mass of zero and refuses the whole model with a
    message about mjMINVAL. The asset usually states its inertia anyway (URDF Import
    authors the <inertial> block), and where it does not, a small solid sphere's keeps
    the mechanism simulating rather than refusing it.
    """
    api = UsdPhysics.MassAPI(prim) if prim.HasAPI(UsdPhysics.MassAPI) else None
    inertia = _authored(api.GetDiagonalInertiaAttr()) if api else None
    centre = _authored(api.GetCenterOfMassAttr()) if api else None
    body.explicitinertial = True
    body.mass = max(float(mass), 1e-6)
    if inertia is not None and all(float(v) > 0.0 for v in inertia):
        body.inertia = [float(v) for v in inertia]
    else:
        solid = 0.4 * body.mass * 0.05 ** 2
        body.inertia = [solid, solid, solid]
    if centre is not None:
        body.ipos = [float(v) for v in centre]
    # A principal-axes frame authored on the asset is not read here; the diagonal is taken
    # in the link's own frame, which is what URDF Import writes when it has no rotation.


def _motor_scale(joint) -> float:
    """USD drive units to MuJoCo's: a revolute drive's target is in degrees and its stiffness
    and damping are per degree, MuJoCo works in radians. A prismatic drive is in stage units
    in both."""
    return math.degrees(1.0) if joint.kind == "revolute" else 1.0


def _add_motor(spec, jnt, joint) -> None:
    """A position actuator on `jnt`: UsdPhysics' drive force, stiffness * (target - position)
    - damping * velocity, with the drive's maxForce as the force limit.

    The damping goes in the actuator, not on the joint, so a joint's drive is all in one
    place; the implicit integrator handles an actuator's velocity term as it does a joint's.
    The target is measured from the pose the links are authored in, as Pose Articulation
    writes it: 0 is the rest pose.
    """
    scale = _motor_scale(joint)
    kp = max(float(joint.stiffness), 0.0) * scale
    kv = max(float(joint.damping or 0.0), 0.0) * scale
    act = spec.add_actuator()
    act.name = f"{jnt.name}_motor"
    act.target = jnt.name
    act.trntype = _mj.mjtTrn.mjTRN_JOINT
    act.set_to_position(kp=kp, kv=kv)
    if joint.max_force is not None and 0.0 < joint.max_force < 1e30:
        act.forcelimited = True
        act.forcerange = [-float(joint.max_force), float(joint.max_force)]


def _motor_zeros(ordered) -> dict:
    """Each motor's default target, by link name: the pose a model built now has as zero."""
    zeros = {}
    for link in ordered:
        j = link.joint
        if j is not None and j.motor:
            value = j.target.Get(Usd.TimeCode.Default())
            zeros[link.name] = float(value) if value is not None else 0.0
    return zeros


def _live_model_key(roots, collision_prims, settings) -> tuple | None:
    """What a compiled model depends on besides the motors' targets, or None if it cannot
    be kept.

    The links' pose is left out on purpose: posing moves them on every edit, and a kept
    model takes the new pose as its joints' offsets from the targets it was built at
    (_motor_zeros). That only works where every moving joint is a motor. A passive one
    moved upstream has no target saying how far, so such a mechanism is built afresh.
    """
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    links, bases = [], []
    for root in roots:
        bases.append(tuple(float(v) for row in cache.GetLocalToWorldTransform(root.prim) for v in row))
        for link in _iter_links(root):
            j = link.joint
            if j is not None and j.kind in ("revolute", "prismatic") and not j.motor:
                return None
            links.append((
                link.name, link.parent.name if link.parent else None, link.anchored,
                None if j is None else (j.kind, tuple(j.axis), tuple(j.pos), j.lower, j.upper,
                                        j.stiffness, j.damping, j.max_force, j.friction),
                # Its colliders' material friction: a kept model has it compiled in.
                tuple(_material_friction(p) for p in Usd.PrimRange(link.prim)
                      if p.HasAPI(UsdPhysics.CollisionAPI)),
            ))
    colliders = tuple((str(p.GetPath()), _bounds_key(p), _material_friction(p)) for p in (collision_prims or ()))
    return tuple(links), tuple(bases), colliders, tuple(settings)


def _motor_tracks(model, ordered, start_frame: int, frame_count: int, zeros: dict | None = None):
    """Each motor's target at every frame, in MuJoCo units: (actuator ids, qpos addresses,
    array of frame_count + 1 rows by motor).

    Read on the main thread, before the solve moves to a worker where the stage must not be
    touched. One frame past the end, so the last frame's substeps have a target to ease
    toward. Empty when nothing is a motor.

    Measured from the drive's DEFAULT value, not from zero. The model is built from the
    links' default-time placement, so that is the joint's zero; Pose Articulation authors
    its pose into both the transforms and the default target, and a target taken as
    absolute would turn the joint by the pose a second time. A chunk authoring a rest
    default of 0 reads the same either way.

    `zeros` (by link name) replaces the defaults for a model kept from an earlier pose: its
    zero is the pose it was built in, so a target is measured from the default it had then.
    """
    act_ids, qpos_adr, columns = [], [], []
    times = [Usd.TimeCode(start_frame + f) for f in range(frame_count + 1)]
    for link in ordered:
        j = link.joint
        if j is None or not j.motor:
            continue
        aid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_ACTUATOR, f"{link.name}_joint_motor")
        jid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, f"{link.name}_joint")
        if aid < 0 or jid < 0:
            continue
        to_mj = math.radians(1.0) if j.kind == "revolute" else 1.0
        current = j.target.Get(Usd.TimeCode.Default())
        current = float(current) if current is not None else 0.0
        zero = zeros.get(link.name, current) if zeros is not None else current
        act_ids.append(aid)
        qpos_adr.append(int(model.jnt_qposadr[jid]))
        column = []
        for t in times:
            value = j.target.Get(t)
            column.append(((float(value) if value is not None else current) - zero) * to_mj)
        columns.append(column)
    if not act_ids:
        return [], [], None
    return act_ids, qpos_adr, _np.array(columns).T


def _exclude_initial_overlaps(spec, model, only=None) -> int:
    """Exclude body pairs that are already interpenetrating at frame 0.

    MuJoCo filters contacts between a body and its own parent, which covers adjacent links.
    It does not filter grandparent or sibling pairs -- and a real mechanism is full of those
    that overlap by construction. The SO-101's rest pose is folded, so its base plate sits
    inside links several joints away; MuJoCo resolves that penetration with a separating
    impulse and the arm detonates in three frames. The appliances failed the same way when
    they started interpenetrated.

    Excluding *every* pair within an articulation would be the easy fix and the wrong one: a
    door swinging into its frame is a link-against-link contact that the whole simulation is
    about. What is never meaningful is a pair that starts inside itself, because no contact
    force can separate them without launching the mechanism.

    So: compile, look at the contacts that exist before anything has moved, and exclude only
    those. Pairs that meet later still collide.

    `only`, a set of body names, limits it to pairs of those bodies.
    """
    data = _mj.MjData(model)
    _mj.mj_forward(model, data)

    pairs = set()
    for i in range(data.ncon):
        contact = data.contact[i]
        if contact.dist >= 0.0:
            continue  # touching, not interpenetrating
        b1 = model.geom_bodyid[contact.geom1]
        b2 = model.geom_bodyid[contact.geom2]
        if b1 == b2 or 0 in (b1, b2):
            continue  # same body, or against the world: both are meaningful
        n1 = model.body(b1).name
        n2 = model.body(b2).name
        if n1 and n2 and (only is None or (n1 in only and n2 in only)):
            pairs.add(tuple(sorted((n1, n2))))

    for first, second in sorted(pairs):
        exclude = spec.add_exclude()
        exclude.bodyname1 = first
        exclude.bodyname2 = second
    return len(pairs)


def _write_articulated_results(stage, roots, layer, results, start_frame,
                               settle_only=False, node=None):
    """Author each link's simulated transform back onto its own prim.

    Follows _write_prim_results rather than authoring a plain transform op, for the two
    reasons recorded there:

    * The bake goes into THIS chunk's own named op, and the op order is derived the same
      way every other transform-authoring chunk derives it. Clearing the order and writing
      a bare xformOp:transform excludes an upstream Transform's op entirely -- the op stays
      on the prim, typed and valued, and is never read.
    * What is written is this chunk's *share*, not the absolute placement. The simulated
      matrix says where the link should end up in world space; composing that on top of the
      ops that already placed it would apply them twice.

    settle_only writes the resting pose as the attribute's default with no time samples: a
    prim carrying PhysicsRigidBodyAPI *and* animated transforms is contradictory, and a
    downstream simulator would fight the animation.
    """
    if not results:
        return

    # Match the target layer's timeCodesPerSecond to the stage's before authoring samples.
    # USD maps sample times through a layer offset built from the ratio between them, so
    # writing frame 1..40 into a 24-TCPS layer over a 60-TCPS stage silently lands them at
    # 2.5..100. The values are right and the timing is wrong, which surfaces as an
    # animation that plays at the wrong speed rather than as any kind of error.
    stage_tcps = stage.GetTimeCodesPerSecond()
    if stage_tcps and layer.timeCodesPerSecond != stage_tcps:
        layer.timeCodesPerSecond = stage_tcps

    op_name = chunk_utils.xform_op_name(node) if node is not None else "xformOp:transform"
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    written = 0

    if settle_only:
        _write_settled(stage, roots, layer, results, op_name, xform_cache, node)
        return

    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        for link in (found for root in roots for found in _iter_links(root)):
            mats = results.get(link.name)
            if not mats:
                continue
            prim = link.prim

            parent = prim.GetParent()
            parent_world_inv = Gf.Matrix4d(1.0)
            if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
                parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()

            order = (
                chunk_gizmo.xform_op_order(node, prim, op_name)
                if node is not None
                else [op_name]
            )
            order_attr = prim.GetAttribute("xformOpOrder")
            if not order_attr:
                order_attr = prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray)
            order_attr.Set(order)

            xf_attr = prim.GetAttribute(op_name)
            if not xf_attr:
                xf_attr = prim.CreateAttribute(op_name, Sdf.ValueTypeNames.Matrix4d)

            inv_upstream = _upstream_xform(prim, op_name).GetInverse()
            if settle_only:
                xf_attr.Set(mats[-1] * parent_world_inv * inv_upstream)
            else:
                # An animated upstream -- a bake the motors are following -- is divided
                # out at each frame's own time. Its default-time value would leave the
                # animation in, and the link would move by it twice.
                # Read into a list before writing: write_samples authors inside a change
                # block, where reading the prim being written is not to be trusted.
                animated = UsdGeom.Xformable(prim).TransformMightBeTimeVarying()
                samples = [
                    (start_frame + i, mat * parent_world_inv * (
                        _upstream_xform(prim, op_name, Usd.TimeCode(start_frame + i)).GetInverse()
                        if animated else inv_upstream))
                    for i, mat in enumerate(mats)
                ]
                sim_bake.write_samples(
                    xf_attr, samples, default=mats[0] * parent_world_inv * inv_upstream,
                )
            written += 1

    if not settle_only and written:
        frames = len(next(iter(results.values())))
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.SetStartTimeCode(start_frame)
            stage.SetEndTimeCode(start_frame + frames - 1)


def _write_settled(stage, roots, layer, results, op_name, xform_cache, node) -> None:
    """Each link's resting pose as its op's default, all links in one Sdf change block.

    Every Usd call that authors sends a change notice the stage, the framework and the
    viewport each react to. Three per link (the op order, the op, its value) made writing a
    robot's settled pose cost more than settling it -- most of a live settle on every move.
    So everything is read first, then written straight into the layer's specs at once.
    """
    pending = []
    for link in (found for root in roots for found in _iter_links(root)):
        mats = results.get(link.name)
        if not mats:
            continue
        prim = link.prim
        parent = prim.GetParent()
        parent_world_inv = Gf.Matrix4d(1.0)
        if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
            parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()
        order = chunk_gizmo.xform_op_order(node, prim, op_name) if node is not None else [op_name]
        inv_upstream = _upstream_xform(prim, op_name).GetInverse()
        pending.append((prim.GetPath(), order, mats[-1] * parent_world_inv * inv_upstream))

    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        writer = sdf_authoring.Writer(stage)
        with Sdf.ChangeBlock():
            for path, order, value in pending:
                spec = writer.over(path)
                sdf_authoring.attribute(spec, "xformOpOrder", Sdf.ValueTypeNames.TokenArray,
                                        Vt.TokenArray(order), uniform=True)
                # custom, as Prim.CreateAttribute made it before
                sdf_authoring.attribute(spec, op_name, Sdf.ValueTypeNames.Matrix4d, value, custom=True)


def _run(owner, notice, sender) -> None:
    if not _HAS_MUJOCO:
        logger.warning("mujoco not installed. Run: pip install mujoco")
        return

    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    target = _chunk.get_param("prim_path")
    prim_paths = target.resolve_paths() if target is not None else []
    if not prim_paths:
        logger.warning("No target prim selected")
        return

    # A typed param's .value already returns the correct type and falls back to its
    # default_value when unset, so no cast or `or <fallback>` is needed. (The `or`
    # form was also a latent bug: it clobbered a legitimate 0, e.g. zero bounce.)
    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)

    collision_path_value = _chunk.get_param("collision_path", is_s).value

    frame_count = _chunk.get_param("frame_count", is_i).value
    fps = _chunk.get_param("fps", is_f).value
    gravity = sim_bake.gravity(_chunk.get_param("gravity", is_f).value, stage)
    # The physics scene's step rate when one is authored, as PhysX steps it.
    substeps = _scene_substeps(stage, fps, max(1, _chunk.get_param("substeps", is_i).value))
    start_frame = _chunk.get_param("start_frame", is_i).value
    friction = _chunk.get_param("friction", is_f).value
    restitution = _chunk.get_param("restitution", is_f).value
    mass = _chunk.get_param("mass", is_f).value
    lin_damping = _chunk.get_param("lin_damping", is_f).value
    ang_damping = _chunk.get_param("ang_damping", is_f).value
    shape_type = _chunk.get_param("shape_type", is_s).value
    init_vel = _vec3(_chunk, "initial_velocity")
    ang_vel = _vec3(_chunk, "angular_velocity")
    floor_drop = _chunk.get_param("floor_drop", is_f).value
    settle_only = bool(_chunk.get_param("settle_to_rest", chunk.is_param_t(bool)).value)
    # Live Settle: settle on every cook, inline. One preview frame, then stepping on until
    # at rest -- the pose physics lets the robot hold, not an animation.
    live = bool(getattr(_chunk, "_rbd_live", False))
    if live:
        settle_only = True
        frame_count = 1
    forces = dict(
        wind=_vec3(_chunk, "wind"),
        turbulence=_chunk.get_param("turbulence", is_f).value,
        turb_scale=_chunk.get_param("turb_scale", is_f).value,
        turb_speed=_chunk.get_param("turb_speed", is_f).value,
        turb_seed=_chunk.get_param("turb_seed", is_i).value,
    )

    layer = owner.get_edit_context()
    if not layer:
        return
    # A fractured instancer with Breakable on bakes through the breakage path instead -- on a
    # worker thread, keeping the last bake on screen until the new one is ready.
    if not live and _chunk.get_param("breakable", chunk.is_param_t(bool)).value and _run_breakable(
        owner, _chunk, stage, layer, prim_paths[0], collision_path_value, forces
    ):
        return
    if getattr(_chunk, "_rbd_cache_only", False):
        # Only looking for a cached breakable bake (opening a scene): anything else waits
        # for Run, as it always has.
        return
    if sim_bake.is_running(_chunk):
        _report(_chunk, "already simulating -- wait, or press Cancel")
        return
    # Cleared first: the paths below read the target's starting state off the real stage,
    # and the last bake in this layer would make a new run start where the last one ended.
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # The target prim comes from the REAL stage, not a scratch one: simulate_mujoco[_instancer]
    # write baked results back through the same instancer/attribute objects they read `prim`
    # from (positions_attr.Set(...) inside a `with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer))`), and
    # Usd.EditContext only governs the stage object it's given -- it has no effect on attributes
    # bound to a *different* Usd.Stage. A scratch-stage-bound prim here silently bakes the whole
    # simulation onto the throwaway stage instead of `layer`, discarded the moment this function
    # returns (found the hard way: "Run Simulation" printed a successful bake, but the layer
    # stayed empty). Collision prims are read-only -- never written back through -- so they're
    # still safely scoped through get_upstream_stage: a downstream chunk's edit to a collision
    # prim can't leak into the simulation, without risking the same silent-write-loss bug.
    #
    # Every prim the param selects is simulated. Plain rigid bodies all go into ONE solve, so
    # they collide with each other as well as with the colliders. A PointInstancer (whose
    # instances already share a solve) and an articulated asset keep a solve of their own.
    targets = []
    for path in prim_paths:
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid() and prim.GetPath() != Sdf.Path("/") and prim.GetPath() not in targets:
            targets.append(prim.GetPath())
    if not targets:
        return
    # Held by the bake below, not just by this function: a Usd.Prim does not keep its stage
    # alive, and in a UI the bake runs on after _run returns. The collision prims then
    # expired, and the bake failed with "Invalid range starting with expired prim".
    scratch_stage = chunk_stage.get_upstream_stage(stage, owner.get_scene(), _chunk)
    collision_prims = _parse_collision_prims(collision_path_value, scratch_stage)
    collider_mode = _chunk.get_param("collider_mode", is_s).value
    tri_budget = _chunk.get_param("tri_budget", is_i).value
    collider_warning = _mesh_collider_warning(collision_prims, collider_mode, tri_budget)
    # The steps are made before the bake hands them a Context: the collider build reports
    # through this box once it has one.
    box: dict = {}

    def progress(done: int, total: int) -> None:
        ctx = box.get("ctx")
        if ctx is None:
            return
        ctx.check()  # Cancel stops a long collider build too
        ctx.status(f"building the collider: {done:,} of {total:,} triangles"
                   + (f"\n{collider_warning}" if collider_warning else ""))

    collision_margin = _chunk.get_param("collision_margin", is_f).value
    hfield_res = _chunk.get_param("hfield_res", is_i).value
    collider_thickness = _chunk.get_param("collider_thickness", is_f).value
    defaults = dict(gravity=gravity, mass=mass, friction=friction, restitution=restitution,
                    shape_type=shape_type)

    # Sorted into jobs now, before anything is baked: every target's starting state is read
    # off the stage as it is, not as an earlier job left it.
    rigid: list[dict] = []
    jobs: list[tuple] = []
    for path in targets:
        prim = stage.GetPrimAtPath(path)
        # A sim-ready asset states its own mass, contact properties and collider approximation.
        # Prefer those; the params stay as the fallback for geometry that says nothing.
        authored = _authored_physics(stage, prim, defaults)
        if authored["kinematic"]:
            logger.debug("target prim %s is a kinematic rigid body -- driven, not solved", path)
        # An asset that carries UsdPhysics joints is a mechanism, not a single rigid body:
        # simulate it as a kinematic tree so a door or drawer keeps its own motion. Assets
        # with no joints fall through to the paths below unchanged.
        if read_articulations(prim):
            jobs.append(("articulated", path, authored))
        elif UsdGeom.PointInstancer(prim).GetPositionsAttr().HasValue():
            jobs.append(("instancer", path, authored))
        else:
            xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
            parent = prim.GetParent()
            parent_world_inv = Gf.Matrix4d(1.0)
            if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
                parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()
            rigid.append(dict(
                path=path,
                parent_world_inv=parent_world_inv,
                scale_xyz=_bake_scale(prim, xform_cache),
                mass=authored["mass"],
                friction=authored["friction"],
                restitution=authored["restitution"],
                shape_type=authored["shape_type"],
                gravity=authored["gravity"],
            ))
    # An articulated asset and the plain rigid bodies share one world: they join the first
    # articulated asset's solve, so its links can close on a cube and lift it by friction. In
    # a solve of their own they would never touch.
    holder = next((path for kind, path, _ in jobs if kind == "articulated"), None)
    if rigid and holder is None:
        jobs.insert(0, ("rigid", None, None))
    # And every articulated asset shares it too: two mechanisms touching one part have to
    # touch it, and each other. Solved one by one, the second passed through what the
    # first held. One job for them all, on the first one's settings.
    articulated = [path for kind, path, _ in jobs if kind == "articulated"]
    if len(articulated) > 1:
        jobs = [job for job in jobs if job[0] != "articulated" or job[1] == holder]

    def rigid_steps():
        """The plain rigid bodies, in one world together."""
        bodies = []
        for body in rigid:
            # Looked up by path: a handle taken while sorting can expire before the bake.
            prim = stage.GetPrimAtPath(body["path"])
            if prim and prim.IsValid():
                bodies.append(dict(body, prim=prim))
        if not bodies:
            return iter(())
        return _prims_steps(
            stage,
            bodies,
            layer,
            frame_count,
            fps,
            # The stage's gravity, which _authored_physics reads off the scene, not the body.
            bodies[0]["gravity"],
            substeps,
            init_vel,
            ang_vel,
            start_frame,
            collision_prims=collision_prims,
            # The colliders' friction: the first body's, which is what a single target used.
            friction=bodies[0]["friction"],
            lin_damping=lin_damping,
            ang_damping=ang_damping,
            collision_margin=collision_margin,
            solver_iters=30,
            hfield_res=hfield_res,
            collider_mode=collider_mode,
            collider_thickness=collider_thickness,
            tri_budget=tri_budget,
            floor_drop=floor_drop,
            node=_chunk,
            settle_only=settle_only,
            **forces,
            progress=progress,
        )

    def own_steps(kind, prim, authored):
        """An instancer's or an articulated asset's steps: a solve of its own."""
        if kind == "articulated":
            roots = read_articulations(prim)
            if prim.GetPath() == holder and len(articulated) > 1:
                for other in articulated[1:]:
                    roots += read_articulations(stage.GetPrimAtPath(other))
                _unique_link_names(roots)
            return _articulated_steps(
                stage,
                roots,
                layer,
                frame_count,
                fps,
                authored["gravity"],
                authored["mass"],
                substeps,
                init_vel,
                ang_vel,
                start_frame,
                collision_prims=collision_prims,
                friction=authored["friction"],
                restitution=authored["restitution"],
                solver_iters=30,
                hfield_res=hfield_res,
                collider_mode=collider_mode,
                collider_thickness=collider_thickness,
                floor_drop=floor_drop,
                lin_damping=lin_damping,
                ang_damping=ang_damping,
                collision_margin=collision_margin,
                settle_only=settle_only,
                node=_chunk,
                **forces,
                progress=progress,
                # A live settle keeps each articulation's compiled model between cooks.
                cache=_live_cache(_chunk, prim.GetPath()) if live else None,
                extra=rigid if prim.GetPath() == holder else None,
            )
        return _instancer_steps(
            stage,
            prim,
            layer,
            collision_prims,
            frame_count,
            fps,
            authored["gravity"],
            authored["mass"],
            substeps,
            init_vel,
            ang_vel,
            start_frame,
            friction=authored["friction"],
            restitution=authored["restitution"],
            lin_damping=lin_damping,
            ang_damping=ang_damping,
            shape_type=authored["shape_type"],
            collision_margin=collision_margin,
            solver_iters=30,
            hfield_res=hfield_res,
            collider_mode=collider_mode,
            collider_thickness=collider_thickness,
            tri_budget=tri_budget,
            floor_drop=floor_drop,
            settle_only=settle_only,
            **forces,
            progress=progress,
        )

    # Read now, not in finish: finish moves the bake's layer to the top of the stack, which
    # recomposes the stage and expires the prims. Asking one for its name then raised -- every
    # time a script reopened a saved RBD scene, since the bake now runs during the load.
    names = [path.name for path in targets]
    total_frames = frame_count * len(jobs)

    def bake(ctx):
        """Each job's steps in turn, with their frames as progress.

        The layer is cleared again just before a result is written: a cook while the solve ran
        may have put the kept bake back. The jobs already baked are put back after it.
        """
        box["ctx"] = ctx
        # Keeps the collision prims' stage alive for as long as the bake reads them.
        box["scratch_stage"] = scratch_stage
        if collider_warning:
            ctx.status(collider_warning)
        done_layer = None
        for index, (kind, path, authored) in enumerate(jobs):
            if kind == "rigid":
                if len(jobs) > 1 or len(rigid) > 1:
                    ctx.status(f"simulating {len(rigid)} rigid bodies together")
                steps = rigid_steps()
            else:
                # Looked up again here, on the main thread: the jobs before it wrote to the stage.
                prim = stage.GetPrimAtPath(path)
                if not prim or not prim.IsValid():
                    continue
                if len(jobs) > 1:
                    ctx.status(f"simulating {path.name} ({index + 1} of {len(jobs)})")
                steps = own_steps(kind, prim, authored)
            offset = index * frame_count
            while True:
                try:
                    step = next(steps)
                except StopIteration:
                    break
                if isinstance(step, int):
                    ctx.progress(offset + step, total_frames)
                yield step
                if step is sim_bake.MAIN and (lreg := chunk.get_layer_manager()):
                    lreg.clear_layer(layer.identifier)
                    if done_layer is not None:
                        layer.TransferContent(done_layer)
            # Back on the main thread here: every step generator writes its result after a MAIN.
            if index < len(jobs) - 1:
                done_layer = Sdf.Layer.CreateAnonymous("rbd_solver_done")
                done_layer.TransferContent(layer)

    if len(names) == 1:
        target_text = names[0]
    else:
        shown = ", ".join(names[:3]) + (f", +{len(names) - 3} more" if len(names) > 3 else "")
        target_text = f"{len(names)} prims ({shown})"

    def finish(_result):
        if _chunk.get_edit_context() is None:
            return  # the chunk went away while it was simulating
        # Ensure the baked animation overrides any chunk (e.g. ScatterPoints) that authored
        # the target prim, otherwise its static values win and playback shows nothing.
        _promote_bake_layer(layer, _chunk)
        try:
            scene = chunk_registry.ChunkRegistry().scene
            # A settle bakes no animation, so there is no range to widen the timeline to.
            if scene and not settle_only:
                scene.refresh_timeline(start_frame, start_frame + frame_count - 1)
        except Exception:
            pass
        _report(_chunk, f"settled {target_text}" if settle_only else
                f"baked {frame_count} frames on {target_text}"
                + (f"\n{collider_warning}" if collider_warning else ""))
        _chunk.unset_state(chunk.ChunkFlags.State.STALE)

    sim_bake.run(_chunk, bake, report=lambda text: _report(_chunk, text), finish=finish, name="RBDSolver",
                 inline=live)


def _live_cache(node, path) -> dict:
    """The dict a live settle keeps one articulation's compiled model in, on the chunk."""
    models = getattr(node, "_rbd_live_models", None)
    if models is None:
        models = {}
        node._rbd_live_models = models
    return models.setdefault(str(path), {})


def _breakage():
    """The breakage module beside this file (it has no main, so it is not a chunk).

    Loaded by path rather than imported: chunk folders are not packages, and this module is
    itself loaded under different names inside and outside the repo.
    """
    name = "usd_chunks_rbd_breakage"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("rbd_breakage.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text
    logger.info("[RBD Solver] %s", text.replace("\n", " | "))


def cache_key(node) -> str | None:
    """What the cook cache keys a breakable bake on: its inputs, while Run asks (sim_bake)."""
    return sim_bake.input_key(node)


def _run_breakable(owner, _chunk, stage, layer, target_path, collision_path_value, forces) -> bool:
    """Bake a fractured PointInstancer with breakage. False when the target is not one.

    Reads everything from the upstream stage -- this chunk's own layer holds the last bake,
    which rewrites the very instancer being read -- and only replaces that bake once the new
    one is ready.
    """
    brk = _breakage()
    scene = owner.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, _chunk) if scene is not None else stage
    target = upstream.GetPrimAtPath(target_path)
    if not target or not target.IsA(UsdGeom.PointInstancer):
        return False
    frac = brk.read_fracture(upstream, target)
    if not frac:
        return False
    if sim_bake.is_running(_chunk):
        _report(_chunk, "already simulating -- wait, or press Cancel")
        return True

    is_f, is_i = chunk.is_param_t(float), chunk.is_param_t(int)
    frame_count = max(2, _chunk.get_param("frame_count", is_i).value)
    fps = max(1.0, _chunk.get_param("fps", is_f).value)
    start_frame = _chunk.get_param("start_frame", is_i).value
    up_idx = {"X": 0, "Y": 1, "Z": 2}.get(UsdGeom.GetStageUpAxis(stage).upper(), 1)
    up = _np.zeros(3)
    up[up_idx] = 1.0
    pieces = frac["pieces"]
    radius = min(float(_np.linalg.norm(p["points"], axis=1).max()) for p in pieces.values() if len(p["points"]))

    colliders = _parse_collision_prims(collision_path_value, upstream)
    world = brk.gather_world(upstream, colliders, frame_count, start_frame, fps,
                             _chunk.get_param("collider_thickness", is_f).value, radius)

    # Every instance is a body, read entirely off the instancer: where it is, how it moves
    # (USD's own velocities / angularVelocities), and -- if something like the Emit chunk
    # wrote one -- the frame it enters (primvars:rbd:emitFrame). The Initial State params
    # stand in for instances that carry no velocities of their own.
    inst = UsdGeom.PointInstancer(target)
    xf = _np.array(UsdGeom.Xformable(target).ComputeLocalToWorldTransform(start_frame), dtype=_np.float64)
    rot = Gf.Matrix4d(xf.tolist()).RemoveScaleShear().ExtractRotationQuat()

    def per_instance(attr, width=3):
        v = attr.Get(start_frame) if attr else None
        return _np.asarray(v, dtype=_np.float64).reshape(-1, width) if v is not None and len(v) else None

    positions = per_instance(inst.GetPositionsAttr())
    positions = positions if positions is not None else _np.zeros((0, 3))
    orients = inst.GetOrientationsAttr().Get(start_frame) or []
    indices = list(inst.GetProtoIndicesAttr().Get(start_frame) or [])
    velocities = per_instance(inst.GetVelocitiesAttr())
    spins = per_instance(inst.GetAngularVelocitiesAttr())
    frames_pv = UsdGeom.PrimvarsAPI(target).GetPrimvar("rbd:emitFrame")
    emit_frames = _np.asarray(frames_pv.Get(), dtype=_np.float64) if frames_pv and frames_pv.HasValue() else None
    keys = [frac["proto_key"][i] if 0 <= i < len(frac["proto_key"]) else None for i in indices]
    lin_default = _np.asarray(_vec3(_chunk, "initial_velocity"), dtype=_np.float64)
    ang_default = _np.asarray(_vec3(_chunk, "angular_velocity"), dtype=_np.float64)
    starts = []
    for i, key in enumerate(keys):
        if key is None or i >= len(positions):
            continue
        q = Gf.Quatd(orients[i]) if i < len(orients) else Gf.Quatd(1.0)
        wq = (rot * q).GetNormalized()
        wi = wq.GetImaginary()
        lin = velocities[i] @ xf[:3, :3] if velocities is not None and i < len(velocities) else lin_default
        ang = (_np.radians(spins[i]) @ xf[:3, :3] if spins is not None and i < len(spins) else ang_default)
        when = 0.0
        if emit_frames is not None and i < len(emit_frames):
            when = max(0.0, (emit_frames[i] - start_frame) / fps)
        starts.append(dict(key=key, pos=positions[i] @ xf[:3, :3] + xf[3, :3],
                           quat=_np.array([wq.GetReal(), wi[0], wi[1], wi[2]]), lin=lin, ang=ang, time=when))
    skipped = len(indices) - len(starts)
    if not starts:
        _report(_chunk, "nothing to simulate -- the instancer has no fractured instances")
        return True

    job = dict(
        up_idx=up_idx,
        gravity=sim_bake.gravity(_chunk.get_param("gravity", is_f).value, stage),
        fps=fps,
        frames=frame_count,
        substeps=_scene_substeps(stage, fps, max(1, _chunk.get_param("substeps", is_i).value)),
        iterations=20,
        friction=_authored_physics(stage, target, dict(
            gravity=0.0, mass=1.0, friction=_chunk.get_param("friction", is_f).value,
            restitution=0.0, shape_type=""))["friction"],
        restitution=_chunk.get_param("restitution", is_f).value,
        density=1.0,
        lin_damping=_chunk.get_param("lin_damping", is_f).value,
        ang_damping=_chunk.get_param("ang_damping", is_f).value,
        floor_drop=_chunk.get_param("floor_drop", is_f).value,
        statics=world["statics"],
        belts=world["belts"],
        drivers=world["drivers"],
        pieces=pieces,
        starts=starts,
        breakage=dict(
            strength=_chunk.get_param("strength", is_f).value,
            variation=_chunk.get_param("strength_variation", is_f).value,
            max_generation=max(0, _chunk.get_param("max_generations", is_i).value),
            cooldown=max(0.0, _chunk.get_param("break_delay", is_f).value),
            kick=_chunk.get_param("break_kick", is_f).value,
        ),
        rest_until_hit=_chunk.get_param("rest_until_hit", chunk.is_param_t(bool)).value,
        seed=_chunk.get_param("turb_seed", is_i).value,
    )
    if any(forces.get("wind", ())) or forces.get("turbulence"):
        logger.info("RBD Solver: Wind and Turbulence are not applied to breakable bodies yet")
    proto_index = frac["proto"]
    spins = world["spins"]
    note = f" · {skipped} unfractured instances left out" if skipped else ""
    if emit_frames is not None:
        note += f"\nemitted over frames {int(emit_frames.min())}-{int(emit_frames.max())} (primvars:rbd:emitFrame)"
    note += "\n" + brk.describe_world(world)

    def finish(result):
        if _chunk.get_edit_context() is None:
            return  # the chunk went away while it was simulating
        if lreg := chunk.get_layer_manager():
            lreg.clear_layer(layer.identifier)
        with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
            brk.author_result(stage, stage.GetPrimAtPath(target_path), result, proto_index, start_frame, spins)
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.SetStartTimeCode(start_frame)
            stage.SetEndTimeCode(start_frame + frame_count - 1)
        _promote_bake_layer(layer, _chunk)
        try:
            host = chunk_registry.ChunkRegistry().scene
            if host is not None:
                host.refresh_timeline(start_frame, start_frame + frame_count - 1)
        except Exception:  # noqa: BLE001 - a timeline refresh is a courtesy
            pass
        _chunk._rbd_bake_key = key
        _report(_chunk, brk.summary(result) + note)
        if entry is not None:
            entry.store(_chunk, layer)
        _chunk.unset_state(chunk.ChunkFlags.State.STALE)

    # Nothing that shapes the bake changed: keep it. A bake made from exactly this input
    # before -- this session or an earlier one -- comes back from the cook cache instead of
    # being simulated again, the way a File Cache node would.
    key = sim_bake.digest(job, proto_index, str(target_path), start_frame, spins)
    if key == getattr(_chunk, "_rbd_bake_key", None) and layer.GetPrimAtPath(target_path):
        _chunk.unset_state(chunk.ChunkFlags.State.STALE)
        return True
    # Taken now, not when the bake lands: a param changed while it runs must not relabel it.
    entry = sim_bake.cache_entry(_chunk, key)
    if entry is not None and entry.restore(_chunk, layer):
        _chunk._rbd_bake_key = key
        _promote_bake_layer(layer, _chunk)
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.SetStartTimeCode(start_frame)
            stage.SetEndTimeCode(start_frame + frame_count - 1)
        _report(_chunk, _chunk.get_param("readout").value + "\nread back from the cook cache")
        _chunk.unset_state(chunk.ChunkFlags.State.STALE)
        return True
    if getattr(_chunk, "_rbd_cache_only", False):
        _chunk.set_state(chunk.ChunkFlags.State.STALE)
        _report(_chunk, "not simulated -- press Run Simulation")
        return True

    def live(partial):
        # The frames simulated so far, on screen while the rest are worked out -- the later
        # frames hold the last one done. Replaced by the full result when it lands.
        if _chunk.get_edit_context() is None:
            return
        if lreg := chunk.get_layer_manager():
            lreg.clear_layer(layer.identifier)
        with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
            brk.author_result(stage, stage.GetPrimAtPath(target_path), partial, proto_index, start_frame, spins)
        _promote_bake_layer(layer, _chunk)

    brk.dispatch(_chunk, job, finish, lambda text: _report(_chunk, text), live=live)
    return True


def _cancel(owner, notice, sender) -> None:
    sim_bake.cancel(owner.get_chunk())


_AUTO_RUN_DELAY_MS = 600
# Params that don't represent a real simulation input -- clicking/toggling them must not
# flip the staleness indicator (Run/Reset explicitly manage it themselves; Auto Simulate and
# the collision-list buttons are controls, not sim inputs).
_STALE_IGNORE_PARAMS = {
    "run_sim",
    "reset_sim",
    "auto_sim",
    "add_collision",
    "clear_collision",
    "cancel_sim",
    "readout",
    cook_cache.TOGGLE,
}


def _mark_stale(owner, notice, sender) -> None:
    _chunk = owner.get_chunk()
    _chunk.set_state(chunk.ChunkFlags.State.STALE)
    if _chunk.get_param("auto_sim", chunk.is_param_t(bool)).value:
        _schedule_auto_run(_chunk)


def _on_auto_sim_toggled(owner, notice, sender) -> None:
    _chunk = owner.get_chunk()
    if _chunk.get_param("auto_sim", chunk.is_param_t(bool)).value and _chunk.has_state(
        chunk.ChunkFlags.State.STALE
    ):
        _schedule_auto_run(_chunk)


def _control_changed(owner, notice, sender) -> None:
    """A control changed (Run, Auto Simulate, the readout, ...): the cook it brings is ours.

    Every param change cooks its chunk, and execute() takes any cook it did not ask for as an
    input having changed. A control is not an input, so it says so first: the callbacks run
    during the solve, before the cook. Not for a change that brings no cook -- the readout
    is a Label, which nothing computes from -- or the mark would outlive it and swallow the
    next real change. Asked here, inside the notice, while the solve still holds the verdict.
    """
    if owner.get_dag_node().only_presentation_changed():
        return
    owner.get_chunk()._rbd_controls_cook = True


def _note_input_change(node) -> None:
    """A cook no control asked for: an input changed, so the bake is out of date.

    An input is one of the solver's own params (which _mark_stale marked already) or a chunk
    wired into it: a chunk upstream reaches the solver only through its wires, and only
    when what it changed is something the solver reads (skip_cook) -- not for a change
    nothing computes from, such as a Scatter Brush stroke, which cooks nothing. So this
    is where an upstream change marks the solver, and Auto Simulate re-bakes from here,
    once the chunks above have cooked.
    """
    if getattr(node, "_rbd_controls_cook", False):
        node._rbd_controls_cook = False
        return
    node.set_state(chunk.ChunkFlags.State.STALE)
    auto_sim = node.get_param("auto_sim", chunk.is_param_t(bool))
    if auto_sim is not None and auto_sim.value:
        _schedule_auto_run(node)


def execute(node) -> None:
    """Bake on cook -- headless; interactively, only fetch a cached bake.

    Interactively a bake is expensive, so it waits for Run or Auto Simulate -- but the first
    cook of a session (opening the scene) reads a bake back from the cook cache if one was
    made from exactly this input, so a scene reopens simulated. Later cooks leave the bake
    alone: gathering the inputs to check costs a second or two, too much for every tweak.
    Under ``usdchunks run`` or a sweep nobody can press anything, and a network that cooked
    without baking renders a motionless scene that looks entirely plausible -- so it bakes,
    or loads the cache.

    Live Settle is the exception everywhere: it settles on every cook, inline, so moving
    anything upstream (a pose handle) shows at once where physics lets it rest.
    """
    live = node.get_param("live_settle", chunk.is_param_t(bool))
    if live is not None and live.value and not sim_bake.is_running(node):
        node._rbd_live = True
        try:
            _run(node, None, None)
        finally:
            node._rbd_live = False
        node.unset_state(chunk.ChunkFlags.State.STALE)
        return
    if sim_bake.is_unattended():
        _run(node, None, None)
        return
    if getattr(node, "_rbd_cache_checked", False):
        # The framework cleared this layer for the cook; put the last bake back. Baking
        # again would cost a full simulation on every tweak upstream.
        sim_bake.restore(node, node.get_edit_context())
        _note_input_change(node)
        return
    node._rbd_cache_checked = True
    node._rbd_cache_only = True
    try:
        _run(node, None, None)
    finally:
        node._rbd_cache_only = False


def _schedule_auto_run(root_chunk) -> None:
    """Debounce auto-runs: a sim bake is comparatively expensive, so wait for input changes to
    settle for _AUTO_RUN_DELAY_MS before re-running, rather than baking on every keystroke/drag
    tick while a param is being edited.

    Headless there is nothing to debounce and no event loop to wait in; the bake happens
    in execute() instead, when the cook reaches this chunk. Interactively the host waits
    (SceneSender.call_later), so this runs in usdview and Blender alike: a new change
    cancels the wait the last one started.
    """
    if sim_bake.is_unattended():
        # Headless, execute() bakes instead: the cook calls it in dependency order, once
        # every upstream chunk has cooked. Running here too baked against a half-cooked
        # network -- a solver fired by the first upstream chunk's change, before the
        # colliders it needed existed -- and then again, a full simulation per upstream.
        return

    scene = root_chunk.get_scene()
    if scene is None:
        return
    cancel = getattr(root_chunk, "_auto_run_cancel", None)
    if cancel is not None:
        cancel()
    root_chunk._auto_run_cancel = scene.call_later(
        lambda: _run(root_chunk, None, None), _AUTO_RUN_DELAY_MS / 1000.0)


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("RBD Solver")

    # ------------------------------------------------------------------
    # Header -- the things you touch on every single sim, left uncollapsed so the node
    # reads as "pick a prim, press go" at a glance.
    chunk_api.add_param(
        chunk.LineEdit(
            "prim_path",
            "Target Prim",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="The prims to simulate — meshes, or PointInstancers to simulate every instance. "
            "Selected rigid bodies are solved together, so they collide with each other.",
        )
    )

    # One Run button, at the top. The sections below start collapsed, so the param list is
    # short enough not to need a second one at the bottom.
    run_btn = chunk.Button(
        "run_sim", "Run Simulation", tooltip="Simulate and bake the animation onto the target prim."
    )
    run_btn.add_callback(_run)
    chunk_api.add_param(run_btn)

    reset_btn = chunk.Button("reset_sim", "Reset", tooltip="Clear the baked animation from this chunk's layer.")
    reset_btn.add_callback(reset)
    chunk_api.add_param(reset_btn)

    cancel_btn = chunk.Button("cancel_sim", "Cancel", tooltip="Stop a breakable simulation that is running.")
    cancel_btn.add_callback(_cancel)
    chunk_api.add_param(cancel_btn)
    chunk_api.add_param(chunk.Label("readout", "not simulated yet", default_value="not simulated yet",
                                    tooltip="What the last run did."))

    # On by default headless, off in a UI. The reason it is off interactively is that a bake
    # is expensive and the user can see the orange "needs simulation" border and press the
    # button. Neither is true in `usdchunks run` or a sweep: nobody is watching a border and
    # nobody can press anything, so cooking the network produced a chunk that quietly baked
    # nothing and a scene that rendered a motionless, entirely plausible-looking result.
    interactive = not sim_bake.is_unattended()
    auto_sim = chunk.Checkbox(
        "auto_sim",
        "Auto Simulate",
        default_value=not interactive,
        tooltip="Automatically re-run the simulation shortly after any input changes.\n"
        "Off by default in a UI since a bake is comparatively expensive -- watch the "
        "node's orange border (needs simulation) and press Run Simulation manually for "
        "heavy scenes.\n"
        "On by default headless, where there is no border to watch and no button to "
        "press, so a cook that did not simulate would silently bake nothing.",
    )
    auto_sim.add_callback(_on_auto_sim_toggled)
    chunk_api.add_param(auto_sim)

    chunk_api.add_param(
        chunk.Checkbox(
            "settle_to_rest",
            "Settle To Rest",
            default_value=False,
            tooltip="Bake only the final resting pose, as a default value with no time "
            "samples, instead of the whole frame range. Use it to drop props into a "
            "plausible arrangement rather than to keep the animation of them falling.\n"
            "The simulation runs on past Frames until everything has actually stopped "
            "moving, so the pose is settled rather than whatever Frames happened to end "
            "on.\n"
            "Matters for a sim-ready asset: a prim carrying PhysicsRigidBodyAPI *and* "
            "animated transforms is contradictory, and a downstream simulator ends up "
            "fighting the keyframes.",
        )
    )
    chunk_api.add_param(
        chunk.Checkbox(
            "live_settle",
            "Live Settle",
            default_value=False,
            tooltip="Settle to rest on every cook, right away, instead of waiting for Run "
            "Simulation. Drag a pose handle upstream and the robot lands where its motors, "
            "gravity and contacts let it -- sagging, or stopped by the table.\n"
            "For posing: it writes the resting pose only, no animation. Keep it to small "
            "scenes, since the whole solve runs inside each cook.",
        )
    )

    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    def _section(name: str, label: str, icon: str = "") -> chunk.Group:
        """A folded section.

        Every section starts collapsed: the solver has ~30 params, and showing them all
        meant scrolling past a wall of spinboxes to reach Run. Folding is a pure UI
        preference and is deliberately not treated as a change to the chunk.
        """
        grp = chunk.Group(name, label, icon=icon, flags=_GF)
        grp.set_state(chunk.ChunkFlags.State.COLLAPSED)
        return grp

    # ------------------------------------------------------------------
    # Collision -- what the body hits.
    col_grp = _section("Collision", "Collision", icon="zap")
    col_grp.add_param(
        chunk.TextEdit(
            "collision_path",
            "Collider Prims",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Static collision geometry — one prim path per line. Use 'Add "
            "Selected' to append, or edit/delete lines directly to remove.",
        )
    )
    add_col_btn = chunk.Button(
        "add_collision",
        "Add Selected",
        tooltip="Append the prim(s) selected in the usdview hierarchy to the list.",
    )
    add_col_btn.add_callback(add_collision)
    col_grp.add_param(add_col_btn)

    clear_col_btn = chunk.Button("clear_collision", "Clear", tooltip="Remove all collider prims.")
    clear_col_btn.add_callback(clear_collision)
    col_grp.add_param(clear_col_btn)
    col_grp.add_param(
        chunk.Menu(
            "collider_mode",
            "Collider Type",
            menu_items=["Auto", "Mesh", "Heightfield", "Box"],
            default_value="Auto",
            tooltip="How collider prims are turned into collision geometry.\n"
            "Mesh: the real surface, one thin convex slab per triangle — concave and "
            "two-sided, so a body can go inside a cup, a bottle or a drawer.\n"
            "Heightfield: a 2.5D elevation grid. Cheap for terrain and huge meshes, but it "
            "holds one height per cell, so walls become cliffs bodies stick in.\n"
            "Box: the collider's bounding box.\n"
            "Auto uses Mesh while the collider fits the triangle budget, else Heightfield.",
        )
    )
    col_grp.add_param(
        chunk.Spinbox(
            "collider_thickness",
            "Collider Thickness",
            default_value=0.0,
            tooltip="For Mesh colliders: how deep the solid material behind each triangle is, "
            "in scene units. 0 = auto (2% of the collider's size). Raise it if fast bodies "
            "tunnel through; lower it if a thin-walled object's opposite faces fight each other.\n"
            "Breakable targets: 0 = per collider, from its rbd:colliderThickness attribute if it has "
            "one (the part's real wall thickness), else half its thinnest dimension.",
        )
    )
    col_grp.add_param(
        chunk.Spinbox(
            "tri_budget",
            "Triangle Budget",
            default_value=20000,
            is_int=True,
            tooltip="Auto uses a Mesh collider while the collider is under this many "
            "triangles, and falls back to a heightfield above it.\n\n"
            "This is a time budget as much as a quality one: a Mesh collider costs one "
            "MuJoCo geom per triangle and building them is quadratic, so 5k triangles "
            "takes a few seconds and 20k takes about a minute, every run. Over 20k the "
            "readout says how long the build will take; it runs in the background and "
            "Cancel stops it.",
        )
    )
    col_grp.add_param(
        chunk.Spinbox(
            "floor_drop",
            "Floor Drop",
            default_value=0.0,
            tooltip="Lower the catch-all ground plane by this much below the collider's "
            "lowest point, so bodies that roll off the rim keep falling further.",
        )
    )
    chunk_api.add_param(col_grp)

    # ------------------------------------------------------------------
    # Physics -- what the body is made of.
    physics_grp = _section("Physics", "Physics", icon="waves")
    physics_grp.add_param(
        chunk.Menu(
            "shape_type",
            "Collision Shape",
            menu_items=["Convex Hull", "Sphere", "Box"],
            # Convex Hull is what you almost always want: it is the shape the body actually
            # looks like.
            default_value="Convex Hull",
            tooltip="Collision shape for the dynamic body: Convex Hull (accurate, matches "
            "the mesh), Sphere (fastest, most forgiving in dense piles), or Box.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "collision_margin",
            "Collision Margin",
            default_value=0.0,
            tooltip="Extra padding grown around every collider, in scene units. Helps avoid clipping.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "hfield_res",
            "Collider Resolution",
            default_value=64,
            is_int=True,
            tooltip="Grid resolution of the heightfield built from concave colliders (bowls/terrain). "
            "Higher = finer surface but heavier.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "mass",
            "Mass",
            default_value=1.0,
            tooltip="Mass of the dynamic body — a relative weight, in no particular unit "
            "(not kg or g). Nothing pins it to one: gravity is applied as an acceleration, "
            "so Mass never becomes a weight you could calibrate against. It only ever acts "
            "as the divisor in acceleration = force / Mass, against the numbers you type "
            "into Wind and Turbulence. Read it as a ratio: 1 is the reference, 10 is ten "
            "times harder to push around.\n\n"
            "So Mass shows up wherever a force other than gravity acts — Wind, Turbulence, "
            "how fast Damping bleeds off motion, and how momentum is traded in a collision. "
            "On a plain drop it looks like it does nothing, and that is correct: a heavy "
            "body and a light one fall identically.",
        )
    )
    # Friction and Bounce are physically 0..1, so they get sliders -- you can feel the range
    # instead of guessing which numbers are sensible.
    physics_grp.add_param(
        chunk.Slider(
            "friction",
            "Friction",
            min_value=0.0,
            max_value=1.0,
            default_value=0.5,
            tooltip="Surface friction: 0 = frictionless, 1 = grippy. A collider, body or robot link with a "
                    "physics material bound uses the material's friction instead.",
        )
    )
    physics_grp.add_param(
        chunk.Slider(
            "restitution",
            "Bounce",
            min_value=0.0,
            max_value=1.0,
            default_value=0.1,
            tooltip="Bounciness: 0 = no bounce, 1 = fully elastic.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "lin_damping",
            "Linear Damping",
            default_value=0.0,
            tooltip="Linear drag slowing the body's motion (0 = none). Raise it for a floaty, "
            "air-resisted feel.",
        )
    )
    physics_grp.add_param(
        chunk.Spinbox(
            "ang_damping", "Angular Damping", default_value=0.05, tooltip="Rotational drag slowing the body's spin."
        )
    )
    chunk_api.add_param(physics_grp)

    # ------------------------------------------------------------------
    # Initial State -- where it starts and how hard it is thrown.
    init_grp = _section("InitialState", "Initial State", icon="cube")
    # No position or rotation offset here any more, and no gizmo on this chunk. Transform
    # chunks compose, so an upstream Transform does that job -- and having it in two places
    # was actively harmful: with a chair wired Transform -> RBD Solver, going back to adjust
    # the Transform while the solver held its own offset put the chair back at the origin.
    init_grp.add_param(
        chunk.Vector(
            "initial_velocity",
            "Linear Velocity",
            param=chunk.Spinbox(),
            length=3,
            suffixes="XYZ",
            default_value=(0.0, 0.0, 0.0),
            tooltip="Starting linear velocity, in scene units/s — throw the body rather than dropping it.",
        )
    )
    init_grp.add_param(
        chunk.Vector(
            "angular_velocity",
            "Angular Velocity",
            param=chunk.Spinbox(),
            length=3,
            suffixes="XYZ",
            default_value=(0.0, 0.0, 0.0),
            tooltip="Starting angular velocity, in radians/s — give the body spin as it launches.",
        )
    )
    chunk_api.add_param(init_grp)

    # ------------------------------------------------------------------
    # Forces -- what pushes the body around mid-flight.
    forces_grp = _section("Forces", "Forces", icon="move")
    forces_grp.add_param(
        chunk.Vector(
            "wind",
            "Wind",
            param=chunk.Spinbox(),
            length=3,
            suffixes="XYZ",
            default_value=(0.0, 0.0, 0.0),
            tooltip="Constant directional push on every body. Applied as a force, so a "
            "heavier body shrugs it off and a lighter one gets carried -- turn Mass down to "
            "let the wind take something.",
        )
    )
    forces_grp.add_param(
        chunk.Spinbox(
            "turbulence",
            "Turbulence",
            default_value=0.0,
            tooltip="Strength of the swirling noise added on top of Wind. 0 = perfectly "
            "steady wind. Like Wind this is a force, so it fights the body's Mass; try "
            "roughly a tenth of gravity x mass to start (≈100 on a centimetre scene at "
            "mass 1).",
        )
    )
    forces_grp.add_param(
        chunk.Spinbox(
            "turb_scale",
            "Turbulence Scale",
            default_value=100.0,
            tooltip="World-space size of one gust, in scene units. Small = fine, busy chop; "
            "large = slow, broad swells that neighbouring bodies ride together.",
        )
    )
    forces_grp.add_param(
        chunk.Spinbox(
            "turb_speed",
            "Turbulence Speed",
            default_value=1.0,
            tooltip="How fast the turbulence field itself drifts and reshapes over time. "
            "0 = a frozen field the bodies simply move through.",
        )
    )
    forces_grp.add_param(
        chunk.Spinbox(
            "turb_seed",
            "Seed",
            default_value=0,
            is_int=True,
            tooltip="Changes the gust pattern without changing its strength — the knob to reach "
            "for when a take is nearly right but one body lands badly.",
        )
    )
    chunk_api.add_param(forces_grp)

    # ------------------------------------------------------------------
    # Breakage -- for a fractured PointInstancer (the Fracture chunk's output).
    brk_grp = _section("Breakage", "Breakage", icon="zap")
    brk_grp.add_param(chunk.Checkbox(
        "breakable", "Breakable", default_value=True,
        tooltip="When the target is a fractured PointInstancer, let its bodies break into their "
        "pieces under load. Has no effect on anything else.",
    ))
    brk_grp.add_param(chunk.Spinbox(
        "strength", "Strength", default_value=40.0,
        tooltip="The load that breaks a body, in multiples of its own weight. A fall onto something "
        "hard is a spike of a few tens; something trapped between two driven parts sees as much "
        "as it takes. A piece half the size needs a quarter of the load. 0 = nothing breaks.",
    ))
    brk_grp.add_param(chunk.Slider("strength_variation", "Variation", min_value=0.0, max_value=0.9,
                                   default_value=0.3, tooltip="Each piece's strength varies by ± this fraction."))
    brk_grp.add_param(chunk.Spinbox("max_generations", "Max Generations", default_value=8, is_int=True,
                                    tooltip="Stop breaking past this generation, even where the fracture goes further."))
    brk_grp.add_param(chunk.Spinbox("break_delay", "Break Delay", default_value=0.04,
                                    tooltip="Seconds a fresh piece holds before it can break again, so one blow "
                                    "cannot shatter a body to gravel within a single step."))
    brk_grp.add_param(chunk.Spinbox("break_kick", "Break Kick", default_value=10.0,
                                    tooltip="Speed the pieces spring apart with, scene units/s."))
    brk_grp.add_param(chunk.Checkbox(
        "rest_until_hit", "Rest Until Hit", default_value=False,
        tooltip="Bodies that start resting on something stay still until something hits them, and "
        "bodies that start inside each other do not push apart. For a model fractured part by part, "
        "such as a house, which would otherwise explode on the first frame.",
    ))
    chunk_api.add_param(brk_grp)


    # ------------------------------------------------------------------
    # Simulation -- how the solve itself is run. Last: set once, rarely touched.
    sim_grp = _section("Simulation", "Simulation", icon="waves")
    sim_grp.add_param(
        chunk.Spinbox(
            "start_frame", "Start Frame", default_value=1, is_int=True, tooltip="First frame of the baked animation."
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "frame_count",
            "Frame Count",
            default_value=120,
            is_int=True,
            tooltip="Number of frames to simulate and bake.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "fps", "FPS", default_value=24.0, tooltip="Frames per second — sets the simulation timestep (1/fps)."
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "substeps",
            "Substeps",
            default_value=8,
            is_int=True,
            tooltip="Physics steps per frame. Higher = more stable/accurate but slower. A physics "
                    "scene's step rate (physxScene:timeStepsPerSecond) wins.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "gravity",
            "Gravity",
            default_value=0.0,
            tooltip="Along the up axis, in scene units/s². 0 = Earth gravity in this stage's "
            "units (-981 in a centimetre scene, -9.81 in a metre one).",
        )
    )
    chunk_api.add_param(sim_grp)

    # Any real simulation input marks the chunk stale (orange node border) until the next
    # successful Run; an upstream chunk changing does the same through the cook it brings
    # (_note_input_change). A control says its cook is not an input changing.
    for p in chunk_api.get_params():
        if getattr(p, "name", None) not in _STALE_IGNORE_PARAMS:
            p.add_callback(_mark_stale, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)
        else:
            p.add_callback(_control_changed, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)
    chunk_api.set_state(chunk.ChunkFlags.State.STALE)

    return chunk_api



def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.DYNAMICS,
        icon="cube",
        description=(
            """
        Simulates rigid bodies falling and colliding (MuJoCo) and bakes them as animation.

        Bakes a MuJoCo rigid-body simulation onto each target prim — or every instance
        of a PointInstancer — as keyframed animation over the frame range. Several
        selected prims are solved together and collide with each other. Add colliders
        for objects to rest on or in (a heightfield is built for concave shapes like
        bowls). A robot carrying a box can be given a set-down goal (setDown:* on its
        prim: goal point, box, hand, joints, frames, gain): while the box is held, the
        solver shifts the joints' drive targets so it lands on the goal. Requires the
        'mujoco' package.
        """
        ),
        # Declared, not just described. Saying it in the description tells a person
        # reading the chunk list; declaring it is what lets the framework act -- name
        # the missing package when it is absent, and install it when the user has opted
        # in. Without this the chunk reported "mujoco not installed" to the console and
        # there was nothing for the host to offer.
        #
        # scipy is deliberately absent: a genuine optional, imported inside the functions
        # that want it, with a documented fallback when it is missing.
        requirements=["mujoco>=3.1,<4", "numpy>=2,<3"],
        cache_cook=True,
    )
