"""Scatter Points chunk — distributes prototype instances across a mesh.

Three placement modes:

* **Surface** — area-weighted sampling on the faces, Poisson-disk spacing to avoid overlap.
* **Volume** — uniform fill of a closed interior.
* **Stack** — a regular hex/grid lattice packed against the target, every instance resting
  exactly on the surface it lands on and optionally tiered into levels. This is the mode for
  "a cup of crayons standing up, tight but not intersecting": the pitch comes from the
  prototype's own oriented bounding box, so tight *is* collision-free by construction.

Prototypes are a list, each row carrying its own share of the mix (a percentage, normalised
across the rows), a scale multiplier, and optionally a variant selection written straight
into the path — ``/asset/crayon{color=red}``. ``{color=*}`` expands to one prototype per
variant in that set, which is the cheap way to scatter a whole colour range from one row.

Physical settling (apples coming to rest in a bowl) is still the RBD Solver's job — scatter
loosely, then settle. Stack mode is the deterministic alternative for when you want a tidy,
packed result with no simulation at all.

Region filtering goes through a single per-point mask hook, so a future paint gizmo can
supply a weight without touching the sampler/distribution/variation code.

Requires: pxr (OpenUSD), usd_chunks.
"""

from __future__ import annotations

import bisect
import logging
import math
import random
import re
from dataclasses import dataclass, field

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils
from usd_chunks.plugins import chunk_usd

logger = logging.getLogger(__name__)

_ORIENT_MODES = [
    "Align to Normal",
    "Keep Original",
    "Up",
    "Random",
    "Long Axis Up",
    "Long Axis to Normal",
]
_RAND_AXES = ["Off", "X", "Y", "Z", "All"]
_SCATTER_MODES = ["Surface", "Volume", "Stack"]
_PACK_PATTERNS = ["Hex", "Grid"]
_STACK_ORIENTS = ["Stand Up", "Lay Along Rows", "Keep Original"]

# /asset/crayon{color=red} — the variant value may be "*" (every variant in the set), which
# chunk_utils.split_variant_path's \w+ deliberately doesn't accept, hence the local regex.
_VARIANT_RE = re.compile(r"\{(\w+)=([\w*]+)\}")

_MAX_PROTOTYPES = 64  # a wildcard row on a big variant set shouldn't author 500 prototypes


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


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


def _triangle_bounds(triangles):
    """Axis-aligned (min, max) Vec3f of a triangle soup, or None when empty."""
    if not triangles:
        return None
    xs = [v[0] for tri in triangles for v in tri[:3]]
    ys = [v[1] for tri in triangles for v in tri[:3]]
    zs = [v[2] for tri in triangles for v in tri[:3]]
    return Gf.Vec3f(min(xs), min(ys), min(zs)), Gf.Vec3f(max(xs), max(ys), max(zs))


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


def _ray_triangle(orig, direction, v0, v1, v2):
    """Möller–Trumbore. Returns the ray parameter t of the front hit, or None."""
    e1 = v1 - v0
    e2 = v2 - v0
    h = Gf.Cross(direction, e2)
    a = Gf.Dot(e1, h)
    if abs(a) < 1e-12:
        return None
    f = 1.0 / a
    s = orig - v0
    u = f * Gf.Dot(s, h)
    if u < -1e-6 or u > 1.0 + 1e-6:
        return None
    q = Gf.Cross(s, e1)
    v = f * Gf.Dot(direction, q)
    if v < -1e-6 or u + v > 1.0 + 1e-6:
        return None
    t = f * Gf.Dot(e2, q)
    return t if t > 1e-9 else None


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


class _SurfaceRayGrid:
    """Lateral bucket grid over a triangle soup, for downward ray casts.

    Stack mode needs "where does the surface sit under this lattice point" thousands of
    times; bucketing the triangles by their extent in the two non-up axes keeps each cast
    to the handful of faces the ray can actually hit.
    """

    def __init__(self, triangles, up_idx, cells=64):
        self._triangles = triangles
        self._up_idx = up_idx
        self._axes = [i for i in range(3) if i != up_idx]
        bounds = _triangle_bounds(triangles)
        self._grid: dict = {}
        if not bounds:
            self._min = Gf.Vec3f(0, 0, 0)
            self._max = Gf.Vec3f(0, 0, 0)
            self._cell = (1.0, 1.0)
            return
        self._min, self._max = bounds
        a, b = self._axes
        self._cells = cells
        self._cell = (
            max((self._max[a] - self._min[a]) / cells, 1e-9),
            max((self._max[b] - self._min[b]) / cells, 1e-9),
        )
        for ti, tri in enumerate(triangles):
            va, vb = self._axes
            lo_a = min(tri[0][va], tri[1][va], tri[2][va])
            hi_a = max(tri[0][va], tri[1][va], tri[2][va])
            lo_b = min(tri[0][vb], tri[1][vb], tri[2][vb])
            hi_b = max(tri[0][vb], tri[1][vb], tri[2][vb])
            for ca in range(self._key(lo_a, 0), self._key(hi_a, 0) + 1):
                for cb in range(self._key(lo_b, 1), self._key(hi_b, 1) + 1):
                    self._grid.setdefault((ca, cb), []).append(ti)

    def _key(self, value, axis):
        a = self._axes[axis]
        idx = int((value - self._min[a]) / self._cell[axis])
        return max(0, min(getattr(self, "_cells", 1) - 1, idx))

    def hit(self, lateral_a, lateral_b, up_dir):
        """Highest (point, normal) under the given lateral position, or None."""
        cand = self._grid.get((self._key(lateral_a, 0), self._key(lateral_b, 1)))
        if not cand:
            return None
        a, b = self._axes
        orig = [0.0, 0.0, 0.0]
        orig[a] = lateral_a
        orig[b] = lateral_b
        # Start clear of the mesh and cast down; the first surface met from above is the
        # one an instance should rest on (a bowl's inner floor, not its underside).
        orig[self._up_idx] = self._max[self._up_idx] + max(1.0, self._max[self._up_idx] - self._min[self._up_idx])
        origin = Gf.Vec3f(*orig)
        direction = -up_dir
        best_t = None
        best_normal = None
        for ti in cand:
            v0, v1, v2, n = self._triangles[ti]
            t = _ray_triangle(origin, direction, v0, v1, v2)
            if t is None:
                continue
            if best_t is None or t < best_t:
                best_t = t
                best_normal = n
        if best_t is None:
            return None
        return origin + direction * best_t, best_normal


#: Per-vertex float, 0..1, saying how much scatter a part of the surface wants. Painted by
#: the brush; ordinary USD, so a DCC's own paint tool or a bake can author it just as well.
DENSITY_PRIMVAR = "scatterDensity"

#: How finely the lookup grid divides the surface's bounds. The grid only has to be fine
#: enough that a cell holds few vertices, since a query answers with the nearest one in it --
#: past that it costs memory for nothing.
_LOOKUP_DIVISIONS = 48


#: What only means something when scatter is choosing the positions itself. Wire painted
#: points in and every one of them is answering a question that has already been settled.
_SAMPLING_ONLY = ("auto_spacing", "spacing", "max_count", "seed", "max_slope", "scatter_mode")


def _update_sampling_params(owner) -> None:
    """Hide the distribution controls while the positions are being handed to us.

    Reported live: with Painted Points connected, Auto Spacing, Spacing, Max Count and Seed
    all still sat there doing nothing. A control that cannot affect the result is worse than
    an absent one -- it invites you to reach for it when something looks wrong.
    """
    placed = bool(owner.get_param("points_prim") and owner.get_param("points_prim").value)
    for name in _SAMPLING_ONLY:
        param = owner.get_param(name)
        if param is None:
            continue
        if placed:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)


def _on_points_input_changed(owner, notice, sender) -> None:
    _update_sampling_params(owner.get_chunk())


def _painted_points(stage, points_path):
    """Points a brush placed by hand, with the surface normal recorded at each.

    Empty whenever nothing is wired, which is the common case and costs one path lookup.

    The normals are why this returns a pair. A scatter has the triangle it sampled and can
    take the facing from that; a placed point has no triangle, so the brush records the
    surface's facing when the dab lands -- the same job Houdini's N does on a point.
    """
    if not points_path or stage is None:
        return [], []
    prim = stage.GetPrimAtPath(points_path)
    if not prim or not prim.IsValid():
        return [], []

    cloud = UsdGeom.Points(prim)
    points = cloud.GetPointsAttr().Get()
    if not points:
        return [], []
    normals = cloud.GetNormalsAttr().Get()

    up = Gf.Vec3f(0, 0, 1)
    positions = [Gf.Vec3f(*p) for p in points]
    facing = [Gf.Vec3f(*n) for n in normals] if normals else []
    # Points placed before normals were recorded still work; they simply lie flat.
    facing += [up] * (len(positions) - len(facing))
    return positions, facing[:len(positions)]


def _density_mask(prim, primvar_name=None):
    """A weight function over the painted surface, or None when nothing is painted.

    `primvar_name` is which painted field to read, and it comes off this chunk's Density Field
    input -- normally wired from a Scatter Brush's output rather than typed. Empty means *no
    field*, and an unconnected scatter therefore behaves exactly as it did before painting
    existed. That is the point of the wire: the dependency is visible, and a scatter nobody
    connected a brush to is not quietly governed by one.

    None rather than a function returning 1.0, deliberately: the sampler skips the hook
    entirely when there is no mask, so an unpainted scatter costs exactly what it did before
    this existed. That matters -- the hook runs per candidate point, and candidates outnumber
    instances by whatever the rejection rate happens to be.

    Sampling is nearest-vertex. The weight is authored per vertex, and the sampler hands over
    a point with no idea which triangle produced it, so there is nothing to interpolate
    barycentrically against. Density resolution is therefore mesh resolution: paint a
    twelve-triangle floor and the edge of the painted region is blocky. Worth knowing before
    it is discovered.
    """
    primvar_name = primvar_name or ""
    if not prim or not prim.IsValid() or not primvar_name:
        return None

    points, weights = [], []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        mesh_points = mesh.GetPointsAttr().Get()
        if not mesh_points:
            continue
        primvar = UsdGeom.PrimvarsAPI(p).GetPrimvar(primvar_name)
        if not primvar or not primvar.HasValue():
            continue

        values = primvar.ComputeFlattened()
        if values is None:
            continue
        # Constant interpolation is one number for the whole mesh -- a legitimate way to dial
        # a surface down without painting it, and it has no per-vertex values to index.
        if len(values) == 1:
            values = [values[0]] * len(mesh_points)
        if len(values) < len(mesh_points):
            logger.warning(
                "%s has %d %s values for %d points; ignoring it",
                p.GetPath(), len(values), primvar_name, len(mesh_points),
            )
            continue

        world_xf = UsdGeom.XformCache().GetLocalToWorldTransform(p)
        for i, pt in enumerate(mesh_points):
            wp = world_xf.Transform(Gf.Vec3d(pt[0], pt[1], pt[2]))
            points.append(Gf.Vec3f(wp[0], wp[1], wp[2]))
            weights.append(float(values[i]))

    if not points:
        return None
    return _NearestWeight(points, weights).sample


class _NearestWeight:
    """Nearest-vertex weight lookup over a uniform grid.

    Brute force over every vertex would be O(V) per candidate point, and the sampler makes up
    to a hundred candidates per instance -- on a real mesh that is the whole cook. The grid
    makes it O(1) at the cost of one pass at build time.
    """

    def __init__(self, points, weights):
        self._weights = weights
        lo = [min(p[i] for p in points) for i in range(3)]
        hi = [max(p[i] for p in points) for i in range(3)]
        span = max(max(hi[i] - lo[i] for i in range(3)), 1e-6)
        self._origin = lo
        self._cell = span / _LOOKUP_DIVISIONS

        self._cells = {}
        for index, pt in enumerate(points):
            self._cells.setdefault(self._key(pt), []).append((pt, index))

    def _key(self, pt):
        return tuple(int((pt[i] - self._origin[i]) // self._cell) for i in range(3))

    def sample(self, point, normal=None):
        """The painted weight nearest `point`, in 0..1. `normal` is unused but is part of the
        hook's signature -- a future falloff-by-facing would want it."""
        key = self._key(point)
        best, best_dist = None, None
        # The point can land nearer a vertex in an adjacent cell than one in its own, and an
        # empty cell is ordinary on a coarse mesh, so the neighbours are always searched.
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for candidate, index in self._cells.get(
                        (key[0] + dx, key[1] + dy, key[2] + dz), ()
                    ):
                        offset = candidate - point
                        dist = offset[0] * offset[0] + offset[1] * offset[1] + offset[2] * offset[2]
                        if best_dist is None or dist < best_dist:
                            best, best_dist = index, dist
        if best is None:
            # Nothing painted anywhere near: outside the painted region entirely.
            return 0.0
        return min(1.0, max(0.0, self._weights[best]))


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


# ---------------------------------------------------------------------------
# rotation helpers — internal math in Quatf, converted to Quath only on write
# ---------------------------------------------------------------------------


def _align_quat(from_dir, to_dir):
    """Shortest-arc rotation taking from_dir to to_dir."""
    a = Gf.Vec3f(from_dir[0], from_dir[1], from_dir[2])
    b = Gf.Vec3f(to_dir[0], to_dir[1], to_dir[2])
    la, lb = Gf.GetLength(a), Gf.GetLength(b)
    if la < 1e-8 or lb < 1e-8:
        return Gf.Quatf(1, 0, 0, 0)
    a, b = a / la, b / lb
    dot = Gf.Dot(a, b)
    if dot > 0.9999:
        return Gf.Quatf(1, 0, 0, 0)
    if dot < -0.9999:
        # Antiparallel: any perpendicular axis is a valid 180° turn.
        axis = Gf.Cross(a, Gf.Vec3f(1, 0, 0))
        if Gf.GetLength(axis) < 1e-6:
            axis = Gf.Cross(a, Gf.Vec3f(0, 1, 0))
        axis = axis / Gf.GetLength(axis)
        return Gf.Quatf(0, axis[0], axis[1], axis[2])
    axis = Gf.Cross(a, b)
    axis = axis / Gf.GetLength(axis)
    half = math.acos(max(-1.0, min(1.0, dot))) * 0.5
    s = math.sin(half)
    return Gf.Quatf(math.cos(half), axis[0] * s, axis[1] * s, axis[2] * s)


def _normal_to_quat(normal):
    """Rotation taking +Z to the given normal direction."""
    return _align_quat(Gf.Vec3f(0, 0, 1), normal)


def _axis_quat(axis, angle_rad):
    a = Gf.Vec3f(axis[0], axis[1], axis[2])
    ln = Gf.GetLength(a)
    if ln < 1e-8:
        return Gf.Quatf(1, 0, 0, 0)
    a = a / ln
    half = angle_rad * 0.5
    s = math.sin(half)
    return Gf.Quatf(math.cos(half), a[0] * s, a[1] * s, a[2] * s)


def _quat_mul(a, b):
    aw, ax, ay, az = a.GetReal(), *a.GetImaginary()
    bw, bx, by, bz = b.GetReal(), *b.GetImaginary()
    return Gf.Quatf(
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _rotate_vec(quat, vec):
    """Rotate vec by quat: v + 2w(u×v) + 2u×(u×v), u = imaginary part."""
    w = quat.GetReal()
    im = quat.GetImaginary()
    u = Gf.Vec3f(im[0], im[1], im[2])
    v = Gf.Vec3f(vec[0], vec[1], vec[2])
    uv = Gf.Cross(u, v)
    uuv = Gf.Cross(u, uv)
    return v + uv * (2.0 * w) + uuv * 2.0


def _to_quath(quat):
    im = quat.GetImaginary()
    return Gf.Quath(quat.GetReal(), im[0], im[1], im[2])


def _random_quat(rng):
    """Uniform random orientation (Shoemake)."""
    u1, u2, u3 = rng.random(), rng.random(), rng.random()
    s1, s2 = math.sqrt(1 - u1), math.sqrt(u1)
    return Gf.Quatf(
        s2 * math.cos(2 * math.pi * u3),
        s1 * math.sin(2 * math.pi * u2),
        s1 * math.cos(2 * math.pi * u2),
        s2 * math.sin(2 * math.pi * u3),
    )


def _compute_orientation(mode, normal, rng, proto=None, world_up=None):
    """Base orientation for one instance, before random spin/tilt."""
    if mode == "Align to Normal":
        return _normal_to_quat(normal)
    if mode == "Random":
        return _random_quat(rng)
    if mode == "Long Axis Up" and proto is not None:
        return _align_quat(proto.long_axis_dir(), world_up or Gf.Vec3f(0, 0, 1))
    if mode == "Long Axis to Normal" and proto is not None:
        return _align_quat(proto.long_axis_dir(), normal)
    # "Keep Original" / "Up" — prototype's own orientation.
    return Gf.Quatf(1, 0, 0, 0)


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
    return _quat_mul(_axis_quat(ax, angle), base_quat)


# ---------------------------------------------------------------------------
# prototypes
# ---------------------------------------------------------------------------


@dataclass
class _Proto:
    """One resolved prototype: where it came from, how much of the mix it takes, how big it is.

    ``bmin``/``bmax`` are the *untransformed* local bounds — the prototype reference has its
    xformOpOrder cleared, so that is what the instancer actually instances — and ``scale`` is
    the source prim's world scale, which the instancer re-applies per instance. Effective
    world size is therefore ``(bmax - bmin) * scale * instance_scale``.
    """

    src_path: str = ""
    variants: tuple = ()
    weight: float = 100.0
    scale_mult: float = 1.0
    label: str = ""
    path: str = ""
    bmin: Gf.Vec3f = field(default_factory=lambda: Gf.Vec3f(-0.5, -0.5, -0.5))
    bmax: Gf.Vec3f = field(default_factory=lambda: Gf.Vec3f(0.5, 0.5, 0.5))
    scale: Gf.Vec3f = field(default_factory=lambda: Gf.Vec3f(1, 1, 1))

    def dims(self, uniform_scale=1.0):
        s = uniform_scale
        return Gf.Vec3f(
            abs(self.bmax[0] - self.bmin[0]) * self.scale[0] * s,
            abs(self.bmax[1] - self.bmin[1]) * self.scale[1] * s,
            abs(self.bmax[2] - self.bmin[2]) * self.scale[2] * s,
        )

    def long_axis(self):
        d = self.dims()
        return max(range(3), key=lambda i: d[i])

    def long_axis_dir(self):
        v = [0.0, 0.0, 0.0]
        v[self.long_axis()] = 1.0
        return Gf.Vec3f(*v)

    def corners(self, uniform_scale=1.0):
        lo = [self.bmin[i] * self.scale[i] * uniform_scale for i in range(3)]
        hi = [self.bmax[i] * self.scale[i] * uniform_scale for i in range(3)]
        return [
            Gf.Vec3f(x, y, z)
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]


def _extent_along(corners, quat, axis):
    """(lo, hi) of the rotated corner cloud projected on axis."""
    lo = hi = None
    for c in corners:
        d = Gf.Dot(_rotate_vec(quat, c), axis)
        lo = d if lo is None or d < lo else lo
        hi = d if hi is None or d > hi else hi
    return (lo or 0.0), (hi or 0.0)


def _lateral_size(corners, quat, axis_a, axis_b):
    lo_a, hi_a = _extent_along(corners, quat, axis_a)
    lo_b, hi_b = _extent_along(corners, quat, axis_b)
    return hi_a - lo_a, hi_b - lo_b


def _parse_proto_spec(raw):
    """Split "/path/crayon{color=red}" into ("/path/crayon", (("color", "red"),))."""
    selections = tuple(_VARIANT_RE.findall(raw))
    return _VARIANT_RE.sub("", raw).strip(), selections


def _resolve_prototypes(node, scratch_stage):
    """Turn the Prototypes list rows into _Proto entries.

    Handles, per row: glob expansion (one prototype per matched prim), an explicit variant
    selection, and ``{set=*}`` wildcards (one prototype per variant in the set, the row's
    weight split evenly between them).
    """
    rows = node.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return []
    path_t = rows.get_param("proto_path")
    weight_t = rows.get_param("proto_weight")
    scale_t = rows.get_param("proto_scale")

    protos: list[_Proto] = []
    for i in range(min(rows.size, len(rows.get_my_params()))):
        try:
            raw = rows.get_indexed_param(path_t, i).value or ""
            weight = float(rows.get_indexed_param(weight_t, i).value or 0.0)
            scale_mult = float(rows.get_indexed_param(scale_t, i).value or 1.0)
        except (IndexError, ValueError, AttributeError):
            continue
        if not raw.strip() or weight <= 0.0:
            continue

        for token in re.split(r"[\s,]+", raw.strip()):
            if not token:
                continue
            base, selections = _parse_proto_spec(token)
            if not base:
                continue
            matched = chunk_utils.resolve_prim_paths(scratch_stage, base) if scratch_stage else [base]
            for path in matched:
                for expanded in _expand_variants(scratch_stage, path, selections):
                    protos.append(
                        _Proto(
                            src_path=path,
                            variants=expanded,
                            weight=weight,
                            scale_mult=scale_mult,
                            label=_proto_label(path, expanded),
                        )
                    )
    if len(protos) > _MAX_PROTOTYPES:
        del protos[_MAX_PROTOTYPES:]
    # A wildcard row splits its weight across the variants it expanded to, so one row is one
    # share of the mix no matter how many variants it turns out to cover.
    per_source: dict[str, int] = {}
    for p in protos:
        per_source[p.src_path] = per_source.get(p.src_path, 0) + 1
    for p in protos:
        p.weight /= per_source[p.src_path]
    return protos


def _expand_variants(stage, prim_path, selections):
    """Yield one selection tuple per concrete combination, expanding any "*" values."""
    combos = [()]
    prim = stage.GetPrimAtPath(prim_path) if stage else None
    for vset_name, value in selections:
        if value != "*":
            combos = [c + ((vset_name, value),) for c in combos]
            continue
        names = []
        if prim and prim.IsValid():
            vset = prim.GetVariantSets().GetVariantSet(vset_name)
            if vset:
                names = list(vset.GetVariantNames())
        if not names:
            # Unknown set (or a prim that isn't there yet) — drop the wildcard rather than
            # authoring a bogus selection.
            continue
        combos = [c + ((vset_name, n),) for c in combos for n in names]
    return combos


def _proto_label(path, selections):
    name = path.rsplit("/", 1)[-1] or path
    if selections:
        name += "".join(f"{{{s}={v}}}" for s, v in selections)
    return name


def _author_prototypes(stage, inst_path, protos, scratch_stage):
    """Author one prototype prim per _Proto under <inst_path>/Prototypes.

    Each is an internal reference to the source prim with its xformOpOrder cleared (the
    instancer supplies the transform) plus any variant selection for that entry — the same
    asset can therefore appear several times, each row selecting a different variant.

    Returns the prototypes that actually resolved; an empty result means the caller should
    fall back to the default cube.
    """
    proto_scope = f"{inst_path}/Prototypes"
    stage.DefinePrim(proto_scope, "Scope")

    resolved: list[_Proto] = []
    for i, proto in enumerate(protos):
        src = scratch_stage.GetPrimAtPath(proto.src_path) if scratch_stage else None
        if not src or not src.IsValid():
            continue
        path = f"{proto_scope}/proto_{len(resolved)}"
        ref_prim = stage.DefinePrim(path)
        ref_prim.GetReferences().AddInternalReference(Sdf.Path(proto.src_path))
        ref_prim.SetInstanceable(False)
        # src can be valid on the upstream composition yet its real source (a sublayer, a
        # reference chunk) may have vanished from the live stage between the lookup and now
        # -- the internal reference then resolves to nothing and ref_prim has no composed
        # type, so drop the entry instead of authoring onto a typeless prim.
        if not ref_prim.GetTypeName():
            ref_prim.GetReferences().ClearReferences()
            continue
        for vset_name, variant in proto.variants:
            vsets = ref_prim.GetVariantSets()
            if not vsets.HasVariantSet(vset_name):
                continue
            # A local selection on the referencing prim outranks whatever the asset itself
            # selects, which is exactly how one asset yields several coloured prototypes.
            vsets.GetVariantSet(vset_name).SetVariantSelection(variant)
        UsdGeom.Xformable(ref_prim).GetXformOpOrderAttr().Set([])

        proto.path = path
        _measure_proto(proto, ref_prim, src)
        resolved.append(proto)

    if not resolved:
        path = f"{proto_scope}/proto_0"
        cube = UsdGeom.Cube.Define(stage, path)
        cube.GetSizeAttr().Set(1.0)
        fallback = _Proto(label="cube (default)", path=path)
        resolved.append(fallback)
    return resolved


def _measure_proto(proto, proto_prim, src_prim):
    """Fill in proto.bmin/bmax (local, variant-aware) and proto.scale (source world scale)."""
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    rng = bbox_cache.ComputeUntransformedBound(proto_prim).ComputeAlignedRange()
    if not rng.IsEmpty():
        mn, mx = rng.GetMin(), rng.GetMax()
        proto.bmin = Gf.Vec3f(mn[0], mn[1], mn[2])
        proto.bmax = Gf.Vec3f(mx[0], mx[1], mx[2])

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    wm = xform_cache.GetLocalToWorldTransform(src_prim)
    r0, r1, r2 = wm.GetRow3(0), wm.GetRow3(1), wm.GetRow3(2)
    proto.scale = Gf.Vec3f(
        (r0[0] ** 2 + r1[0] ** 2 + r2[0] ** 2) ** 0.5,
        (r0[1] ** 2 + r1[1] ** 2 + r2[1] ** 2) ** 0.5,
        (r0[2] ** 2 + r1[2] ** 2 + r2[2] ** 2) ** 0.5,
    )


def _quota(weights, total):
    """Largest-remainder apportionment: exact per-prototype counts, not a statistical draw.

    30% of 10 instances is 3 of them, every cook, which is what a "percent of the mix"
    control has to mean to be usable.
    """
    if total <= 0 or not weights:
        return [0] * len(weights)
    sum_w = sum(weights)
    if sum_w <= 0:
        base = [total // len(weights)] * len(weights)
        for i in range(total - sum(base)):
            base[i] += 1
        return base
    exact = [w / sum_w * total for w in weights]
    counts = [int(math.floor(e)) for e in exact]
    remainder = total - sum(counts)
    order = sorted(range(len(weights)), key=lambda i: exact[i] - counts[i], reverse=True)
    for i in range(remainder):
        counts[order[i % len(order)]] += 1
    return counts


# ---------------------------------------------------------------------------
# stack (lattice) placement
# ---------------------------------------------------------------------------


def _stack_sites(triangles, up_idx, world_up, cfg, rng):
    """Lattice sites packed over the target, level by level.

    Returns [(point, normal, level)] in fill order — a whole level before the next one
    starts, so a Max Count that doesn't cover the lattice gives a complete single layer
    rather than a lopsided tower.
    """
    bounds = _triangle_bounds(triangles)
    if not bounds:
        return []
    bmin, bmax = bounds
    axis_a, axis_b = [i for i in range(3) if i != up_idx]
    pitch_a = max(cfg["pitch_a"], 1e-4)
    pitch_b = max(cfg["pitch_b"], 1e-4)
    # Hex rows sit in each other's grooves: √3/2 row spacing with a half-pitch offset is the
    # densest arrangement of equal circles, ~15% more instances than a square grid.
    hexagonal = cfg["pattern"] == "Hex"
    row_step = pitch_b * (0.8660254 if hexagonal else 1.0)

    inset = cfg["inset"]
    lo_a, hi_a = bmin[axis_a] + inset, bmax[axis_a] - inset
    lo_b, hi_b = bmin[axis_b] + inset, bmax[axis_b] - inset
    if hi_a < lo_a or hi_b < lo_b:
        return []

    span_a, span_b = hi_a - lo_a, hi_b - lo_b
    cols = max(1, int(span_a / pitch_a) + 1)
    rows = max(1, int(span_b / row_step) + 1)
    centre_a = (lo_a + hi_a) * 0.5
    centre_b = (lo_b + hi_b) * 0.5
    start_a = centre_a - (cols - 1) * pitch_a * 0.5
    start_b = centre_b - (rows - 1) * row_step * 0.5

    grid = _SurfaceRayGrid(triangles, up_idx) if cfg["conform"] else None
    top = bmax[up_idx]

    # Jitter is clamped to the slack between pitch and footprint, so "natural" never means
    # "intersecting".
    jit_a = max(0.0, pitch_a - cfg["footprint_a"]) * 0.5 * cfg["jitter"]
    jit_b = max(0.0, pitch_b - cfg["footprint_b"]) * 0.5 * cfg["jitter"]

    base_sites = []
    for row in range(rows):
        for col in range(cols):
            a = start_a + col * pitch_a
            b = start_b + row * row_step
            if hexagonal and row % 2:
                a += pitch_a * 0.5
                if a > hi_a:
                    continue
            if jit_a:
                a += rng.uniform(-jit_a, jit_a)
            if jit_b:
                b += rng.uniform(-jit_b, jit_b)
            if not (lo_a - 1e-6 <= a <= hi_a + 1e-6 and lo_b - 1e-6 <= b <= hi_b + 1e-6):
                continue
            if grid is not None:
                hit = grid.hit(a, b, world_up)
                if hit is None:
                    continue  # no surface under this lattice point: respect the silhouette
                base_sites.append((hit[0], hit[1]))
            else:
                pt = [0.0, 0.0, 0.0]
                pt[axis_a] = a
                pt[axis_b] = b
                pt[up_idx] = top
                base_sites.append((Gf.Vec3f(*pt), world_up))

    sites = []
    stagger = cfg["stagger"]
    for level in range(max(1, cfg["levels"])):
        for point, normal in base_sites:
            p = point
            if level and stagger:
                # Offset alternate tiers by half a pitch so they nest instead of balancing
                # one directly on top of the other.
                off = [0.0, 0.0, 0.0]
                off[axis_a] = pitch_a * 0.5 * (level % 2)
                off[axis_b] = row_step * 0.5 * (level % 2)
                p = p + Gf.Vec3f(*off)
            sites.append((p, normal, level))
    return sites


# ---------------------------------------------------------------------------
# ui reactions
# ---------------------------------------------------------------------------


def _set_hidden(obj, hidden):
    if obj is None:
        return
    if hidden:
        obj.set_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        obj.unset_state(chunk.ChunkFlags.State.HIDDEN)


def _on_mode_changed(owner, notice, sender):
    """Only show the controls that mean something for the current mode."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    mode = owner.value
    _set_hidden(chunk_obj.get_param("Region"), mode != "Surface")
    _set_hidden(chunk_obj.get_param("Stacking"), mode != "Stack")

    dist = chunk_obj.get_param("Distribution")
    if dist:
        # Stack derives its own pitch from the prototype bounds; Poisson spacing plays no
        # part in it.
        _set_hidden(dist.get_param("auto_spacing"), mode == "Stack")
        _set_hidden(dist.get_param("spacing"), mode == "Stack")
    var = chunk_obj.get_param("Variation")
    if var:
        _set_hidden(var.get_param("orient_mode"), mode == "Stack")
        _set_hidden(var.get_param("sit_on_surface"), mode == "Stack")

    # Last word to the painted-points rule. The two systems overlap on auto_spacing/spacing,
    # and this one had been unhiding them: outside Stack they mean something *to the sampler*,
    # which is true right up until the positions are handed to us and there is no sampler.
    # "Wired to a brush" is the stronger statement, so it is re-applied after the mode's.
    _update_sampling_params(chunk_obj)


def _on_stack_orient_changed(owner, notice, sender):
    """Lay Along Rows is the only stack orientation with a row direction to choose."""
    chunk_obj = owner.get_chunk()
    if not chunk_obj:
        return
    grp = chunk_obj.get_param("Stacking")
    _set_hidden(grp.get_param("stack_row_axis") if grp else None, owner.value != "Lay Along Rows")


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _migrate_legacy_proto(node):
    """Move a pre-list ``proto_prim`` value into row 0 of the Prototypes list.

    Scenes saved before the list existed carry inputs:proto_prim. Migrating on the first
    cook means an old file opens with its prototype visible in the new UI instead of
    silently scattering default cubes.
    """
    legacy = node.get_param("proto_prim")
    if legacy is None or not (legacy.value or "").strip():
        return
    rows = node.get_param("prototypes", chunk.is_inst(chunk.ListGroup))
    if rows is None:
        return
    if rows.size < 1:
        rows.size = 1
    first = rows.get_indexed_param(rows.get_param("proto_path"), 0)
    if first is not None and not (first.value or "").strip():
        first.value = legacy.value
    legacy.value = ""


def execute(node):
    """Scatter prototype instances onto/into/over the surface.

    Reactive: runs automatically whenever a param or an upstream dependency (e.g. a
    Transform feeding surface_prim) changes. Being reactive keeps things correct across an
    ordered DAG solve pass triggered by an *upstream* change (a chunk wired after this one is
    never part of that pass yet) -- but a plain edit made directly on this chunk's own params
    never goes through that ordered pass at all, so the surface and prototype prims are
    resolved through get_upstream_stage(): a downstream chunk's edit to the same prim (e.g. a
    Transform wired after Scatter, re-offsetting the same mesh) can't leak into this read
    regardless of what triggered this execute().
    """
    stage = node.get_stage()
    if not stage:
        return

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    surface_paths = node.get_param("surface_prim").resolve_paths()

    # `.value` is already typed and defaults when unset — no cast/`or` needed. inst_path
    # keeps its fallback because an empty path (user cleared the field) is invalid here.
    inst_path = node.get_param("inst_path", is_s).value or "/ScatterInstancer"
    scatter_mode = node.get_param("scatter_mode", is_s).value
    max_count = max(1, node.get_param("max_count", is_i).value)
    seed = node.get_param("seed", is_i).value
    auto_spacing = node.get_param("auto_spacing", is_b).value
    spacing = node.get_param("spacing", is_f).value
    max_slope = node.get_param("max_slope", is_f).value
    scale_min = node.get_param("scale_min", is_f).value
    scale_max = node.get_param("scale_max", is_f).value
    if scale_max < scale_min:
        scale_min, scale_max = scale_max, scale_min
    normal_offset = node.get_param("normal_offset", is_f).value
    orient_mode = node.get_param("orient_mode", is_s).value
    sit_on_surface = node.get_param("sit_on_surface", is_b).value
    rand_axis = node.get_param("rand_axis", is_s).value
    rand_amount = node.get_param("rand_amount", is_f).value

    stack_orient = node.get_param("stack_orient", is_s).value
    stack_row_axis = node.get_param("stack_row_axis", is_s).value
    pack_pattern = node.get_param("pack_pattern", is_s).value
    pack_padding = node.get_param("pack_padding", is_f).value
    pack_inset = node.get_param("pack_inset", is_f).value
    pack_levels = max(1, node.get_param("pack_levels", is_i).value)
    pack_level_gap = node.get_param("pack_level_gap", is_f).value
    pack_stagger = node.get_param("pack_stagger", is_b).value
    pack_conform = node.get_param("pack_conform", is_b).value
    pack_jitter = node.get_param("pack_jitter", is_f).value
    pack_tilt = node.get_param("pack_tilt", is_f).value

    layer = node.get_edit_context()
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

    scratch_stage = chunk_usd.get_upstream_stage(stage, node.get_scene(), node)
    surface_prim = scratch_stage.GetPrimAtPath(surface_paths[0]) if surface_paths else None

    _migrate_legacy_proto(node)
    specs = _resolve_prototypes(node, scratch_stage)

    triangles = _collect_triangles(surface_prim)
    # None whenever the surface carries no painted weight, so an unpainted scatter never pays
    # for the lookup -- see _density_mask.
    density = _density_mask(surface_prim, node.get_param("density_primvar").value)
    rng = random.Random(seed)
    up_idx = 1 if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else 2
    world_up = Gf.Vec3f(0, 1, 0) if up_idx == 1 else Gf.Vec3f(0, 0, 1)
    lat_a, lat_b = [i for i in range(3) if i != up_idx]
    axis_vec = [Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0), Gf.Vec3f(0, 0, 1)]

    # Read here, used at the dispatch below. Nothing about a painted cloud depends on the
    # prototypes, so it does not need to wait for them.
    points_input = node.get_param("points_prim").value
    placed, placed_normals = _painted_points(scratch_stage, points_input)

    with Usd.EditContext(stage, layer):
        instancer = UsdGeom.PointInstancer.Define(stage, inst_path)
        protos = _author_prototypes(stage, inst_path, specs, scratch_stage)
        instancer.GetPrototypesRel().SetTargets([Sdf.Path(p.path) for p in protos])

        spins = rand_axis != "Off" and abs(rand_amount) > 1e-6

        # Base orientation per prototype, when the mode gives one that doesn't depend on the
        # point (everything except Align to Normal / Random / Long Axis to Normal). The pitch
        # and the resting offset are both derived from it.
        row_idx = "XYZ".find(stack_row_axis)
        row_dir = axis_vec[row_idx if row_idx in (lat_a, lat_b) else lat_a]
        base_quats = []
        for proto in protos:
            if scatter_mode == "Stack":
                if stack_orient == "Stand Up":
                    q = _align_quat(proto.long_axis_dir(), world_up)
                elif stack_orient == "Lay Along Rows":
                    q = _align_quat(proto.long_axis_dir(), row_dir)
                else:
                    q = Gf.Quatf(1, 0, 0, 0)
            else:
                q = _compute_orientation(orient_mode, world_up, random.Random(seed), proto, world_up)
            base_quats.append(q)

        # Footprint: the widest any prototype gets, laterally, at its largest scale. Under a
        # random spin the safe figure is the diagonal of that footprint, since the instance
        # can end up turned any way within the lattice cell.
        foot_a = foot_b = 0.0
        height = 0.0
        for proto, q in zip(protos, base_quats):
            corners = proto.corners(scale_max * proto.scale_mult)
            w, d = _lateral_size(corners, q, axis_vec[lat_a], axis_vec[lat_b])
            if spins or orient_mode == "Random":
                diag = math.hypot(w, d)
                if orient_mode == "Random":
                    lo_u, hi_u = _extent_along(corners, q, world_up)
                    diag = math.sqrt(w * w + d * d + (hi_u - lo_u) ** 2)
                w = d = diag
            foot_a = max(foot_a, w)
            foot_b = max(foot_b, d)
            lo_u, hi_u = _extent_along(corners, q, world_up)
            height = max(height, hi_u - lo_u)

        if auto_spacing and scatter_mode != "Stack":
            # Centre-to-centre >= the largest instance's footprint -> no overlap.
            spacing = max(foot_a, foot_b, 1e-6)

        if scatter_mode != "Stack" and points_input:
            # Hand-placed: the user said where. No sampling, no spacing, no slope filter --
            # those answer "where should these go", which is the question already settled
            # by placing them. The density field is ignored for the same reason.
            #
            # Empty is an answer too: a brush wired up but not yet painted means "nowhere",
            # not "anywhere". The *wire* decides, not whether the brush has points yet.
            #
            # Stack is checked first and so ignores a painted cloud entirely: packing a
            # lattice against the surface and placing points by hand are contradictory
            # answers to the same question, and the lattice is what the mode exists for.
            positions, normals = placed, placed_normals
            levels = [0] * len(positions)
        elif scatter_mode == "Stack":
            cfg = {
                "pitch_a": foot_a + pack_padding,
                "pitch_b": foot_b + pack_padding,
                "footprint_a": foot_a,
                "footprint_b": foot_b,
                "pattern": pack_pattern,
                "inset": pack_inset,
                "conform": pack_conform,
                "levels": pack_levels,
                "stagger": pack_stagger,
                "jitter": max(0.0, min(1.0, pack_jitter)),
            }
            sites = _stack_sites(triangles, up_idx, world_up, cfg, rng)[:max_count]
            positions = [s[0] for s in sites]
            normals = [s[1] for s in sites]
            levels = [s[2] for s in sites]
        elif scatter_mode == "Volume":
            positions, normals = _fill_in_volume(triangles, max_count, rng, spacing, mask=density)
            normals = [world_up] * len(positions)
            levels = [0] * len(positions)
        else:
            # Surface — slope pre-filters the triangle set (correct area weighting,
            # no wasted samples). The paint mask, being per-point, will instead be
            # passed as the `mask=` hook above.
            surf_tris = [t for t in triangles if _slope_ok(t[3], world_up, max_slope)]
            if surf_tris:
                positions, normals = _scatter_on_triangles(surf_tris, max_count, rng, spacing, mask=density)
            else:
                # No mesh / no qualifying faces — fall back to a jittered grid.
                sp = max(spacing, 2.0)
                n = int(math.sqrt(max_count))
                off = (n - 1) * sp / 2
                positions = [
                    Gf.Vec3f(
                        x * sp - off + rng.uniform(-sp * 0.3, sp * 0.3),
                        y * sp - off + rng.uniform(-sp * 0.3, sp * 0.3),
                        0,
                    )
                    for x in range(n)
                    for y in range(n)
                ]
                normals = [Gf.Vec3f(0, 0, 1)] * len(positions)
            levels = [0] * len(positions)

        count = len(positions)

        # Exact mix: quota per prototype, then shuffled so the shares are spread over the
        # scatter instead of arriving in blocks.
        quota = _quota([p.weight for p in protos], count)
        assignment = [i for i, n in enumerate(quota) for _ in range(n)]
        assignment += [0] * (count - len(assignment))
        random.Random(seed + 31).shuffle(assignment)

        scale_rng = random.Random(seed + 13)
        orient_rng = random.Random(seed + 7)
        tilt_rng = random.Random(seed + 19)

        out_positions = Vt.Vec3fArray(count)
        out_scales = Vt.Vec3fArray(count)
        out_orients = Vt.QuathArray(count)
        out_indices = Vt.IntArray(count)

        level_step = height + pack_level_gap

        for i in range(count):
            pidx = assignment[i]
            proto = protos[pidx]
            if placed:
                # Seeded per point when the points were placed by hand, so a point's look is
                # tied to the point rather than to its position in the list.
                #
                # Appending alone would survive the sequential rng too -- consuming one more
                # value leaves the earlier ones untouched, which is worth stating because it
                # is easy to assume otherwise. What does not survive it is *editing*: delete
                # a point from the middle, or reorder them, and every point after it silently
                # changes scale and rotation. Painting is an editing workflow, so the look is
                # pinned to the index.
                scale_rng = random.Random(seed + 13 + i)
                orient_rng = random.Random(seed + 7 + i)
            s = scale_rng.uniform(scale_min, scale_max)
            nrm = normals[i]

            if scatter_mode == "Stack":
                quat = base_quats[pidx]
                if pack_tilt > 1e-6:
                    # Lean about a random lateral axis — a pile that isn't machine-perfect.
                    tilt_axis = Gf.Cross(world_up, _random_lateral(tilt_rng, lat_a, lat_b, axis_vec))
                    quat = _quat_mul(_axis_quat(tilt_axis, math.radians(tilt_rng.uniform(0.0, pack_tilt))), quat)
            else:
                quat = _compute_orientation(orient_mode, nrm, orient_rng, proto, world_up)
            quat = _apply_random_rotation(quat, rand_axis, rand_amount, orient_rng)

            pos = positions[i]
            if scatter_mode == "Stack":
                # Rest the instance's own bounding box on the point it landed on, whatever
                # its pivot happens to be, then lift it by its level. Recomputed from the
                # final orientation so a tilted instance still touches rather than sinks.
                lo_u, _ = _extent_along(proto.corners(s * proto.scale_mult), quat, world_up)
                lift = -lo_u + levels[i] * level_step
                pos = pos + world_up * lift
            elif sit_on_surface:
                lo_n, _ = _extent_along(proto.corners(s * proto.scale_mult), quat, nrm)
                pos = pos + Gf.Vec3f(nrm[0], nrm[1], nrm[2]) * -lo_n

            if abs(normal_offset) > 1e-9:
                pos = pos + Gf.Vec3f(nrm[0] * normal_offset, nrm[1] * normal_offset, nrm[2] * normal_offset)

            out_positions[i] = pos
            out_indices[i] = pidx
            out_orients[i] = _to_quath(quat)
            sm = s * proto.scale_mult
            out_scales[i] = Gf.Vec3f(sm * proto.scale[0], sm * proto.scale[1], sm * proto.scale[2])

        if count:
            instancer.GetPositionsAttr().Set(out_positions)
            instancer.GetProtoIndicesAttr().Set(out_indices)
            instancer.GetScalesAttr().Set(out_scales)
            instancer.GetOrientationsAttr().Set(out_orients)

    _report(node, protos, quota, count, scatter_mode, pack_levels)


def _random_lateral(rng, lat_a, lat_b, axis_vec):
    """A random unit direction in the ground plane (for tilt axes)."""
    ang = rng.uniform(0.0, 2.0 * math.pi)
    return axis_vec[lat_a] * math.cos(ang) + axis_vec[lat_b] * math.sin(ang)


def _report(node, protos, quota, count, mode, levels):
    """Fill the readout label + the output params other chunks can read."""
    if count and protos:
        mix = "  ".join(
            f"{p.label} {q * 100.0 / count:.0f}% ({q})" for p, q in zip(protos, quota) if q
        )
    else:
        mix = "nothing placed — check the surface mesh and prototype paths"
    suffix = f", {levels} levels" if mode == "Stack" and levels > 1 else ""
    text = f"{count} instances · {mode}{suffix}\n{mix}"

    if readout := node.get_param("readout"):
        readout.value = text
    if out_count := node.get_param("out_count"):
        out_count.value = count
    if out_protos := node.get_param("out_prototypes"):
        out_protos.value = len(protos)

    print(f"[ScatterPoints] {text.replace(chr(10), ' | ')}")


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------

_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def _collapsed(group):
    """Collapse a group by default — the chunk is tall, so only the target is open."""
    group.set_state(chunk.ChunkFlags.State.COLLAPSED)
    return group


def main():
    chunk_api = chunk.Chunk("Scatter Points")

    # --- Target ---
    chunk_api.add_param(
        chunk.LineEdit(
            "surface_prim",
            "Surface Mesh",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="Mesh to scatter onto (Surface), fill (Volume) or pack against (Stack).",
        )
    )
    points_input = chunk.LineEdit(
        "points_prim",
        "Painted Points",
        flags=chunk.ChunkFlags.ParamFlags.LABELED,
        tooltip="Place one instance at each point a Scatter Brush painted, instead of "
        "scattering. Wire this from the brush's Painted Points output. While it is "
        "connected, the distribution settings below do nothing and are hidden.",
    )
    points_input.add_callback(_on_points_input_changed)
    chunk_api.add_param(points_input)
    chunk_api.add_param(
        chunk.LineEdit(
            "density_primvar",
            "Density Field",
            flags=chunk.ChunkFlags.ParamFlags.LABELED,
            tooltip="Which painted field decides where instances may land. Wire this from a "
            "Scatter Brush's Density Field output. Empty means no field — scatter everywhere.",
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

    # --- Prototypes (the list) ---
    protos = chunk.ListGroup(
        "prototypes",
        "Prototypes",
        tooltip="One row per prototype. Mix % is relative — the rows are normalised, so "
        "75/25 and 3/1 mean the same thing.",
    )
    protos.add_param(
        chunk.LineEdit(
            "proto_path",
            "Prim",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="Prim to instance. Globs work (/props/crayon_*), and a variant selection "
            "can be written into the path:\n"
            "  /props/crayon{color=red}   one variant\n"
            "  /props/crayon{color=*}     one prototype per variant in the set,\n"
            "                             sharing this row's Mix %.",
        )
    )
    protos.add_param(
        chunk.Slider(
            "proto_weight",
            "Mix %",
            default_value=100.0,
            min_value=0.0,
            max_value=100.0,
            flags=_PF.LABELED,
            tooltip="This prototype's share of the instances, relative to the other rows. "
            "0 skips it. Counts are exact, not statistical: 25% of 100 is 25.",
        )
    )
    protos.add_param(
        chunk.Slider(
            "proto_scale",
            "Scale",
            default_value=1.0,
            min_value=0.01,
            max_value=10.0,
            flags=_PF.LABELED,
            tooltip="Extra scale for this prototype only, on top of the random Scale Min/Max.",
        )
    )
    protos.size = 1
    chunk_api.add_param(protos)

    # Legacy single-prototype field: hidden, kept so scenes saved before the list still
    # resolve their prototype. execute() migrates the value into row 0 on the first cook.
    legacy = chunk.LineEdit(
        "proto_prim",
        "Prototype Prim (legacy)",
        flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="Superseded by the Prototypes list; migrated automatically.",
    )
    legacy.set_state(chunk.ChunkFlags.State.HIDDEN)
    chunk_api.add_param(legacy)

    chunk_api.add_param(
        chunk.Label(
            "readout",
            default_value="no scatter yet",
            tooltip="What the last cook produced, and how the mix actually landed.",
        )
    )

    # --- Distribution ---
    dist_grp = chunk.Group("Distribution", flags=_GF)
    mode_param = chunk.Menu(
        "scatter_mode",
        "Mode",
        menu_items=_SCATTER_MODES,
        default_value="Surface",
        tooltip="Surface: place on faces.\n"
        "Volume: fill the interior of a closed mesh.\n"
        "Stack: pack a regular lattice against the target, resting on it — tight but never "
        "intersecting, and stackable in levels.",
    )
    mode_param.add_callback(_on_mode_changed)
    dist_grp.add_param(mode_param)
    dist_grp.add_param(
        chunk.Checkbox(
            "auto_spacing",
            "Auto Spacing",
            default_value=True,
            tooltip="Derive spacing from the widest prototype's footprint so instances don't "
            "overlap. Turn off to set Spacing manually.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "spacing",
            "Spacing",
            default_value=10.0,
            tooltip="Minimum distance between instances, in scene units (used when Auto Spacing "
            "is off). 0 = allow overlap.",
        )
    )
    dist_grp.add_param(
        chunk.Spinbox(
            "max_count",
            "Max Count",
            default_value=200,
            is_int=True,
            tooltip="Upper limit on instances. Spacing (or the lattice) may place fewer if the "
            "surface fills up first.",
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
    chunk_api.add_param(_collapsed(dist_grp))

    # --- Stacking (Stack mode only) ---
    stack_grp = chunk.Group("Stacking", flags=_GF)
    orient_param = chunk.Menu(
        "stack_orient",
        "Orientation",
        menu_items=_STACK_ORIENTS,
        default_value="Stand Up",
        tooltip="Stand Up: turn the prototype's longest axis to world up — crayons in a cup.\n"
        "Lay Along Rows: lay it down along the row direction — a woodpile.\n"
        "Keep Original: leave the prototype as authored.",
    )
    orient_param.add_callback(_on_stack_orient_changed)
    stack_grp.add_param(orient_param)
    row_axis = chunk.Menu(
        "stack_row_axis",
        "Row Axis",
        menu_items=["X", "Y", "Z"],
        default_value="X",
        tooltip="Which axis the rows run along, for Lay Along Rows.",
    )
    row_axis.set_state(chunk.ChunkFlags.State.HIDDEN)
    stack_grp.add_param(row_axis)
    stack_grp.add_param(
        chunk.Menu(
            "pack_pattern",
            "Pattern",
            menu_items=_PACK_PATTERNS,
            default_value="Hex",
            tooltip="Hex nests every other row into the gaps — the tightest arrangement for "
            "round-ish prototypes. Grid keeps square rows and columns.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_padding",
            "Padding",
            default_value=0.0,
            tooltip="Extra gap between neighbours, in scene units. 0 packs them touching; the "
            "pitch already comes from the prototype's own oriented bounds, so 0 does not "
            "mean intersecting.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_inset",
            "Edge Inset",
            default_value=0.0,
            tooltip="Shrink the packed area inwards by this much, to keep instances from "
            "hanging over the rim.",
        )
    )
    stack_grp.add_param(
        chunk.Slider(
            "pack_levels",
            "Levels",
            default_value=1,
            min_value=1,
            max_value=32,
            is_int=True,
            tooltip="Stack this many tiers, each lifted by the tallest prototype's height.",
        )
    )
    stack_grp.add_param(
        chunk.Spinbox(
            "pack_level_gap",
            "Level Gap",
            default_value=0.0,
            tooltip="Extra vertical gap between tiers, in scene units.",
        )
    )
    stack_grp.add_param(
        chunk.Checkbox(
            "pack_stagger",
            "Stagger Levels",
            default_value=True,
            tooltip="Offset alternate tiers by half a pitch so they nest into the layer below "
            "instead of balancing on it.",
        )
    )
    stack_grp.add_param(
        chunk.Checkbox(
            "pack_conform",
            "Conform to Surface",
            default_value=True,
            tooltip="Drop each lattice point onto the mesh below it, so instances follow the "
            "real surface and its outline. Off places them on the target's bounding-box top.",
        )
    )
    stack_grp.add_param(
        chunk.Slider(
            "pack_jitter",
            "Jitter",
            default_value=0.0,
            min_value=0.0,
            max_value=1.0,
            tooltip="Randomise position within the free space of each cell. 1 uses all of the "
            "slack between pitch and footprint — never enough to intersect.",
        )
    )
    stack_grp.add_param(
        chunk.Slider(
            "pack_tilt",
            "Tilt",
            default_value=0.0,
            min_value=0.0,
            max_value=45.0,
            tooltip="Random lean, in degrees. Instances still rest on the surface — the resting "
            "offset is recomputed from the tilted bounds.",
        )
    )
    chunk_api.add_param(_collapsed(stack_grp))
    stack_grp.set_state(chunk.ChunkFlags.State.HIDDEN)  # Surface is the default mode

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
    chunk_api.add_param(_collapsed(region_grp))

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
            "Random: fully random.\n"
            "Long Axis Up: turn the prototype's longest axis to world up.\n"
            "Long Axis to Normal: same, but along the surface normal.",
        )
    )
    var_grp.add_param(
        chunk.Checkbox(
            "sit_on_surface",
            "Rest on Surface",
            default_value=False,
            tooltip="Offset each instance so the bottom of its bounding box touches the surface "
            "point, instead of its pivot. Fixes half-buried prototypes whose origin isn't at "
            "their base. Stack mode always does this.",
        )
    )
    var_grp.add_param(
        chunk.Menu(
            "rand_axis",
            "Random Rotation Axis",
            menu_items=_RAND_AXES,
            default_value="Off",
            tooltip="Add a random spin around this axis on top of the base orientation. Off "
            "disables it. With a spin on, spacing and pack pitch grow to the footprint's "
            "diagonal so a turned instance still can't overlap its neighbour.",
        )
    )
    var_grp.add_param(
        chunk.Spinbox(
            "rand_amount",
            "Random Rotation (deg)",
            default_value=360.0,
            tooltip="Maximum random rotation in degrees (each instance gets a random angle between "
            "-this and +this).",
        )
    )
    chunk_api.add_param(_collapsed(var_grp))

    # --- Outputs (readable by other chunks as @Scatter Points.out_count@) ---
    chunk_api.add_param(chunk.Slider("out_count", "Instances", is_int=True, flags=_PF.OUTPUT))
    chunk_api.add_param(chunk.Slider("out_prototypes", "Prototypes", is_int=True, flags=_PF.OUTPUT))

    _update_sampling_params(chunk_api)

    return chunk_api


def config():
    return chunk.DefaulConfig(
        category="Create",
        description=(
            "Scatters instances of one or more prototypes across a mesh as a PointInstancer.\n"
            "\n"
            "Prototypes are a list: each row takes a prim (globs allowed), a share of the mix\n"
            "as a percentage, and its own scale multiplier. A variant selection can be written\n"
            "into the path -- /props/crayon{color=red} -- and {color=*} expands to one\n"
            "prototype per variant in that set, splitting the row's share between them. Mix\n"
            "percentages are exact quotas, not probabilities.\n"
            "\n"
            "Modes:\n"
            "  Surface  area-weighted sampling on the faces, Poisson-disk spacing.\n"
            "  Volume   uniform fill of a closed interior.\n"
            "  Stack    a hex or grid lattice packed against the target, each instance resting\n"
            "           on the surface under it and optionally stacked in levels. Pitch comes\n"
            "           from the prototype's oriented bounding box, so packing tight is\n"
            "           collision-free: a cup of crayons standing up, a woodpile, a tray of\n"
            "           bottles. Jitter and tilt stay inside the free space of each cell.\n"
            "\n"
            "Feed the instancer path to the RBD Solver to settle a loose scatter physically."
        ),
    )
