"""MuJoCo RBD Solver — rigid body simulation via MuJoCo.

Requires: pxr (OpenUSD), usd_chunks, mujoco, numpy.
Optional: scipy — smoother/denser heightfield colliders; falls back gracefully if absent.
"""

from __future__ import annotations

import math
import random
from collections.abc import Generator

from PySide6 import QtCore
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_gizmo, chunk_registry, chunk_utils
from usd_chunks.plugins import chunk_usd

try:
    import mujoco as _mj
    import numpy as _np

    _HAS_MUJOCO = True
except ImportError:
    _HAS_MUJOCO = False
    _np = None


def _force_phases(seed: int) -> tuple:
    """Six fixed phase offsets for the turbulence field, so a given Seed always gives the
    same gusts -- a re-run has to reproduce the take you just approved."""
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


def _build_heightfield(cprim: Usd.Prim, up_idx: int, resolution: int = 64, min_cell: float = 0.0) -> dict | None:
    """Sample the top surface of *cprim*'s meshes into a MuJoCo heightfield.

    A bowl/plate/terrain inner surface is single-valued in height, so a
    heightfield gives stable, smooth *concave* collision — unlike a mesh geom,
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
    """
    if _np is None:
        return None

    xc = UsdGeom.XformCache()
    pts_world = []
    tris = []
    for p in Usd.PrimRange(cprim):
        mesh = UsdGeom.Mesh(p)
        if not mesh:
            continue
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        wxf = xc.GetLocalToWorldTransform(p)
        base = len(pts_world)
        for pt in points:
            w = wxf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
            pts_world.append((w[0], w[1], w[2]))
        # Faces too, not just their corners -- see the rasterisation pass below.
        counts = mesh.GetFaceVertexCountsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        if counts and indices:
            k = 0
            for c in counts:
                if c >= 3 and k + c <= len(indices):
                    i0 = base + indices[k]
                    for t in range(1, c - 1):
                        tris.append((i0, base + indices[k + t], base + indices[k + t + 1]))
                k += c

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
    # sampled height is the slab's own UNDERSIDE, so the collider grew a several-unit-deep
    # phantom trench straight through the middle of the table. Live-reported as an apple
    # landing correctly and then sinking half-way into a dining table as it rolled: it was
    # rolling into that trench. Rasterising each triangle across the cells it actually
    # covers gives the real surface height there instead.
    #
    # Only triangles spanning more than one cell need this -- a triangle smaller than a
    # cell is already represented by its own corners above, so a dense mesh (where that is
    # nearly every triangle) skips the loop almost entirely and stays fast.
    if tris:
        TRI = _np.asarray(tris, dtype=_np.int64)
        tc = (V[TRI][:, :, a0] - min0) / (max0 - min0) * (N - 1)
        tr = (max1 - V[TRI][:, :, a1]) / (max1 - min1) * (N - 1)
        tu = V[TRI][:, :, up_idx]
        c_lo = _np.clip(_np.floor(tc.min(axis=1)).astype(int), 0, N - 1)
        c_hi = _np.clip(_np.ceil(tc.max(axis=1)).astype(int), 0, N - 1)
        r_lo = _np.clip(_np.floor(tr.min(axis=1)).astype(int), 0, N - 1)
        r_hi = _np.clip(_np.ceil(tr.max(axis=1)).astype(int), 0, N - 1)
        multi_cell = _np.nonzero((c_hi - c_lo + 1) * (r_hi - r_lo + 1) > 1)[0]
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
    spec: "_mj.MjSpec",
    body,
    cprim: Usd.Prim,
    name: str,
    thickness: float,
    friction: float,
    contact_tc: float,
    xform_inv: Gf.Matrix4d | None = None,
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
    """
    verts, tris = _collider_triangles(cprim)
    if not tris:
        return 0

    if xform_inv is not None:
        verts = [tuple(xform_inv.Transform(Gf.Vec3d(*v))) for v in verts]

    V = _np.asarray(verts, dtype=_np.float64)
    count = 0
    for i, (a, b, c) in enumerate(tris):
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


def _add_colliders(
    spec: "_mj.MjSpec",
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
) -> list[tuple[str, Usd.Prim]]:
    """Add static/kinematic collision bodies to *spec*.

    No collision prims â†’ infinite ground plane. Otherwise each prim becomes, by mode:

    * ``Mesh`` â€” one thin convex prism per triangle (see _add_triangle_soup): exact for
      any shape, concave or thin-walled, and the only option that lets a body go *inside*
      a cup or a bottle. Animated colliders get the same thing on a mocap body.
    * ``Heightfield`` â€” a 2.5D elevation grid: cheap for terrain and huge meshes, but only
      one surface height per cell, so walls become cliffs.
    * ``Box`` â€” the collider's bounding box.
    * ``Auto`` (default) â€” Mesh while the collider's triangle count fits *tri_budget*,
      else Heightfield, else Box.

    *dyn_radius* sizes the heightfield cell-size cap to the dynamic body.
    Returns mocap_col_names: list of (body_name, prim) needing per-frame updates.
    """
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

    # Catch-all ground plane at the lowest point of the colliders â€” the "table"
    # the bowl sits on. An infinite plane is a rock-solid (non-singular) contact,
    # so it reliably catches anything a collider ejects AND any body that
    # rolls/spills off the rim, instead of letting it fall forever.
    ground_up = None
    for cp in collision_prims:
        cr = _geom_bounds(cp)
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

    for i, cp in enumerate(collision_prims):
        is_anim = any(op.GetNumTimeSamples() > 1 for op in UsdGeom.Xformable(cp).GetOrderedXformOps())
        bounds = _geom_bounds(cp)

        # --- exact triangle-soup collider (the general case) ---
        soup = collider_mode in ("Auto", "Mesh")
        if soup and bounds is not None:
            _, tris = _collider_triangles(cp)
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
                    xfc = UsdGeom.XformCache(Usd.TimeCode.Default())
                    rest = xfc.GetLocalToWorldTransform(cp)
                    xform_inv = rest.GetInverse()
                    rt = rest.ExtractTranslation()
                    rq = rest.RemoveScaleShear().ExtractRotation().GetQuat()
                    rqi = rq.GetImaginary()
                    cbody.pos = [float(rt[0]), float(rt[1]), float(rt[2])]
                    cbody.quat = [rq.GetReal(), float(rqi[0]), float(rqi[1]), float(rqi[2])]
                    cbody.mocap = True
                    mocap_col_names.append((f"col_{i}", cp))
                n = _add_triangle_soup(
                    spec, cbody, cp, f"col_{i}", thickness, friction, contact_tc, xform_inv
                )
                if n:
                    continue
                soup = False

        # --- concave collision via heightfield (static, non-X-up prims only) ---
        hf = None
        if collider_mode in ("Auto", "Heightfield") and not is_anim and up_idx != 0:
            # Keep hfield cells from getting much smaller than the body, or
            # the contact at the bowl bottom goes singular and explodes.
            hf = _build_heightfield(cp, up_idx, hfield_res, min_cell=0.15 * dyn_radius)

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
            xfc = UsdGeom.XformCache()
            col_t = xfc.GetLocalToWorldTransform(cp).ExtractTranslation()
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
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    floor_drop: float = 0.0,
    wind: "tuple | list" = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
) -> Generator[int, None, list[Gf.Matrix4d] | None]:
    """MuJoCo rigid body simulation â€” yields frame index, returns all_mats.

    Uses MjSpec to build the scene programmatically from USD mesh data (no file I/O).
    Concave static colliders use heightfields (see _build_heightfield); this
    build of MuJoCo has no generic mesh-SDF collider.
    MuJoCo quaternions are [w,x,y,z] matching USD â€” no reordering needed.
    """
    # --- body frame, geometry extent ---
    # The MuJoCo body frame goes on the GEOMETRY, not on the prim's transform origin. A prim
    # from a production asset routinely has its points authored far from that origin (the
    # kitchen set's crayon: 230 units away, in shared model space), and a free joint that far
    # from the mass it carries turns every contact impulse into a huge angular one -- the body
    # is launched across the scene on first touch. Placing the frame on the geometry also
    # makes the box/sphere colliders land on the mesh instead of beside it, and gives a tight
    # box for a rotated prim (the old world-AABB half-extents grew with rotation).
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
    # results are composed against this (see the loop below), so the prim keeps its authored
    # transform structure however far its points sit from its origin.
    rest_mat = Gf.Matrix4d(1.0)
    rest_mat.SetRotate(world_rot_quat)
    rest_mat.SetTranslateOnly(body_centre)
    rest_inv = rest_mat.GetInverse()
    # The prim's world transform without scale â€” the writer re-applies the prim's own scale,
    # so carrying it here as well would square it.
    prim_world_rest = world_xf.RemoveScaleShear()

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
            print("[RBDSolver/MuJoCo] Convex Hull: no mesh data, falling back to Box")

    # --- build MjSpec scene ---
    spec = _mj.MjSpec()
    sub_dt = dt / substeps  # per-step timestep
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.noslip_iterations = 3
    # implicitfast integrates contacts/damping more stably than the default Euler.
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST

    # Contact stiffness timeconst: must be >= 2*timestep for stability. A stiff
    # 0.01 floor keeps impact penetration tiny (~0.1cm vs ~1.4cm at 0.04), so
    # fast impacts at -981 don't visibly clip. Stability under pile-ups comes
    # from the catch-all ground plane (see _add_colliders), not from softening.
    contact_tc = max(2.0 * sub_dt, 0.01)

    # --- static / kinematic collision bodies ---
    dyn_radius = max(half_extents) + max(collision_margin, 0.0)
    mocap_col_names = _add_colliders(
        spec,
        collision_prims,
        up_idx,
        friction,
        contact_tc,
        dyn_radius,
        hfield_res,
        floor_drop,
        collider_mode,
        collider_thickness,
    )

    # --- initial orientation (offset already applied above) ---
    gq = rot.GetQuat()
    gi = gq.GetImaginary()
    # MuJoCo quaternion convention matches USD: [w, x, y, z]
    init_quat_mj = [gq.GetReal(), float(gi[0]), float(gi[1]), float(gi[2])]

    # --- initial position: the body frame sits on the geometry centre, so the user's
    # translate offset moves the body from there ---
    init_pos = [float(body_centre[0]), float(body_centre[1]), float(body_centre[2])]

    lat = [j for j in range(3) if j != up_idx]

    # --- dynamic body ---
    dyn_body = spec.worldbody.add_body()
    dyn_body.name = "dynamic"
    dyn_body.pos = list(init_pos)
    dyn_body.quat = init_quat_mj

    free_jnt = dyn_body.add_joint()
    free_jnt.type = _mj.mjtJoint.mjJNT_FREE
    free_jnt.name = "free_joint"

    dyn_geom = dyn_body.add_geom()
    dyn_geom.friction = [friction, 0.005, 0.0001]
    dyn_geom.mass = mass
    # Map restitution (0-1) to solref: lower dampratio = bouncier.
    # solref[0] = timeconst (kept >= 2*timestep for stability), solref[1] = dampratio
    dyn_geom.solref = [contact_tc, 1.0 - max(0.0, min(1.0, restitution)) * 0.9]
    # solimp: [dmin, dmax, width, midpoint, power]
    dyn_geom.solimp = [0.95, 0.99, 0.001, 0.5, 2]
    # condim=3 (normal + sliding friction); condim=6's torsional/rolling
    # constraints destabilise the singular contact at a bowl's concave bottom.
    dyn_geom.condim = 3

    if shape_type == "Sphere":
        sphere_r = max(half_extents[lat[0]], half_extents[lat[1]])
        radius = max(sphere_r + collision_margin, 0.001)
        dyn_geom.type = _mj.mjtGeom.mjGEOM_SPHERE
        dyn_geom.size = [radius, 0.0, 0.0]

    elif shape_type == "Convex Hull" and mesh_pts is not None:
        hull_ma = spec.add_mesh()
        hull_ma.name = "hull_dyn"
        hull_ma.uservert = mesh_pts.flatten()
        # MuJoCo builds a convex hull internally from the vertex cloud â€”
        # maxhullvert caps the output vertex count for the collision hull.
        hull_ma.maxhullvert = max(hull_max_verts, 64) if hull_max_verts > 0 else 1024
        if collision_margin != 0.0:
            max_ext = max(half_extents) if max(half_extents) > 1e-6 else 1.0
            ms = (max_ext + collision_margin) / max_ext
            hull_ma.scale = [ms, ms, ms]
        dyn_geom.type = _mj.mjtGeom.mjGEOM_MESH
        dyn_geom.meshname = "hull_dyn"

    else:
        adj = [max(h + collision_margin, 0.001) for h in half_extents]
        dyn_geom.type = _mj.mjtGeom.mjGEOM_BOX
        dyn_geom.size = adj

    # --- compile ---
    try:
        model = spec.compile()
    except Exception as _ce:
        print(f"[RBDSolver/MuJoCo] Scene compile failed: {_ce}")
        return

    data = _mj.MjData(model)

    # Apply linear/angular damping on the freejoint DOFs directly on the compiled model.
    # DOFs 0-2 are translational, 3-5 are rotational.
    jnt_id = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_JOINT, "free_joint")
    if jnt_id >= 0:
        dof0 = model.jnt_dofadr[jnt_id]
        model.dof_damping[dof0 : dof0 + 3] = lin_damping
        model.dof_damping[dof0 + 3 : dof0 + 6] = ang_damping
        # Set initial velocity
        data.qvel[dof0 : dof0 + 3] = init_vel[:3]
        data.qvel[dof0 + 3 : dof0 + 6] = ang_vel[:3]

    bid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, "dynamic")

    # Resolve mocap body ids for kinematic collision updates
    mocap_ids = []
    for bname, cprim in mocap_col_names:
        mbid = _mj.mj_name2id(model, _mj.mjtObj.mjOBJ_BODY, bname)
        mid = int(model.body_mocapid[mbid])
        mocap_ids.append((mid, cprim))

    print(f"[RBDSolver/MuJoCo] Compiled â€” {model.nbody} bodies, {model.ngeom} geoms, shape={shape_type}")

    # Initialize forward kinematics so xpos/xquat are valid on frame 0
    _mj.mj_forward(model, data)

    # --- Debug: write collision geoms to USD session layer for visualization ---
    # --- simulation loop ---
    phases = _force_phases(turb_seed)
    sim_t = 0.0
    all_mats = []
    for frame in range(frame_count):
        # Update kinematic (mocap) collision bodies from USD time samples
        for mid, cprim in mocap_ids:
            tc = Usd.TimeCode(start_frame + frame)
            kc = UsdGeom.XformCache(tc)
            kxf = kc.GetLocalToWorldTransform(cprim)
            kt = kxf.ExtractTranslation()
            kq = kxf.RemoveScaleShear().ExtractRotation().GetQuat()  # strip scale, see note above
            ki = kq.GetImaginary()
            data.mocap_pos[mid] = [float(kt[0]), float(kt[1]), float(kt[2])]
            data.mocap_quat[mid] = [kq.GetReal(), float(ki[0]), float(ki[1]), float(ki[2])]

        # Sample state before stepping (frame N shows where the body was at the start of step N)
        pos = data.xpos[bid]
        qmj = data.xquat[bid]  # [w, x, y, z] â€” same convention as USD, no reordering needed

        body_now = Gf.Matrix4d()
        body_now.SetRotate(Gf.Quatd(float(qmj[0]), float(qmj[1]), float(qmj[2]), float(qmj[3])))
        body_now.SetTranslateOnly(Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2])))
        # The body frame sits on the geometry, not on the prim's origin, so the prim's world
        # transform is its rest pose carried by the body's rigid motion since frame 0:
        #   W(t) = W(0) . B(0)^-1 . B(t)
        # This is exact wherever the prim's origin happens to be relative to its points, and
        # collapses to plain B(t) when they coincide (which is what the old code assumed).
        world_mat = prim_world_rest * rest_inv * body_now
        all_mats.append(world_mat)

        for _ in range(substeps):
            _apply_wind(model, data, (bid,), wind, turbulence, turb_scale, turb_speed, phases, sim_t)
            _mj.mj_step(model, data)
            sim_t += sub_dt

        # Update debug collision geoms for this frame
        yield frame

    return all_mats


def _bake_scale(prim: Usd.Prim, xform_cache) -> tuple:
    """The scale the bake has to re-apply: the prim's WORLD scale, not its local one.

    The bake converts a world-space result into local space with parent_world_inv, which
    divides out every ancestor's scale -- and the simulation result is rotation and
    translation only, so nothing puts that back. Reading the LOCAL scale compensates for the
    wrong thing: a mesh under a parent scaled by 100 has a local scale of 1, so nothing was
    re-applied and the baked op came out at 0.01, exactly the 1/100 the parent contributes.
    Reported as the apple shrinking on Run Simulation.

    It only ever worked because of where it was tested. A prim at the ROOT has the same
    local and world scale, so the two coincide and the mistake is invisible -- which is
    exactly why simulating /food_apple_01_4k was fine and simulating its mesh was not.
    """
    world = xform_cache.GetLocalToWorldTransform(prim)
    return tuple(world.GetRow3(i).GetLength() for i in range(3))


def _upstream_xform(prim: Usd.Prim, op_name: str) -> Gf.Matrix4d:
    """Everything applying to `prim` before this chunk's own op.

    Read as the composed local transform with this chunk's own contribution divided back
    out, so it reflects what is actually in effect -- muting and layer strength included --
    rather than a re-composition of the op list by hand.

    Ordering, verified rather than assumed: for xformOpOrder [rot, translate] USD composes
    T * R, so the last op multiplies on the left. An appended op therefore gives
    local = mine * upstream, hence upstream = mine^-1 * local.
    """
    xformable = UsdGeom.Xformable(prim)
    local = xformable.GetLocalTransformation(Usd.TimeCode.Default())
    if isinstance(local, tuple):
        local = local[0]
    mine = Gf.Matrix4d(1.0)
    attr = prim.GetAttribute(op_name)
    if attr and attr.IsValid():
        current = attr.Get()
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
) -> None:
    """Write single-prim simulation results to USD (main-thread only)."""
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
    # This used to author the plain xformOp:transform and hard-set the order to just that.
    # Under the old scheme that was merely the usual strongest-layer-wins; under the current
    # one it is worse, because the solver sits downstream and so its one-item list outranks
    # everyone else's -- excluding an upstream Transform's op from the order entirely. The
    # op stayed on the prim, typed and valued, and was never read, so placing a chair with a
    # Transform stopped working the moment a simulation existed. Reported as the chair
    # jumping the instant the Transform was nudged.
    op_name = chunk_utils.xform_op_name(node) if node is not None else "xformOp:transform"
    with Usd.EditContext(stage, layer):
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
        # _local() gives where the body should end up in its parent's space. That used to be
        # the whole answer, because the bake replaced the prim's op stack. It composes on top
        # of the upstream ops now, so writing the absolute value in applies whatever placed
        # the body a second time -- reported as the chair jumping away the moment Run
        # Simulation was pressed, with a Transform upstream of the solver.
        #
        # Same conversion the gizmo write-back does, for the same reason: an absolute answer
        # arriving at something that contributes a share has to have the share divided out.
        upstream = _upstream_xform(prim, op_name)
        inv_upstream = upstream.GetInverse()
        xf_attr.Set(_local(all_mats[0]) * inv_upstream)
        for frame_idx, mat in enumerate(all_mats):
            xf_attr.Set(_local(mat) * inv_upstream, Usd.TimeCode(start_frame + frame_idx))
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + len(all_mats) - 1)
    print(f"[RBDSolver] Baked {len(all_mats)} frames on {prim.GetPath()}")


def simulate_mujoco(
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
    lin_damping: float = 0.0,
    ang_damping: float = 0.05,
    scale_xyz: tuple[float, float, float] | None = None,
    shape_type: str = "Convex Hull",
    collision_margin: float = 0.0,
    solver_iters: int = 30,
    hfield_res: int = 96,
    collider_mode: str = "Auto",
    collider_thickness: float = 0.0,
    tri_budget: int = 20000,
    floor_drop: float = 0.0,
    wind: "tuple | list" = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
    node=None,
) -> list[Gf.Matrix4d] | None:
    """Run MuJoCo simulation.

    `node` is the chunk itself, needed only so the bake lands in this chunk's own named
    xformOp rather than the shared one -- see _write_prim_results.
    """
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    translate = world_xf.ExtractTranslation()
    # Strip scale/shear first: ExtractRotation() on a scaled matrix returns
    # garbage (e.g. a 100x-scaled -90deg matrix decodes as a 360deg identity),
    # which would bake the wrong orientation onto the prim and desync the
    # collision shape from the visual mesh.
    rot = world_xf.RemoveScaleShear().ExtractRotation()

    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    dt = 1.0 / fps

    # Run the working MuJoCo simulation generator
    gen = _sim_prim_gen_mj(
        prim,
        collision_prims,
        frame_count,
        gravity,
        friction,
        restitution,
        mass,
        substeps,
        init_vel,
        ang_vel,
        start_frame,
        dt,
        up_idx,
        shape_type,
        collision_margin,
        solver_iters,
        translate,
        rot,
        lin_damping,
        ang_damping,
        hfield_res=hfield_res,
        floor_drop=floor_drop,
        wind=wind,
        turbulence=turbulence,
        turb_scale=turb_scale,
        turb_speed=turb_speed,
        turb_seed=turb_seed,
    )

    # Exhaust the generator to get all_mats
    all_mats = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        all_mats = e.value

    if not all_mats:
        print("[MuJoCoSolver] Simulation produced no results")
        return

    # Write results using the proven writer
    _write_prim_results(stage, prim, layer, parent_world_inv, all_mats, start_frame, scale_xyz, node)
    print(f"[MuJoCoSolver] Baked {frame_count} frames on {prim.GetPath()}")
    return all_mats


def _proto_hull_verts(stage: Usd.Stage, proto_targets, cap: int = 200) -> _np.ndarray | None:
    """Prototype mesh vertices for a shared convex-hull collider (extracted once).

    Points are gathered in the prototype's world-bound space — the same space the
    caller measures proto extent/centre in — so per-instance scaling stays
    consistent with the sphere/box path. Subsampled to *cap* points to keep the
    per-asset hull build cheap. Returns None if the prototype has no mesh data.
    """
    if _np is None or not proto_targets:
        return None
    proto_prim = stage.GetPrimAtPath(proto_targets[0])
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


def simulate_mujoco_instancer(
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
    wind: "tuple | list" = (0.0, 0.0, 0.0),
    turbulence: float = 0.0,
    turb_scale: float = 100.0,
    turb_speed: float = 1.0,
    turb_seed: int = 0,
) -> None:
    """Run MuJoCo multi-body simulation for a UsdGeom.PointInstancer using MjSpec.

    Each instance gets a free joint and collides with the other instances and the
    same collider stack as the single-prim path (concave heightfield + backstop
    for a bowl, else a ground plane). *shape_type* picks the per-instance collider:
    "Sphere" (fast, most stable in dense piles), "Box", or "Convex Hull" (accurate,
    heavier â€” the shared prototype hull is built once and reused per distinct
    scale). A sphere is rotationally symmetric, so the rotation offset and each
    instance's own orientation only influence the physics for Box / Convex Hull.
    """
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
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    world_xf = xform_cache.GetLocalToWorldTransform(prim)
    world_inv = world_xf.GetInverse()
    # Instancer orientations are in instancer-local space; convert to/from world
    # with the instancer's own rotation (scale stripped â€” see note in simulate_mujoco).
    inst_rot = world_xf.RemoveScaleShear().ExtractRotation()
    inst_rot_inv = inst_rot.GetInverse()

    # Prototype's UNSCALED bound (extents + centre). The scatter tool clears the
    # prototype's transform and bakes the real scale into per-instance scales, so
    # the collider must multiply the proto extent by each instance's scale â€”
    # otherwise the spheres are ~100x too small and the instances clip straight
    # through everything.
    proto_targets = instancer.GetPrototypesRel().GetTargets()
    proto_ext = Gf.Vec3d(1.0, 1.0, 1.0)
    proto_ctr = Gf.Vec3d(0.0, 0.0, 0.0)
    if proto_targets:
        proto_prim = stage.GetPrimAtPath(proto_targets[0])
        if proto_prim:
            bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
            # Local, NOT world: the instancer's world transform (e.g. a Transform node feeding it)
            # is already applied to the body positions via world_xf. Using ComputeWorldBound here
            # would double-count it in the collider centre/extent â€” offsetting and rescaling the
            # collision geometry away from the visible instance. Identical to the old result when
            # the instancer is untransformed.
            rng = bbox_cache.ComputeLocalBound(proto_prim).ComputeAlignedRange()
            if not rng.IsEmpty():
                proto_ext = rng.GetMax() - rng.GetMin()
                proto_ctr = (rng.GetMin() + rng.GetMax()) * 0.5

    scales = instancer.GetScalesAttr().Get()

    def _instance_scale(idx: int) -> tuple[float, float, float]:
        if scales and idx < len(scales):
            s = scales[idx]
            return float(s[0]), float(s[1]), float(s[2])
        return 1.0, 1.0, 1.0

    def _instance_radius(idx: int) -> float:
        sx, sy, sz = _instance_scale(idx)
        return (
            max(
                float(proto_ext[0]) * sx,
                float(proto_ext[1]) * sy,
                float(proto_ext[2]) * sz,
            )
            / 2.0
        )

    # Representative radius (largest instance) for the heightfield cell-size cap.
    max_radius = max([0.5] + [_instance_radius(i) for i in range(num_instances)]) + max(collision_margin, 0.0)

    dt = 1.0 / fps

    # --- Build MjSpec ---
    spec = _mj.MjSpec()
    # Many piled bodies generate many contacts/constraints; the default solver
    # arena (~16 MB) overflows. Scale the allocation with the instance count.
    spec.memory = max(64 * 1024 * 1024, num_instances * 2 * 1024 * 1024)
    sub_dt = dt / substeps
    spec.option.timestep = sub_dt
    grav = [0.0, 0.0, 0.0]
    grav[up_idx] = gravity
    spec.option.gravity = grav
    spec.option.iterations = solver_iters
    spec.option.noslip_iterations = 3
    spec.option.integrator = _mj.mjtIntegrator.mjINT_IMPLICITFAST
    contact_tc = max(2.0 * sub_dt, 0.01)

    # Same colliders as the single-prim path (bowl heightfield + backstop, etc.)
    mocap_col_names = _add_colliders(
        spec,
        collision_prims,
        up_idx,
        friction,
        contact_tc,
        max_radius,
        hfield_res,
        floor_drop,
        collider_mode,
        collider_thickness,
        tri_budget,
    )

    dyn_dampratio = 1.0 - max(0.0, min(1.0, restitution)) * 0.9

    off_rot = None  # no rotation offset on this chunk any more; see main()

    # Convex hull: extract the shared prototype geometry once, then build one mesh
    # asset per distinct instance scale (MuJoCo computes the hull per asset).
    hull_base = None
    if shape_type == "Convex Hull":
        hull_base = _proto_hull_verts(stage, proto_targets)
        if hull_base is None:
            print("[MuJoCoSolver] Convex Hull: no prototype mesh data, falling back to Sphere")
        else:
            # Recentre on the prototype's geometry centre: the body frame sits there, so the
            # hull has to be expressed about it rather than about the prototype's origin.
            hull_base = hull_base - _np.asarray(
                [float(proto_ctr[0]), float(proto_ctr[1]), float(proto_ctr[2])], dtype=_np.float32
            )

    hull_mesh_cache: dict[tuple[float, float, float], str] = {}

    def _hull_mesh_for_scale(sx: float, sy: float, sz: float) -> str:
        key = (round(sx, 5), round(sy, 5), round(sz, 5))
        name = hull_mesh_cache.get(key)
        if name is not None:
            return name
        name = f"hull{len(hull_mesh_cache)}"
        ma = spec.add_mesh()
        ma.name = name
        ma.uservert = hull_base.flatten()
        msx, msy, msz = sx, sy, sz
        if collision_margin != 0.0:
            # Grow the hull outward by ~collision_margin via a uniform scale bump
            # (same approximation the single-prim path uses).
            max_ext = max(float(proto_ext[0]) * sx, float(proto_ext[1]) * sy, float(proto_ext[2]) * sz) / 2.0
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

        sx, sy, sz = _instance_scale(i)
        radius_i = max(_instance_radius(i) + collision_margin, 1e-4)
        # Offset from the instance's own origin to its geometry centre, scaled. The body
        # frame goes on the geometry (see the single-prim path): a prototype whose points are
        # authored away from its origin -- anything referenced out of a production asset --
        # otherwise gives every instance a long lever arm between its free joint and its mass,
        # and the pile detonates on first contact.
        ctr_scaled = Gf.Vec3d(
            float(proto_ctr[0]) * sx,
            float(proto_ctr[1]) * sy,
            float(proto_ctr[2]) * sz,
        )
        ctr_world = world_rot.TransformDir(ctr_scaled)

        body = spec.worldbody.add_body()
        body.name = f"b{i}"
        body.pos = [px + float(ctr_world[0]), py + float(ctr_world[1]), pz + float(ctr_world[2])]
        body.quat = [wq.GetReal(), float(wqi[0]), float(wqi[1]), float(wqi[2])]
        jnt = body.add_joint()
        jnt.type = _mj.mjtJoint.mjJNT_FREE
        g = body.add_geom()
        if hull_base is not None:
            # Shared prototype hull, recentred on the geometry, scaled per instance.
            g.type = _mj.mjtGeom.mjGEOM_MESH
            g.meshname = _hull_mesh_for_scale(sx, sy, sz)
        elif shape_type == "Box":
            g.type = _mj.mjtGeom.mjGEOM_BOX
            g.size = [
                max(float(proto_ext[0]) * sx / 2.0 + collision_margin, 1e-4),
                max(float(proto_ext[1]) * sy / 2.0 + collision_margin, 1e-4),
                max(float(proto_ext[2]) * sz / 2.0 + collision_margin, 1e-4),
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
        print(f"[MuJoCoSolver] Instancer compile failed: {e}")
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
    for frame in range(frame_count):
        # Update kinematic (mocap) collision bodies from USD time samples
        for mid, cprim in mocap_ids:
            tc = Usd.TimeCode(start_frame + frame)
            kc = UsdGeom.XformCache(tc)
            kxf = kc.GetLocalToWorldTransform(cprim)
            kt = kxf.ExtractTranslation()
            kq = kxf.RemoveScaleShear().ExtractRotation().GetQuat()
            ki = kq.GetImaginary()
            data.mocap_pos[mid] = [float(kt[0]), float(kt[1]), float(kt[2])]
            data.mocap_quat[mid] = [kq.GetReal(), float(ki[0]), float(ki[1]), float(ki[2])]

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
            sx, sy, sz = _instance_scale(i)
            ctr_now = body_rot.TransformDir(
                Gf.Vec3d(float(proto_ctr[0]) * sx, float(proto_ctr[1]) * sy, float(proto_ctr[2]) * sz)
            )
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

        all_positions.append(frame_pos)
        all_orientations.append(frame_ori)

        for _ in range(substeps):
            _apply_wind(model, data, bids, wind, turbulence, turb_scale, turb_speed, phases, sim_t)
            _mj.mj_step(model, data)
            sim_t += sub_dt

    # Write time samples
    with Usd.EditContext(stage, layer):
        for frame_idx in range(frame_count):
            tc = Usd.TimeCode(start_frame + frame_idx)
            positions_attr.Set(all_positions[frame_idx], tc)
            orientations_attr.Set(all_orientations[frame_idx], tc)

    with Usd.EditContext(stage, stage.GetSessionLayer()):
        stage.SetStartTimeCode(start_frame)
        stage.SetEndTimeCode(start_frame + frame_count - 1)

    print(f"[MuJoCoSolver] Baked {frame_count} frames on PointInstancer {prim.GetPath()} ({num_instances} bodies)")


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
    layer = owner.get_edit_context()
    if layer and (lreg := chunk.get_layer_manager()):
        lreg.clear_layer(layer.identifier)


def _promote_bake_layer(layer: Sdf.Layer | None) -> None:
    """Make the bake layer the strongest chunk layer for its target prim.

    Chunk layers are ordered so the earlier-added sibling wins. When the solver
    animates a prim that another chunk authored (e.g. a PointInstancer created by
    ScatterPoints), that source chunk's static values would otherwise override the
    baked time samples. Move the bake layer — and its parent graph layer — to the
    front of their containers so the simulation wins.
    """
    lreg = chunk.get_layer_manager()
    if not lreg or not layer:
        return
    try:
        # Each graph layer's own parent is whatever real layer it's currently sublayered
        # under (session layer by default, or an Edit-Target-chosen layer) — build the
        # hierarchy from iter_chunk_layers() rather than a single fixed root, since a
        # chunk's graph layer no longer always attaches to the same place.
        hierarchy: dict = {}
        for parent, sub in lreg.iter_chunk_layers():
            hierarchy[sub.identifier] = parent
            if sub.identifier != layer.identifier:
                continue
            # Strongest sibling within its parent graph layer.
            if sub.identifier in parent.subLayerPaths and parent.subLayerPaths[0] != sub.identifier:
                parent.subLayerPaths.remove(sub.identifier)
                parent.subLayerPaths.insert(0, sub.identifier)
            # Strongest graph layer under its own parent.
            grandparent = hierarchy.get(parent.identifier)
            if (
                grandparent
                and parent.identifier in grandparent.subLayerPaths
                and grandparent.subLayerPaths[0] != parent.identifier
            ):
                grandparent.subLayerPaths.remove(parent.identifier)
                grandparent.subLayerPaths.insert(0, parent.identifier)
            break
    except Exception as e:
        print(f"[MuJoCoSolver] Could not promote bake layer: {e}")


def reset(owner, notice, sender) -> None:
    _clear_chunk_bake(owner)
    owner.get_chunk().set_state(chunk.ChunkFlags.State.STALE)
    print("[MuJoCoSolver] Reset — bake cleared.")


def add_collision(owner, notice, sender) -> None:
    """Append the viewport-selected prim(s) to the collision list (comma-joined
    so get_prims() picks them all up)."""
    param = owner.get_chunk().get_param("collision_path")
    scene = chunk_registry.ChunkRegistry().scene
    if not scene:
        return
    selected = scene.get_selected_prims()
    if not selected:
        print("[MuJoCoSolver] No prim selected in viewport")
        return
    existing = [p.strip() for p in (param.value or "").replace(",", "\n").split("\n") if p.strip()]
    added = 0
    for prim in selected:
        path = str(prim.GetPath())
        if path and path != "/" and path not in existing:
            existing.append(path)
            added += 1
    param.value = "\n".join(existing)
    print(f"[MuJoCoSolver] Added {added} collision prim(s); {len(existing)} total")


def clear_collision(owner, notice, sender) -> None:
    owner.get_chunk().get_param("collision_path").value = ""
    print("[MuJoCoSolver] Cleared collision prims")


def _run(owner, notice, sender) -> None:
    if not _HAS_MUJOCO:
        print("[MuJoCoSolver] mujoco not installed. Run: pip install mujoco")
        return

    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    prim_paths = _chunk.get_param("prim_path").resolve_paths()
    if not prim_paths:
        print("[MuJoCoSolver] No target prim selected")
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
    gravity = _chunk.get_param("gravity", is_f).value
    substeps = max(1, _chunk.get_param("substeps", is_i).value)
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
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # The target prim comes from the REAL stage, not a scratch one: simulate_mujoco[_instancer]
    # write baked results back through the same instancer/attribute objects they read `prim`
    # from (positions_attr.Set(...) inside a `with Usd.EditContext(stage, layer)`), and
    # Usd.EditContext only governs the stage object it's given -- it has no effect on attributes
    # bound to a *different* Usd.Stage. A scratch-stage-bound prim here silently bakes the whole
    # simulation onto the throwaway stage instead of `layer`, discarded the moment this function
    # returns (found the hard way: "Run Simulation" printed a successful bake, but the layer
    # stayed empty). Collision prims are read-only -- never written back through -- so they're
    # still safely scoped through get_upstream_stage: a downstream chunk's edit to a collision
    # prim can't leak into the simulation, without risking the same silent-write-loss bug.
    prim = stage.GetPrimAtPath(prim_paths[0])
    if not prim.IsValid() or prim.GetPath() == Sdf.Path("/"):
        return
    scratch_stage = chunk_usd.get_upstream_stage(stage, owner.get_scene(), _chunk)
    collision_prims = _parse_collision_prims(collision_path_value, scratch_stage)

    if UsdGeom.PointInstancer(prim).GetPositionsAttr().HasValue():
        simulate_mujoco_instancer(
            stage,
            prim,
            layer,
            collision_prims,
            frame_count,
            fps,
            gravity,
            mass,
            substeps,
            init_vel,
            ang_vel,
            start_frame,
            friction=friction,
            restitution=restitution,
            lin_damping=lin_damping,
            ang_damping=ang_damping,
            shape_type=shape_type,
            collision_margin=_chunk.get_param("collision_margin", is_f).value,
            solver_iters=30,
            hfield_res=_chunk.get_param("hfield_res", is_i).value,
            collider_mode=_chunk.get_param("collider_mode", is_s).value,
            collider_thickness=_chunk.get_param("collider_thickness", is_f).value,
            tri_budget=_chunk.get_param("tri_budget", is_i).value,
            floor_drop=floor_drop,
            **forces,
        )
    else:
        xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        parent = prim.GetParent()
        parent_world_inv = Gf.Matrix4d(1.0)
        if parent and parent.IsValid() and parent.GetPath() != Sdf.Path("/"):
            parent_world_inv = xform_cache.GetLocalToWorldTransform(parent).GetInverse()

        scale_xyz = _bake_scale(prim, xform_cache)

        simulate_mujoco(
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
            collision_prims=collision_prims,
            friction=friction,
            restitution=restitution,
            lin_damping=lin_damping,
            ang_damping=ang_damping,
            scale_xyz=scale_xyz,
            shape_type=shape_type,
            collision_margin=_chunk.get_param("collision_margin", is_f).value,
            solver_iters=30,
            hfield_res=_chunk.get_param("hfield_res", is_i).value,
            collider_mode=_chunk.get_param("collider_mode", is_s).value,
            collider_thickness=_chunk.get_param("collider_thickness", is_f).value,
            tri_budget=_chunk.get_param("tri_budget", is_i).value,
            floor_drop=floor_drop,
            node=_chunk,
            **forces,
        )

    # Ensure the baked animation overrides any chunk (e.g. ScatterPoints) that authored
    # the target prim, otherwise its static values win and playback shows nothing.
    _promote_bake_layer(layer)

    try:
        scene = chunk_registry.ChunkRegistry().scene
        if scene:
            scene.refresh_timeline(start_frame, start_frame + frame_count - 1)
    except Exception:
        pass

    _chunk.unset_state(chunk.ChunkFlags.State.STALE)


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


def _on_any_chunk_changed(owner, notice, sender) -> None:
    """Global CHUNK_CHANGED listener -- catches an upstream chunk (e.g. a Transform feeding
    the collider or target prim) changing, even though it's wired via a live-drawn graph
    connection rather than DagNode.add_dependency(). Real graph wiring (_author_connection in
    chunk_qt_nodegraph.py) only authors the USD inputs:chunk/inputs:* connection -- it never
    calls add_dependency, so ChunkNoticeAlias.INPUT_CHANGED (which is purely a DagNode concept)
    never fires between two chunks connected that way. get_exec_upstream_chunks walks the same
    real USD connections recook_for_view and get_upstream_stage already trust, so membership in
    it is the correct, already-battle-tested definition of "is this chunk really upstream of me".
    """
    _chunk = owner.get_chunk()
    if sender is None or sender is _chunk:
        return
    scene = chunk_registry.ChunkRegistry().scene
    stage = _chunk.get_stage()
    if not scene or not stage:
        return
    if sender not in chunk_usd.get_exec_upstream_chunks(_chunk, stage, scene):
        return
    if _only_presentation_changed(sender):
        return
    _chunk.set_state(chunk.ChunkFlags.State.STALE)
    if _chunk.get_param("auto_sim", chunk.is_param_t(bool)).value:
        _schedule_auto_run(_chunk)


def _only_presentation_changed(sender) -> bool:
    """Whether an upstream chunk changed in a way nothing computes from.

    The listener above is deliberately broad -- it exists because graph wiring never calls
    add_dependency, so INPUT_CHANGED never fires between two chunks wired that way. The cost of
    that breadth is that it also hears the changes `propagate_dirty` already decided were
    presentation only, and re-bakes a simulation for each one.

    Reported live: with Auto Simulate on and a Scatter Brush upstream, touching the brush re-ran
    MuJoCo repeatedly. A watch on propagate_dirty showed *every* propagation routed as
    presentation and not one as a data change -- so nothing was dirtying the chain at all, and
    the solver was re-baking off this notice instead.

    Asked of the framework rather than answered here, so the DISABLED carve-out keeps working:
    disabling a chunk is a state change, but it decides whether that chunk cooks, and is
    therefore a real simulation input. DagNode.only_presentation_changed reads back the
    verdict chunk_scheduler recorded when the change was propagated; this used to re-ask each
    sender's is_presentation_change(), which re-reads an old_value/value pair that has since
    moved on.

    Only meaningful *inside* the notice. `_changes` is cleared in a `finally` once the whole
    solve finishes (chunk_scheduler.solve), and the notices fire during it -- so this reads a
    populated record here and an empty one anywhere else. An earlier attempt was reverted for
    exactly that reason: its test asked afterwards, saw nothing, and could not tell the fix
    from its absence.
    """
    try:
        # Nothing recorded reads as False, i.e. assume it mattered: a missed re-run is a
        # wrong simulation, an extra one is only slow.
        return sender.get_dag_node().only_presentation_changed()
    except Exception:  # noqa: BLE001 - never let this stop a legitimate re-run
        return False


def _schedule_auto_run(root_chunk) -> None:
    """Debounce auto-runs: a sim bake is comparatively expensive, so wait for input changes to
    settle for _AUTO_RUN_DELAY_MS before re-running, rather than baking on every keystroke/drag
    tick while a param is being edited."""
    timer = getattr(root_chunk, "_auto_run_timer", None)
    if timer is None:
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: _run(root_chunk, None, None))
        root_chunk._auto_run_timer = timer
    timer.start(_AUTO_RUN_DELAY_MS)


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
            tooltip="The prim to simulate — a single mesh, or a PointInstancer to simulate every instance.",
        )
    )

    # One Run button, at the top. There used to be a second identical one at the bottom of
    # the node purely so it stayed reachable past the param list; the sections below are
    # collapsed now, so the list is short enough not to need it.
    run_btn = chunk.Button(
        "run_sim", "Run Simulation", tooltip="Simulate and bake the animation onto the target prim."
    )
    run_btn.add_callback(_run)
    chunk_api.add_param(run_btn)

    reset_btn = chunk.Button("reset_sim", "Reset", tooltip="Clear the baked animation from this chunk's layer.")
    reset_btn.add_callback(reset)
    chunk_api.add_param(reset_btn)

    auto_sim = chunk.Checkbox(
        "auto_sim",
        "Auto Simulate",
        default_value=False,
        tooltip="Automatically re-run the simulation shortly after any input changes. Off by "
        "default since a bake is comparatively expensive -- watch the node's orange border "
        "(needs simulation) and press Run Simulation manually for heavy scenes.",
    )
    auto_sim.add_callback(_on_auto_sim_toggled)
    chunk_api.add_param(auto_sim)

    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    def _section(name: str, label: str) -> chunk.Group:
        """A folded section.

        Every section starts collapsed: the solver has ~30 params, and showing them all
        meant scrolling past a wall of spinboxes to reach Run. Folding is a pure UI
        preference and is deliberately not treated as a change to the chunk.
        """
        grp = chunk.Group(name, label, flags=_GF)
        grp.set_state(chunk.ChunkFlags.State.COLLAPSED)
        return grp

    # ------------------------------------------------------------------
    # Collision -- what the body hits.
    col_grp = _section("Collision", "Collision")
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
            "tunnel through; lower it if a thin-walled object's opposite faces fight each other.",
        )
    )
    col_grp.add_param(
        chunk.Spinbox(
            "tri_budget",
            "Triangle Budget",
            default_value=20000,
            is_int=True,
            tooltip="Auto uses a Mesh collider while the collider is under this many "
            "triangles, and falls back to a heightfield above it.",
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
    physics_grp = _section("Physics", "Physics")
    physics_grp.add_param(
        chunk.Menu(
            "shape_type",
            "Collision Shape",
            menu_items=["Convex Hull", "Sphere", "Box"],
            # Convex Hull is what you almost always want: it is the shape the body actually
            # looks like. Sphere used to be the default because it was the only one that
            # stayed put on a heightfield collider -- which turned out to be a defect in how
            # those colliders were built (a phantom trench through low-poly geometry), not a
            # property of the hull.
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
            tooltip="Surface friction: 0 = frictionless, 1 = grippy.",
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
    # Initial State -- where it starts and how hard it is thrown. Replaces the old Offsets
    # group (six loose spinboxes) and Velocity group (two text fields you had to type
    # space-separated numbers into).
    init_grp = _section("InitialState", "Initial State")
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
    forces_grp = _section("Forces", "Forces")
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
    # Simulation -- how the solve itself is run. Last: set once, rarely touched.
    sim_grp = _section("Simulation", "Simulation")
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
            tooltip="Physics steps per frame. Higher = more stable/accurate but slower.",
        )
    )
    sim_grp.add_param(
        chunk.Spinbox(
            "gravity",
            "Gravity",
            default_value=-981.0,
            tooltip="Gravity along the scene's up axis, in scene units/s². Default -981 assumes centimetres.",
        )
    )
    chunk_api.add_param(sim_grp)

    # Any real simulation input marks the chunk stale (orange node border) until the next
    # successful Run; an upstream chunk changing does the same via the global listener above.
    for p in chunk_api.get_params():
        if getattr(p, "name", None) not in _STALE_IGNORE_PARAMS:
            p.add_callback(_mark_stale, notice_alias=chunk.ChunkNoticeAlias.PARAM_CHANGED, send_on_create=False)
    chunk_api.add_callback(
        _on_any_chunk_changed,
        notice_alias=chunk.ChunkNoticeAlias.CHUNK_CHANGED,
        listen_globally=True,
        send_on_create=False,
    )
    chunk_api.set_state(chunk.ChunkFlags.State.STALE)

    return chunk_api



def config() -> chunk.DefaulConfig:
    return chunk.DefaulConfig(
        category="Simulation",
        description=(
            "Bakes a MuJoCo rigid-body simulation onto the target prim — or every instance\n"
            "of a PointInstancer — as keyframed animation over the frame range. Add colliders\n"
            "for objects to rest on or in (a heightfield is built for concave shapes like\n"
            "bowls). Requires the 'mujoco' package."
        ),
    )
