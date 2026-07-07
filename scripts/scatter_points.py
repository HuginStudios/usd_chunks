"""Scatter Points chunk — distributes prototype instances across a mesh.

Placement only: Surface (on faces) or Volume (fill a closed interior). Physical
settling (e.g. apples coming to rest in a bowl) is the RBD Solver's job — scatter
loosely, then settle. Spacing (Poisson-disk) keeps instances from overlapping.

Region filtering goes through a single per-point mask hook, so a future paint
gizmo can supply a weight without touching the sampler/distribution/variation code.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import bisect
import math
import random

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk

_ORIENT_MODES = ["Align to Normal", "Keep Original", "Up", "Random"]
_RAND_AXES = ["Off", "X", "Y", "Z", "All"]
_SCATTER_MODES = ["Surface", "Volume"]


def _collect_triangles(prim):
    """Collect world-space (v0, v1, v2, normal) triangles from prim + descendants."""
    if not prim or not prim.IsValid():
        return []

    triangles = []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        face_counts = mesh.GetFaceVertexCountsAttr().Get()
        face_indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not face_counts or not face_indices:
            continue

        xform_cache = UsdGeom.XformCache()
        world_xf = xform_cache.GetLocalToWorldTransform(p)
        world_pts = []
        for pt in points:
            wp = world_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
            world_pts.append(Gf.Vec3f(wp[0], wp[1], wp[2]))

        idx = 0
        for fc in face_counts:
            if fc >= 3:
                v0 = world_pts[face_indices[idx]]
                for i in range(1, fc - 1):
                    v1 = world_pts[face_indices[idx + i]]
                    v2 = world_pts[face_indices[idx + i + 1]]
                    e1 = v1 - v0
                    e2 = v2 - v0
                    n = Gf.Vec3f(
                        e1[1] * e2[2] - e1[2] * e2[1],
                        e1[2] * e2[0] - e1[0] * e2[2],
                        e1[0] * e2[1] - e1[1] * e2[0],
                    )
                    ln = Gf.GetLength(n)
                    if ln > 1e-8:
                        n = n / ln
                    triangles.append((v0, v1, v2, n))
            idx += fc

    if not triangles:
        # Fallback: flat quad on the top face of the bounding box.
        bbox_cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
        bbox_range = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if not bbox_range.IsEmpty():
            mn = bbox_range.GetMin()
            mx = bbox_range.GetMax()
            v0 = Gf.Vec3f(mn[0], mn[1], mx[2])
            v1 = Gf.Vec3f(mx[0], mn[1], mx[2])
            v2 = Gf.Vec3f(mx[0], mx[1], mx[2])
            v3 = Gf.Vec3f(mn[0], mx[1], mx[2])
            up = Gf.Vec3f(0, 0, 1)
            triangles.append((v0, v1, v2, up))
            triangles.append((v0, v2, v3, up))

    return triangles


def _slope_ok(normal, world_up, max_slope_deg):
    """True if the face is within max_slope_deg of facing world up.

    90 = every upward-facing face (the old default); 0 = only perfectly flat;
    180 = every face (e.g. wrap a sphere).
    """
    if max_slope_deg >= 180.0:
        return True
    n = Gf.Vec3f(normal[0], normal[1], normal[2])
    ln = Gf.GetLength(n)
    if ln < 1e-8:
        return False
    cos_a = Gf.Dot(n / ln, world_up)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
    return angle <= max_slope_deg + 1e-4


class _PoissonGrid:
    """Spatial hash for O(1) min-distance rejection during sampling."""

    def __init__(self, min_dist):
        self._grid: dict = {}
        self._cell = max(min_dist, 1e-6)
        self._min_sq = min_dist * min_dist

    def _key(self, pt):
        c = self._cell
        return (math.floor(pt[0] / c), math.floor(pt[1] / c), math.floor(pt[2] / c))

    def too_close(self, pt):
        if self._min_sq <= 0:
            return False
        kx, ky, kz = self._key(pt)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for dz in range(-1, 2):
                    for ex in self._grid.get((kx + dx, ky + dy, kz + dz), ()):
                        d = (pt[0] - ex[0]) ** 2 + (pt[1] - ex[1]) ** 2 + (pt[2] - ex[2]) ** 2
                        if d < self._min_sq:
                            return True
        return False

    def add(self, pt):
        self._grid.setdefault(self._key(pt), []).append(pt)


def _scatter_on_triangles(triangles, count, rng, min_dist=0, mask=None):
    """Area-weighted point sampling with min-distance rejection.

    *mask*: optional per-point hook ``mask(point, normal) -> 0..1`` — the paint
    gizmo will pass a weight function here; a candidate is kept with that
    probability. None means accept everything that passes spacing.
    """
    areas = []
    for v0, v1, v2, _ in triangles:
        e1, e2 = v1 - v0, v2 - v0
        cross = Gf.Vec3f(
            e1[1] * e2[2] - e1[2] * e2[1],
            e1[2] * e2[0] - e1[0] * e2[2],
            e1[0] * e2[1] - e1[1] * e2[0],
        )
        areas.append(0.5 * Gf.GetLength(cross))

    total_area = sum(areas)
    if total_area < 1e-10:
        return [], []

    cdf = []
    cumulative = 0.0
    for a in areas:
        cumulative += a / total_area
        cdf.append(cumulative)

    positions, normals = [], []
    grid = _PoissonGrid(min_dist)
    max_attempts = count * 100
    attempts = 0
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        tri_idx = min(bisect.bisect_left(cdf, rng.random()), len(triangles) - 1)
        v0, v1, v2, n = triangles[tri_idx]

        u, v = rng.random(), rng.random()
        if u + v > 1.0:
            u, v = 1.0 - u, 1.0 - v
        w = 1.0 - u - v
        pt = Gf.Vec3f(
            w * v0[0] + u * v1[0] + v * v2[0],
            w * v0[1] + u * v1[1] + v * v2[1],
            w * v0[2] + u * v1[2] + v * v2[2],
        )

        if grid.too_close(pt):
            continue
        if mask is not None and rng.random() > mask(pt, n):
            continue

        positions.append(pt)
        normals.append(n)
        grid.add(pt)

    return positions, normals


def _fill_in_volume(triangles, count, rng, min_dist=0, mask=None):
    """Uniformly fill the interior of a closed mesh with random points.

    Point-in-mesh is an odd/even ray cast along +X; triangles are bucketed by
    their (y, z) extent so each test only checks faces the ray can actually hit
    (was O(all triangles) per candidate — the slow path this rebuild fixes).
    """
    if not triangles:
        return [], []

    xs = [v[0] for tri in triangles for v in tri[:3]]
    ys = [v[1] for tri in triangles for v in tri[:3]]
    zs = [v[2] for tri in triangles for v in tri[:3]]
    mn = (min(xs), min(ys), min(zs))
    mx = (max(xs), max(ys), max(zs))

    ncells = 32
    cy = max((mx[1] - mn[1]) / ncells, 1e-9)
    cz = max((mx[2] - mn[2]) / ncells, 1e-9)

    def _gy(y):
        return max(0, min(ncells - 1, int((y - mn[1]) / cy)))

    def _gz(z):
        return max(0, min(ncells - 1, int((z - mn[2]) / cz)))

    yz_grid: dict = {}
    for ti, (v0, v1, v2, _) in enumerate(triangles):
        for gy in range(_gy(min(v0[1], v1[1], v2[1])), _gy(max(v0[1], v1[1], v2[1])) + 1):
            for gz in range(_gz(min(v0[2], v1[2], v2[2])), _gz(max(v0[2], v1[2], v2[2])) + 1):
                yz_grid.setdefault((gy, gz), []).append(ti)

    def _inside(pt):
        cand = yz_grid.get((_gy(pt[1]), _gz(pt[2])))
        if not cand:
            return False
        ox, oy, oz = pt[0], pt[1], pt[2]
        hits = 0
        for ti in cand:
            v0, v1, v2, _ = triangles[ti]
            e1x, e1y, e1z = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
            e2x, e2y, e2z = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
            # ray dir = (1,0,0); h = dir × e2 = (0, -e2z, e2y)
            a = e1y * -e2z + e1z * e2y
            if abs(a) < 1e-10:
                continue
            f = 1.0 / a
            sx, sy, sz = ox - v0[0], oy - v0[1], oz - v0[2]
            uu = f * (sy * -e2z + sz * e2y)
            if uu < 0.0 or uu > 1.0:
                continue
            qx = sy * e1z - sz * e1y
            qy = sz * e1x - sx * e1z
            qz = sx * e1y - sy * e1x
            vv = f * qx
            if vv < 0.0 or uu + vv > 1.0:
                continue
            t = f * (e2x * qx + e2y * qy + e2z * qz)
            if t > 1e-10:
                hits += 1
        return (hits % 2) == 1

    positions, normals = [], []
    up = Gf.Vec3f(0, 1, 0)  # caller replaces with world up
    grid = _PoissonGrid(min_dist)
    max_attempts = count * 200
    attempts = 0
    while len(positions) < count and attempts < max_attempts:
        attempts += 1
        pt = Gf.Vec3f(rng.uniform(mn[0], mx[0]), rng.uniform(mn[1], mx[1]), rng.uniform(mn[2], mx[2]))
        if not _inside(pt):
            continue
        if grid.too_close(pt):
            continue
        if mask is not None and rng.random() > mask(pt, up):
            continue
        positions.append(pt)
        normals.append(up)
        grid.add(pt)

    return positions, normals


def _normal_to_quat(normal):
    """Rotation taking +Z to the given normal direction."""
    up = Gf.Vec3f(0, 0, 1)
    n = Gf.Vec3f(normal[0], normal[1], normal[2])
    ln = Gf.GetLength(n)
    if ln < 1e-8:
        return Gf.Quath(1, 0, 0, 0)
    n = n / ln
    dot = Gf.Dot(up, n)
    if dot > 0.9999:
        return Gf.Quath(1, 0, 0, 0)
    if dot < -0.9999:
        return Gf.Quath(0, 1, 0, 0)
    axis = Gf.Cross(up, n)
    axis = axis / Gf.GetLength(axis)
    half = math.acos(max(-1.0, min(1.0, dot))) * 0.5
    s = math.sin(half)
    return Gf.Quath(math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def _quat_mul(a, b):
    aw, ax, ay, az = a.GetReal(), *a.GetImaginary()
    bw, bx, by, bz = b.GetReal(), *b.GetImaginary()
    return Gf.Quath(
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _random_quat(rng):
    """Uniform random orientation (Shoemake)."""
    u1, u2, u3 = rng.random(), rng.random(), rng.random()
    s1, s2 = math.sqrt(1 - u1), math.sqrt(u1)
    return Gf.Quath(
        s2 * math.cos(2 * math.pi * u3),
        s1 * math.sin(2 * math.pi * u2),
        s1 * math.cos(2 * math.pi * u2),
        s2 * math.sin(2 * math.pi * u3),
    )


def _compute_orientation(mode, normal, rng):
    if mode == "Align to Normal":
        return _normal_to_quat(normal)
    if mode == "Random":
        return _random_quat(rng)
    # "Keep Original" / "Up" — prototype's own orientation.
    return Gf.Quath(1, 0, 0, 0)


def _apply_random_rotation(base_quat, axis, amount_deg, rng):
    if axis == "Off":
        return base_quat
    amount_rad = math.radians(amount_deg)
    if axis == "All":
        ax = Gf.Vec3f(rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1))
        ln = Gf.GetLength(ax)
        if ln < 1e-8:
            return base_quat
        ax = ax / ln
    else:
        ax = {"X": Gf.Vec3f(1, 0, 0), "Y": Gf.Vec3f(0, 1, 0), "Z": Gf.Vec3f(0, 0, 1)}[axis]
    angle = rng.uniform(-amount_rad, amount_rad)
    half = angle * 0.5
    s = math.sin(half)
    rand_q = Gf.Quath(math.cos(half), ax[0] * s, ax[1] * s, ax[2] * s)
    return _quat_mul(rand_q, base_quat)


def _proto_metrics(proto_prim, up_idx):
    """Return (world_scale Vec3f, lateral_footprint) of the prototype.

    world_scale is baked into the instance scales (the prototype ref has its
    xformOpOrder cleared, so its own scale would otherwise be lost). footprint
    is the larger of the two non-up world dimensions — used to auto-derive a
    no-overlap spacing.
    """
    proto_scale = Gf.Vec3f(1.0, 1.0, 1.0)
    footprint = 1.0
    if proto_prim and proto_prim.IsValid():
        xc = UsdGeom.XformCache(Usd.TimeCode.Default())
        wm = xc.GetLocalToWorldTransform(proto_prim)
        r0, r1, r2 = wm.GetRow3(0), wm.GetRow3(1), wm.GetRow3(2)
        proto_scale = Gf.Vec3f(
            (r0[0] ** 2 + r1[0] ** 2 + r2[0] ** 2) ** 0.5,
            (r0[1] ** 2 + r1[1] ** 2 + r2[1] ** 2) ** 0.5,
            (r0[2] ** 2 + r1[2] ** 2 + r2[2] ** 2) ** 0.5,
        )
        bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        rng = bc.ComputeWorldBound(proto_prim).ComputeAlignedRange()
        if not rng.IsEmpty():
            size = rng.GetSize()
            lat = [j for j in range(3) if j != up_idx]
            footprint = max(float(size[lat[0]]), float(size[lat[1]]), 1e-6)
    return proto_scale, footprint


def _on_mode_changed(owner, notice, sender):
    """Region (slope) controls only apply to Surface mode — hide them in Volume."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    region_grp = chunk_obj.get_param("Region")
    if not region_grp:
        return
    if owner.value == "Volume":
        region_grp.set_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        region_grp.unset_state(chunk.ChunkFlags.State.HIDDEN)


def _auto_scatter(owner, notice, sender):
    """Re-scatter on any setting change, but only while Auto Scatter is on."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    toggle = chunk_obj.get_param("auto_scatter")
    if toggle and toggle.value:
        _run(owner, notice, sender)


def _run(owner, notice, sender):
    """Scatter prototype instances onto/into the surface — Scatter button."""
    _chunk = owner.get_chunk()
    stage = owner.get_stage()
    if not stage:
        return

    surface_prims = _chunk.get_param("surface_prim").get_prims()
    surface_prim = surface_prims[0] if surface_prims else None
    proto_prims = _chunk.get_param("proto_prim").get_prims()
    proto_prim = proto_prims[0] if proto_prims else None

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    # `.value` is already typed and defaults when unset — no cast/`or` needed. inst_path
    # keeps its fallback because an empty path (user cleared the field) is invalid here.
    inst_path = _chunk.get_param("inst_path", is_s).value or "/ScatterInstancer"
    scatter_mode = _chunk.get_param("scatter_mode", is_s).value
    max_count = max(1, _chunk.get_param("max_count", is_i).value)
    seed = _chunk.get_param("seed", is_i).value
    auto_spacing = _chunk.get_param("auto_spacing", is_b).value
    spacing = _chunk.get_param("spacing", is_f).value
    max_slope = _chunk.get_param("max_slope", is_f).value
    scale_min = _chunk.get_param("scale_min", is_f).value
    scale_max = _chunk.get_param("scale_max", is_f).value
    if scale_max < scale_min:
        scale_min, scale_max = scale_max, scale_min
    normal_offset = _chunk.get_param("normal_offset", is_f).value
    orient_mode = _chunk.get_param("orient_mode", is_s).value
    rand_axis = _chunk.get_param("rand_axis", is_s).value
    rand_amount = _chunk.get_param("rand_amount", is_f).value

    layer = owner.get_edit_context()
    if not layer:
        return
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)

    # Erase stale positions/orientations from other layers (e.g. a solver bake),
    # or their old N-item arrays win over scatter's new M-item arrays and break
    # the PointInstancer (mismatched protoIndices vs positions).
    _inst_sdf = Sdf.Path(inst_path)
    for _lyr in stage.GetLayerStack():
        if _lyr.identifier == layer.identifier:
            continue
        for _prop in ("positions", "orientations", "scales"):
            _pp = _inst_sdf.AppendProperty(_prop)
            for _t in list(_lyr.ListTimeSamplesForPath(_pp)):
                _lyr.EraseTimeSample(_pp, _t)
            if _spec := _lyr.GetAttributeAtPath(_pp):
                _spec.ClearDefaultValue()

    triangles = _collect_triangles(surface_prim)
    rng = random.Random(seed)
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    world_up = Gf.Vec3f(0, 1, 0) if up_idx == 1 else Gf.Vec3f(0, 0, 1)

    proto_scale, footprint = _proto_metrics(proto_prim, up_idx)
    if auto_spacing:
        # Centre-to-centre >= the largest instance's footprint -> no overlap.
        spacing = footprint * max(scale_max, 1e-6)

    if scatter_mode == "Volume":
        positions, normals = _fill_in_volume(triangles, max_count, rng, spacing)
        normals = [world_up] * len(positions)
    else:
        # Surface — slope pre-filters the triangle set (correct area weighting,
        # no wasted samples). The paint mask, being per-point, will instead be
        # passed as the `mask=` hook above.
        surf_tris = [t for t in triangles if _slope_ok(t[3], world_up, max_slope)]
        if surf_tris:
            positions, normals = _scatter_on_triangles(surf_tris, max_count, rng, spacing)
        else:
            # No mesh / no qualifying faces — fall back to a jittered grid.
            sp = max(spacing, 2.0)
            n = int(math.sqrt(max_count))
            off = (n - 1) * sp / 2
            positions = [
                Gf.Vec3f(
                    x * sp - off + rng.uniform(-sp * 0.3, sp * 0.3), y * sp - off + rng.uniform(-sp * 0.3, sp * 0.3), 0
                )
                for x in range(n)
                for y in range(n)
            ]
            normals = [Gf.Vec3f(0, 0, 1)] * len(positions)

    if abs(normal_offset) > 1e-9:
        positions = [
            Gf.Vec3f(p[0] + n[0] * normal_offset, p[1] + n[1] * normal_offset, p[2] + n[2] * normal_offset)
            for p, n in zip(positions, normals)
        ]

    with Usd.EditContext(stage, layer):
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)

        proto_scope = f"{inst_path}/Prototypes"
        stage.DefinePrim(proto_scope, "Scope")
        proto_path = f"{proto_scope}/proto_0"
        if proto_prim and proto_prim.IsValid():
            ref_prim = stage.DefinePrim(proto_path)
            ref_prim.GetReferences().AddInternalReference(proto_prim.GetPath())
            ref_prim.SetInstanceable(False)
            UsdGeom.Xformable(ref_prim).GetXformOpOrderAttr().Set([])
        else:
            UsdGeom.Cube.Define(stage, proto_path).GetSizeAttr().Set(1.0)

        instancer.GetPrototypesRel().SetTargets([Sdf.Path(proto_path)])

        if positions:
            instancer.GetPositionsAttr().Set(Vt.Vec3fArray(positions))
            instancer.GetProtoIndicesAttr().Set(Vt.IntArray([0] * len(positions)))

            scale_rng = random.Random(seed + 13)
            scales = Vt.Vec3fArray(len(positions))
            for i in range(len(positions)):
                s = scale_rng.uniform(scale_min, scale_max)
                scales[i] = Gf.Vec3f(s * proto_scale[0], s * proto_scale[1], s * proto_scale[2])
            instancer.GetScalesAttr().Set(scales)

            orient_rng = random.Random(seed + 7)
            orientations = Vt.QuathArray(len(positions))
            for i, nrm in enumerate(normals):
                base = _compute_orientation(orient_mode, nrm, orient_rng)
                orientations[i] = _apply_random_rotation(base, rand_axis, rand_amount, orient_rng)
            instancer.GetOrientationsAttr().Set(orientations)

    print(f"[ScatterPoints] Scattered {len(positions)} instances at {inst_path}")


def main():
    chunk_api = chunk.Chunk("Scatter Points")

    # --- Target ---
    chunk_api.add_param(
        chunk.LineEdit(
            "surface_prim",
            "Surface Mesh",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="Mesh to scatter onto (Surface) or fill (Volume).",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "proto_prim",
            "Prototype Prim",
            flags=chunk.ChunkFlags.ParamFlags.LABELED | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
            tooltip="Prim instanced at each scatter point. Empty = a default cube.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "inst_path",
            "Instancer Path",
            default_value="/ScatterInstancer",
            tooltip="USD path where the PointInstancer is created. Feed this to the RBD Solver "
            "to settle the instances.",
        )
    )

    _GF = (
        chunk.ChunkFlags.GroupFlags.COLLAPSABLE
        | chunk.ChunkFlags.GroupFlags.LABELED
        | chunk.ChunkFlags.GroupFlags.FRAMED
    )

    # --- Distribution ---
    dist_grp = chunk.Group("Distribution", flags=_GF)
    mode_param = chunk.Menu(
        "scatter_mode",
        "Mode",
        menu_items=_SCATTER_MODES,
        default_value="Surface",
        tooltip="Surface: place on faces.\nVolume: fill the interior of a closed mesh.",
    )
    mode_param.add_callback(_on_mode_changed)
    dist_grp.add_param(mode_param)
    dist_grp.add_param(
        chunk.Checkbox(
            "auto_spacing",
            "Auto Spacing",
            default_value=True,
            tooltip="Derive spacing from the prototype's footprint so instances don't overlap. "
            "Turn off to set Spacing manually.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "spacing",
            "Spacing",
            default_value=0.0,
            tooltip="Minimum distance between instances, in scene units (used when Auto Spacing is off). "
            "0 = allow overlap.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "max_count",
            "Max Count",
            default_value=200,
            is_int=True,
            tooltip="Upper limit on instances. Spacing may place fewer if the surface fills up first.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "seed",
            "Seed",
            default_value=0,
            is_int=True,
            tooltip="Random seed — change for a different pattern with the same settings.",
        )
    )
    chunk_api.add_param(dist_grp)

    # --- Region (Surface only) ---
    region_grp = chunk.Group("Region", flags=_GF)
    region_grp.add_param(
        chunk.Spinbox(
            "max_slope",
            "Max Slope",
            default_value=90.0,
            tooltip="Only scatter on faces within this many degrees of facing up.\n"
            "90 = all upward faces, lower = flatter ground only, 180 = every face.",
        )
    )
    chunk_api.add_param(region_grp)

    # --- Variation ---
    var_grp = chunk.Group("Variation", flags=_GF)
    var_grp.add_param(
        chunk.Spinbox(
            "scale_min",
            "Scale Min",
            default_value=1.0,
            tooltip="Smallest random scale applied to an instance.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "scale_max",
            "Scale Max",
            default_value=1.0,
            tooltip="Largest random scale. Set Min and Max apart for natural size variation.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "normal_offset",
            "Normal Offset",
            default_value=0.0,
            tooltip="Push instances along the surface normal. Positive lifts them off the surface "
            "(handy before an RBD settle); negative sinks them in.",
        )
    )
    var_grp.add_param(
        chunk.Menu(
            "orient_mode",
            "Orientation Mode",
            menu_items=_ORIENT_MODES,
            default_value="Align to Normal",
            tooltip="Align to Normal: follow the surface.\n"
            "Up / Keep Original: use the prototype's orientation.\n"
            "Random: fully random.",
        )
    )
    var_grp.add_param(
        chunk.Menu(
            "rand_axis",
            "Random Rotation Axis",
            menu_items=_RAND_AXES,
            default_value="Off",
            tooltip="Add a random spin around this axis on top of the base orientation. Off disables it.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "rand_amount",
            "Random Rotation (deg)",
            default_value=360.0,
            tooltip="Maximum random rotation in degrees (each instance gets a random angle between -this and +this).",
        )
    )
    chunk_api.add_param(var_grp)

    chunk_api.add_param(
        chunk.Checkbox(
            "auto_scatter",
            "Auto Scatter",
            default_value=False,
            tooltip="Re-scatter automatically whenever a setting changes — no need to press Scatter. "
            "Off by default to avoid lag on heavy meshes.",
        )
    )

    scatter_btn = chunk.Button("scatter", "Scatter", tooltip="Place the instances now.")
    scatter_btn.add_callback(_run)
    chunk_api.add_param(scatter_btn)

    # Re-run on change while Auto Scatter is on. Wire every tuning param (and the
    # toggle itself, so turning it on does an immediate scatter). inst_path is
    # left out so editing the path doesn't spawn orphan instancers per keystroke.
    for name in (
        "surface_prim",
        "proto_prim",
        "scatter_mode",
        "auto_spacing",
        "spacing",
        "max_count",
        "seed",
        "max_slope",
        "scale_min",
        "scale_max",
        "normal_offset",
        "orient_mode",
        "rand_axis",
        "rand_amount",
        "auto_scatter",
    ):
        chunk_api.get_param(name).add_callback(_auto_scatter)

    return chunk_api


def config():
    return chunk.DefaulConfig(category="Create")
