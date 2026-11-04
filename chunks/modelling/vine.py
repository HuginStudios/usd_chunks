"""Vine chunk -- grows climbing vines across a painted region of a mesh.

Pairs with Scatter Brush. Paint where you want vines, wire the brush's **Density Field** into
this chunk's, and a vine network grows there and nowhere else. Wire the brush's **Painted
Points** in as well and each dab becomes a root, so you place the stems by hand and let the
growth decide the rest.

The growth is Runions' space colonization (*Modeling Trees with a Space Colonization
Algorithm*, EGWNP 2007), constrained to the surface. The painted region is turned into a cloud
of **attractors**; every growing node steps toward the average direction of the attractors that
consider it their nearest node, and an attractor is consumed once a node comes within the kill
distance. That is why painting is the whole interface: the paint *is* the attractor cloud, so
the vines fill exactly what was painted, branch where the region forks, and stop when it is
covered -- none of which a "grow N metres of vine" parameter can do.

Three forces are added to that pull, after Luft's *An Ivy Generator* (2007), which is the other
half of the pedigree here: **momentum** (a vine does not turn on the spot), **climb** (an
up-vector standing in for phototropism), and **wander**. Every direction is then flattened into
the surface's tangent plane and the new node re-projected onto the mesh, which is this chunk's
one departure from both papers -- an ivy that leaves the wall is a different tool.

Output is two prims under one Scope:

* ``Curves`` -- a ``BasisCurves`` of the vines, tapered by the pipe model (a fork is thicker
  than either branch leaving it), ready to render with a curve shader.
* ``Leaves`` -- a ``Points`` cloud with a surface normal per point, which is deliberately the
  same shape a Scatter Brush publishes. Wire it into a **Scatter Points** chunk's *Painted
  Points* input and its prototypes are instanced along the vine, laid flat on the wall. Leaf
  geometry is somebody's asset, not something to generate.

Requires: pxr (OpenUSD), usd_chunks, numpy (growth is array work over thousands of nodes).
"""

from __future__ import annotations

import logging
import math

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils
from usd_chunks import chunk_stage, mesh_math

logger = logging.getLogger(__name__)

#: The two prims this chunk authors, under the Vine Path scope.
CURVES_PRIM = "Curves"
LEAVES_PRIM = "Leaves"

_FORMS = ["Smooth", "Linear"]

#: How much of the previous step's direction is carried into the next one.
#:
#: Not a parameter, deliberately. It is the difference between a vine and a ball of wire, not a
#: look to dial: at zero the colonization step turns freely and the result reads as scribble;
#: much past one it overwhelms the attractors and the vine walks straight out of the paint.
#: This sits where a vine stops looking like a random walk and still goes where it is sent.
_MOMENTUM = 0.6

#: Pipe-model exponent. Two is da Vinci's rule -- a branch's cross-section equals the sum of its
#: children's -- which is the thinnest-looking of the usual choices and the right end for a vine,
#: whose whole character is that it barely thickens.
_PIPE_EXPONENT = 2.0

#: Ceilings, so a slider dragged to its end on a scanned asset cannot wedge the session. Both
#: sit far past anything a vine needs, and the readout says when one has been hit.
_MAX_NODES = 20000
_MAX_ATTRACTORS = 20000

#: Surface samples used for re-projection. Independent of the attractors: this set has to cover
#: the painted region evenly whatever the paint says, because it is what a node lands on.
_MAX_SURFACE_SAMPLES = 60000


# ---------------------------------------------------------------------------
# the painted surface
# ---------------------------------------------------------------------------


class _Surface:
    """World-space triangles of the painted region, with the paint kept per corner.

    Seven flat arrays rather than N triangle objects, for the same reason Scatter Points' soup
    is: an ordinary photogrammetry asset is several hundred thousand faces, and building a
    Python object per face costs more than every query it goes on to answer.

    The weights are held *per corner* rather than per face so the edge of a stroke can be
    interpolated. Per face would put the boundary on face lines -- the blocky edge the brush's
    own docstring warns about -- and a vine, unlike a scattered instance, follows that edge
    visibly along its whole length.
    """

    __slots__ = ("v0", "v1", "v2", "n", "w0", "w1", "w2", "area")

    def __init__(self, v0, v1, v2, n, w0, w1, w2):
        self.v0, self.v1, self.v2, self.n = v0, v1, v2, n
        self.w0, self.w1, self.w2 = w0, w1, w2
        self.area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)

    @classmethod
    def empty(cls):
        zero3 = np.zeros((0, 3), dtype=np.float64)
        zero1 = np.zeros(0, dtype=np.float64)
        return cls(zero3, zero3, zero3, zero3, zero1, zero1, zero1)

    def __len__(self):
        return len(self.v0)

    def __bool__(self):
        return len(self.v0) > 0

    def painted(self):
        """Only the triangles the paint reaches, as a new surface.

        Everything downstream -- sampling, the projection grid, the bounds Auto Step measures --
        then works on the painted region rather than on the whole asset. On a rock with one
        patch painted that is the difference between queries against 680k triangles and queries
        against a few hundred.
        """
        keep = np.flatnonzero((self.w0 > 1e-4) | (self.w1 > 1e-4) | (self.w2 > 1e-4))
        if len(keep) == len(self):
            return self
        return _Surface(
            self.v0[keep], self.v1[keep], self.v2[keep], self.n[keep],
            self.w0[keep], self.w1[keep], self.w2[keep],
        )

    def bounds(self):
        """Axis-aligned (low, high) of the triangles, or None when there are none."""
        if not len(self):
            return None
        low = np.minimum(np.minimum(self.v0.min(axis=0), self.v1.min(axis=0)), self.v2.min(axis=0))
        high = np.maximum(np.maximum(self.v0.max(axis=0), self.v1.max(axis=0)), self.v2.max(axis=0))
        return low, high

    def diagonal(self):
        """The diagonal of those bounds -- what Auto Step is measured against."""
        found = self.bounds()
        return float(np.linalg.norm(found[1] - found[0])) if found else 0.0


def _vertex_weights(prim, count, primvar_name):
    """The paint on one mesh, per vertex, or all-ones when there is no field to read.

    All-ones for the *unwired* case: a Vine with nothing connected grows over the surface it was
    pointed at, which is what a chunk with no brush attached should visibly do. A
    wired-but-unpainted brush is the other case entirely -- the primvar exists and is zero, and
    nothing grows. That difference is the whole reason the field is a wire rather than a
    convention, and Scatter Points draws the same line.
    """
    if not primvar_name:
        return np.ones(count, dtype=np.float64)
    primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(primvar_name)
    if not primvar or not primvar.HasValue():
        return np.zeros(count, dtype=np.float64)
    values = primvar.ComputeFlattened()
    if values is None:
        return np.zeros(count, dtype=np.float64)
    values = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    if len(values) == 1:
        # Constant interpolation: one number for the whole mesh, a legitimate way to dial a
        # surface down without painting it.
        return np.full(count, values[0])
    if len(values) < count:
        logger.warning("%s has %d %s values for %d points; ignoring it",
                       prim.GetPath(), len(values), primvar_name, count)
        return np.zeros(count, dtype=np.float64)
    return values[:count]


def _face_outward(mesh, prim, normals, fan, point_count):
    """Point the winding normals the way the mesh says its outside is. Edits `normals` in place.

    Which side of a face is "out" decides which way a vine is lifted, and a vine lifted the
    wrong way is *inside* the wall -- measured on a sphere wound inwards, the tube sat a full
    radius under the surface and drew as nothing. A cross product cannot tell on its own: it
    reports the winding, and the winding is only outward by convention.

    So the two things USD does say are used. `orientation` is normative -- leftHanded means the
    winding runs the other way -- and authored normals are the artist saying it outright, which
    beats any convention. Only vertex-length normals are read; face-varying ones would have to
    be unpacked through the same fan and are not worth it to answer a yes/no question.

    A mesh that says neither keeps its winding, which is all anyone can do with it.
    """
    if UsdGeom.Mesh(prim).GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded:
        normals *= -1.0

    authored = mesh.GetNormalsAttr().Get()
    if authored is None or len(authored) != point_count:
        return
    authored = np.asarray(authored, dtype=np.float64)
    i0, i1, i2 = fan
    # The face's own idea of out, from its three corners. Only the sign of the agreement is
    # used, so it does not matter that the sum is not normalised.
    said = authored[i0] + authored[i1] + authored[i2]
    normals[_dot(normals, said) < 0.0] *= -1.0


def _collect_surface(prim, primvar_name):
    """Every mesh under `prim` as one world-space triangle set, carrying the paint per corner.

    A hierarchy, not a mesh: what a user picks as "the surface" is almost never a single Mesh,
    and the brush already learned that lesson the hard way -- see its `_paint_meshes`.
    """
    if not prim or not prim.IsValid():
        return _Surface.empty()

    parts = []
    for p in Usd.PrimRange(prim):
        mesh = UsdGeom.Mesh(p)
        points = mesh.GetPointsAttr().Get()
        if not points:
            continue
        counts = mesh.GetFaceVertexCountsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not counts or not indices:
            continue
        triangles, _, _ = mesh_math.fan_triangulate(counts, indices)
        if not len(triangles):
            continue
        fan = tuple(triangles.T)
        i0, i1, i2 = fan
        world = mesh_math.world_points(p, points)
        weights = _vertex_weights(p, len(points), primvar_name)
        v0, v1, v2 = world[i0], world[i1], world[i2]
        n = np.cross(v1 - v0, v2 - v0)
        length = np.linalg.norm(n, axis=1)
        real = length > 1e-12
        n[real] /= length[real, None]
        _face_outward(mesh, p, n, (i0, i1, i2), len(points))
        parts.append(_Surface(v0, v1, v2, n, weights[i0], weights[i1], weights[i2]))

    if not parts:
        return _Surface.empty()
    if len(parts) == 1:
        return parts[0]
    return _Surface(*(
        np.concatenate([getattr(part, name) for part in parts])
        for name in ("v0", "v1", "v2", "n", "w0", "w1", "w2")
    ))


# ---------------------------------------------------------------------------
# sampling and projection
# ---------------------------------------------------------------------------


def _sample(surface, count, rng, weighted):
    """`count` points scattered over the surface: positions, normals, weights, triangle ids.

    `weighted` picks triangles in proportion to area *and* paint, which is what attractors want;
    without it the choice is area alone, which is what the projection set wants -- it has to
    cover the region evenly however faintly a part of it was painted.
    """
    if count <= 0 or not len(surface):
        return _no_samples()
    strength = surface.area * (
        (surface.w0 + surface.w1 + surface.w2) / 3.0 if weighted else 1.0
    )
    total = float(strength.sum())
    if total <= 0.0:
        return _no_samples()

    idx = rng.choice(len(surface), size=int(count), p=strength / total)
    u = rng.random(len(idx))
    v = rng.random(len(idx))
    # Fold the far half of the unit square back over the diagonal -- the standard uniform
    # barycentric sample, and one array pass rather than a rejection loop.
    fold = (u + v) > 1.0
    u[fold] = 1.0 - u[fold]
    v[fold] = 1.0 - v[fold]

    e1 = surface.v1[idx] - surface.v0[idx]
    e2 = surface.v2[idx] - surface.v0[idx]
    points = surface.v0[idx] + e1 * u[:, None] + e2 * v[:, None]
    weights = surface.w0[idx] * (1.0 - u - v) + surface.w1[idx] * u + surface.w2[idx] * v
    return points, surface.n[idx], np.clip(weights, 0.0, 1.0), idx


def _no_samples():
    return (
        np.zeros((0, 3), dtype=np.float64),
        np.zeros((0, 3), dtype=np.float64),
        np.zeros(0, dtype=np.float64),
        np.zeros(0, dtype=np.int64),
    )


#: Cell ids are packed into one integer -- `(x * S + y) * S + z` -- so a cell groups with
#: np.unique and looks up without a tuple being built per query. The packing is linear, so a
#: neighbour's id is this cell's plus a constant.
_KEY_STRIDE = 1 << 20

_KEY_OFFSETS = np.asarray(
    [
        (dx * _KEY_STRIDE + dy) * _KEY_STRIDE + dz
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
    ],
    dtype=np.int64,
)


#: Shared empty candidate list, for a cell whose neighbourhood holds no surface at all.
_NO_TRIANGLES = np.zeros(0, dtype=np.int64)


class _PointGrid:
    """Nearest-point lookup over a uniform cell hash.

    Every node the growth places has to be put back onto the mesh, and doing that against the
    whole triangle set is O(faces) per node -- on a scan, the entire cook. So the surface is
    sampled once, the samples are bucketed here, and a projection asks this for the handful of
    samples nearby and then does exact triangle maths on only their faces.
    """

    __slots__ = ("_points", "_origin", "_cell", "_cells")

    def __init__(self, points, cell):
        self._points = np.asarray(points, dtype=np.float64)
        self._cell = max(float(cell), 1e-9)
        self._origin = self._points.min(axis=0) if len(self._points) else np.zeros(3)
        keys = self._keys(self._points)
        order = np.argsort(keys, kind="stable")
        unique, starts = np.unique(keys[order], return_index=True)
        self._cells = dict(zip(unique.tolist(), np.split(order, starts[1:])))

    def _keys(self, points):
        cell = np.floor_divide(np.asarray(points, dtype=np.float64) - self._origin, self._cell)
        cell = cell.astype(np.int64)
        return (cell[..., 0] * _KEY_STRIDE + cell[..., 1]) * _KEY_STRIDE + cell[..., 2]

    def keys_of(self, points):
        """The cell id of each of an (N, 3) array of points."""
        return self._keys(np.asarray(points, dtype=np.float64))

    def near_key(self, key):
        """Indices of every sample in the 27 cells around a cell id; may be empty."""
        if not len(self._points):
            return np.zeros(0, dtype=np.int64)
        found = [self._cells[k] for k in (int(key) + _KEY_OFFSETS).tolist() if k in self._cells]
        if not found:
            return np.zeros(0, dtype=np.int64)
        return found[0] if len(found) == 1 else np.concatenate(found)

    def near(self, point):
        """Indices of every sample in the 27 cells around `point`; may be empty."""
        if not len(self._points):
            return np.zeros(0, dtype=np.int64)
        return self.near_key(self._keys(np.asarray(point, dtype=np.float64).reshape(1, 3))[0])


def _dot(a, b):
    return np.sum(a * b, axis=-1)


def _closest_on_triangles(point, v0, v1, v2):
    """Closest point on each triangle to the matching point, over any leading shape.

    Ericson's region test (*Real-Time Collision Detection*, §5.1.5) written over arrays: the
    barycentric interior solution is computed for every triangle and then overwritten wherever
    the point falls outside an edge or past a vertex. The regions are disjoint, so the order the
    overwrites happen in does not matter.

    Shapes broadcast, which is what lets a whole growth iteration be answered in one call. One
    point against M triangles is `(3,)` and `(M, 3)`; Q points against K candidates each is
    `(Q, 1, 3)` and `(Q, K, 3)`. Per-call numpy overhead was 310us against a median of 21
    triangles -- the arithmetic was never the cost, the three thousand calls were.
    """
    ab = v1 - v0
    ac = v2 - v0
    ap = point - v0
    d1 = _dot(ab, ap)
    d2 = _dot(ac, ap)
    bp = point - v1
    d3 = _dot(ab, bp)
    d4 = _dot(ac, bp)
    cp = point - v2
    d5 = _dot(ab, cp)
    d6 = _dot(ac, cp)

    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    denom = va + vb + vc

    with np.errstate(divide="ignore", invalid="ignore"):
        inv = np.where(np.abs(denom) > 1e-20, 1.0 / denom, 0.0)
    out = v0 + ab * (vb * inv)[..., None] + ac * (vc * inv)[..., None]
    # A degenerate triangle has no interior to land in; its first corner is as good as anything.
    degenerate = np.abs(denom) <= 1e-20
    out = np.where(degenerate[..., None], v0, out)

    edge = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    if edge.any():
        t = _safe_ratio(d1, d1 - d3)
        out = np.where(edge[..., None], v0 + ab * t[..., None], out)
    edge = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    if edge.any():
        t = _safe_ratio(d2, d2 - d6)
        out = np.where(edge[..., None], v0 + ac * t[..., None], out)
    edge = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    if edge.any():
        t = _safe_ratio(d4 - d3, (d4 - d3) + (d5 - d6))
        out = np.where(edge[..., None], v1 + (v2 - v1) * t[..., None], out)

    out = np.where(((d1 <= 0) & (d2 <= 0))[..., None], v0, out)
    out = np.where(((d3 >= 0) & (d4 <= d3))[..., None], v1, out)
    out = np.where(((d6 >= 0) & (d5 <= d6))[..., None], v2, out)
    return out


def _safe_ratio(numerator, denominator):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.clip(np.where(np.abs(denominator) > 1e-20, numerator / denominator, 0.0), 0.0, 1.0)


class _Projector:
    """Puts a point back on the skin: the nearest surface point, and the normal there.

    Two stages, because neither alone is affordable. The grid narrows tens of thousands of
    triangles down to the few whose neighbourhood the point is in; `_closest_on_triangles` then
    answers exactly for those, so the result is a genuine closest point rather than the nearest
    sample. A node landing where no sample is -- off the edge of the paint, usually -- is left
    where it is and reports no normal, and the growth reads that as "stop".
    """

    def __init__(self, surface, samples, triangle_of, cell):
        self._surface = surface
        self._triangle_of = triangle_of
        self._grid = _PointGrid(samples, cell) if len(samples) else None
        # Which triangles a cell's neighbourhood holds depends only on the cell, so it is
        # answered once and kept. A vine walks the same neighbourhood for several steps
        # running, and every node of every iteration was re-deriving it.
        self._candidates: dict = {}

    def project(self, point):
        """(position, normal) on the surface, or (point, None) when it is out of reach."""
        landed, normals, found = self.project_many(np.asarray(point, dtype=np.float64)[None, :])
        return (landed[0], normals[0]) if found[0] else (point, None)

    def _cell_triangles(self, key):
        tris = self._candidates.get(key)
        if tris is None:
            near = self._grid.near_key(key)
            tris = np.unique(self._triangle_of[near]) if len(near) else _NO_TRIANGLES
            self._candidates[key] = tris
        return tris

    def project_many(self, points):
        """Put a whole batch back on the skin: (positions, normals, found).

        One call per growth iteration instead of one per node. The arithmetic was never the
        cost -- a median query tests 21 triangles -- but at 310us of numpy call overhead apiece,
        three thousand of them were most of the cook.

        Rows are padded out to the widest candidate list by repeating a candidate that is
        already in the row, so padding can only ever tie with a real answer and never beat one.
        The result is what the one-at-a-time version produced, triangle for triangle.
        """
        points = np.asarray(points, dtype=np.float64)
        found = np.zeros(len(points), dtype=bool)
        landed = points.copy()
        normals = np.zeros_like(points)
        if self._grid is None or not len(points):
            return landed, normals, found

        per_point = [self._cell_triangles(key) for key in self._grid.keys_of(points).tolist()]
        lengths = np.asarray([len(t) for t in per_point])
        live = np.flatnonzero(lengths)
        if not len(live):
            return landed, normals, found

        # The ragged candidate lists as (Q, K) blocks, by index arithmetic rather than a
        # np.full per row: padding a row at a time was thousands of tiny allocations a cook.
        # Short rows repeat their last entry, which is already a candidate of that row, so a
        # padded column can only tie with a real answer and never beat one. Rows go in blocks
        # of about the same length, shortest first: one block padded every row to the
        # longest, about twice the work. Each row's answer is the same either way.
        lengths = lengths[live]
        by_length = np.argsort(lengths, kind="stable")
        s = self._surface
        start = 0
        while start < len(by_length):
            width = int(lengths[by_length[start]])
            stop = int(np.searchsorted(lengths[by_length], width * 1.25, side="right"))
            stop = max(stop, start + 1)
            block = by_length[start:stop]
            rows_live = live[block]
            block_lengths = lengths[block]
            width = int(block_lengths.max())
            flat = np.concatenate([per_point[i] for i in rows_live])
            starts = np.concatenate(([0], np.cumsum(block_lengths)[:-1]))
            column = np.minimum(np.arange(width)[None, :], (block_lengths - 1)[:, None])
            index = flat[starts[:, None] + column]
            query = points[rows_live][:, None, :]
            candidates = _closest_on_triangles(query, s.v0[index], s.v1[index], s.v2[index])
            offset = candidates - query
            best = np.argmin(_dot(offset, offset), axis=1)
            rows = np.arange(len(rows_live))
            landed[rows_live] = candidates[rows, best]
            normals[rows_live] = s.n[index[rows, best]]
            start = stop
        found[live] = True
        return landed, normals, found


# ---------------------------------------------------------------------------
# growth
# ---------------------------------------------------------------------------


class _Nodes:
    """The vine skeleton as it is built: position, parent, surface normal, last direction.

    Appended in creation order, which makes a parent's index always lower than its child's, so
    every bottom-up pass (widths, subtree length) is one reversed loop with no sorting.

    The three vectors live in one growing (N, 3) buffer each rather than in a list of little
    arrays, so a whole iteration's worth of nodes can be read and written by fancy indexing.
    `nodes.position[some_indices]` was `np.asarray([nodes.position[i] for i in ...])`, once per
    iteration over every attractor still pulling -- a list comprehension in the hot loop.
    `position`, `normal` and `direction` are views of the filled part, so indexed writes into
    them (the lift does one) land in the buffer.
    """

    __slots__ = ("parent", "_position", "_normal", "_direction", "_count")

    def __init__(self, capacity=256):
        capacity = max(int(capacity), 8)
        self.parent: list[int] = []
        self._position = np.zeros((capacity, 3), dtype=np.float64)
        self._normal = np.zeros((capacity, 3), dtype=np.float64)
        self._direction = np.zeros((capacity, 3), dtype=np.float64)
        self._count = 0

    @property
    def position(self):
        return self._position[: self._count]

    @property
    def normal(self):
        return self._normal[: self._count]

    @property
    def direction(self):
        return self._direction[: self._count]

    def _reserve(self, extra):
        capacity = len(self._position)
        if self._count + extra <= capacity:
            return
        while capacity < self._count + extra:
            capacity *= 2
        for name in ("_position", "_normal", "_direction"):
            grown = np.zeros((capacity, 3), dtype=np.float64)
            grown[: self._count] = getattr(self, name)[: self._count]
            setattr(self, name, grown)

    def add(self, position, parent, normal, direction):
        self._reserve(1)
        index = self._count
        self._position[index] = position
        self._normal[index] = normal
        self._direction[index] = direction
        self.parent.append(int(parent))
        self._count += 1
        return index

    def add_many(self, positions, parents, normals, directions):
        """Append a whole growth iteration at once; the indices they were given."""
        count = len(positions)
        if not count:
            return np.zeros(0, dtype=np.int64)
        self._reserve(count)
        start = self._count
        self._position[start:start + count] = positions
        self._normal[start:start + count] = normals
        self._direction[start:start + count] = directions
        self.parent.extend(int(p) for p in parents)
        self._count += count
        return np.arange(start, start + count)

    def __len__(self):
        return self._count


def _tangent(direction, normal, rng):
    """`direction` flattened into the surface's tangent plane, unit length.

    A direction pointing straight into or out of the wall has no tangent, which happens on a
    root whose only pull is the climb vector and whose wall faces up. Rather than stall, it
    picks a tangent at random -- one vine out of a hundred starts in an arbitrary direction,
    which is indistinguishable from wander.
    """
    if normal is not None:
        direction = direction - normal * float(np.dot(direction, normal))
    length = float(np.linalg.norm(direction))
    if length > 1e-9:
        return direction / length
    if normal is None:
        return np.asarray([1.0, 0.0, 0.0])
    # Any vector not parallel to the normal, crossed with it, lies in the plane.
    other = np.asarray([0.0, 0.0, 1.0]) if abs(normal[2]) < 0.9 else np.asarray([1.0, 0.0, 0.0])
    fallback = np.cross(normal, other)
    fallback = fallback + rng.normal(scale=1e-3, size=3)
    return fallback / max(float(np.linalg.norm(fallback)), 1e-9)


def _tangents(directions, normals):
    """`_tangent` for a whole iteration, and without the random nudge.

    The nudge exists to give roots that have no pull yet somewhere to face; a growing node
    always has one, so the only case this has to survive is a direction that happens to point
    straight into the wall, and any tangent will do for that.
    """
    flat = directions - normals * _dot(directions, normals)[:, None]
    length = np.linalg.norm(flat, axis=1)
    stuck = length <= 1e-9
    if stuck.any():
        other = np.where(
            np.abs(normals[stuck][:, 2:3]) < 0.9,
            np.asarray([0.0, 0.0, 1.0]),
            np.asarray([1.0, 0.0, 0.0]),
        )
        flat[stuck] = np.cross(normals[stuck], other)
        length[stuck] = np.linalg.norm(flat[stuck], axis=1)
    return flat / np.maximum(length, 1e-9)[:, None]


def _choose_roots(points, up, count, spacing):
    """Which attractors vines start from when nobody painted the roots by hand.

    Lowest first, then spread: a vine that starts at the bottom of what was painted and climbs
    is the picture people have of ivy, and starting them all in one corner -- which picking at
    random cheerfully does -- reads as one vine, not five. Spacing is relaxed rather than
    enforced, so a narrow painted strip still gets the roots it was asked for.

    Indices, not positions, so the caller can take the normal it already sampled alongside each
    attractor rather than projecting thousands of candidates to find five.
    """
    if not len(points) or count <= 0:
        return []
    order = np.argsort(points @ up)
    chosen: list[int] = []
    for gap in (spacing, spacing * 0.25, 0.0):
        for i in order.tolist():
            if len(chosen) >= count:
                break
            if gap > 0.0 and chosen and _too_near(points, i, chosen, gap):
                continue
            chosen.append(i)
        if len(chosen) >= count:
            break
    return chosen


def _too_near(points, i, chosen, gap) -> bool:
    """Whether point i is closer than gap to any chosen point.

    The distances in one array call; a pair within rounding of the gap is decided by the
    one-pair norm the per-pair loop used, so the answer is the loop's exactly. Pair by pair
    it was 64k norm calls a vine.
    """
    near = np.sqrt(np.einsum("ij,ij->i", points[chosen] - points[i], points[chosen] - points[i]))
    if (near < gap * (1.0 - 1e-9)).any():
        return True
    close = np.flatnonzero(np.abs(near - gap) <= gap * 1e-9)
    return any(float(np.linalg.norm(points[i] - points[chosen[k]])) < gap for k in close.tolist())


def _grow(roots, attractors, projector, settings, rng):
    """Space colonization, on a surface. Returns the node set.

    One iteration is: attach every attractor to its nearest node, drop the ones a node has
    reached, then step each node that still has attractors toward their average direction.

    The nearest-node bookkeeping is kept incrementally rather than recomputed. Attractors never
    move and nodes are only ever added, so the distance from every attractor to the handful of
    nodes born this iteration is the only thing that can change the answer -- an (A, new) matrix
    per iteration instead of an (A, N) one, which is what makes a few thousand of each a second
    rather than a minute.
    """
    nodes = _Nodes()
    for position, normal in roots:
        nodes.add(position, -1, normal, _tangent(rng.normal(size=3), normal, rng))
    if not len(nodes) or not len(attractors):
        return nodes

    alive = np.ones(len(attractors), dtype=bool)
    nearest = np.full(len(attractors), -1, dtype=np.int64)
    nearest_sq = np.full(len(attractors), np.inf)
    attractor_sq = _dot(attractors, attractors)
    pending = np.arange(len(nodes))

    up = settings["up"]
    step = settings["step"]
    influence_sq = settings["influence"] ** 2
    kill_sq = settings["kill"] ** 2
    climb = settings["climb"]
    wander = settings["wander"]

    while len(pending) and len(nodes) < settings["max_nodes"]:
        fresh = nodes.position[pending]
        live = np.flatnonzero(alive)
        if not len(live):
            break

        # |a - f|^2 expanded, so the distances are one matrix product rather than an
        # (attractors x new nodes x 3) difference that has to be built before it is reduced.
        # That temporary was the largest allocation in the cook and was made every iteration.
        # Clipped at zero: the expansion can go a few ulps negative where a node sits exactly
        # on an attractor, and a negative square would come back NaN from the sqrt nobody takes
        # but would still lose the comparison against the kill distance.
        distance_sq = np.maximum(
            attractor_sq[live][:, None] - 2.0 * (attractors[live] @ fresh.T) + _dot(fresh, fresh),
            0.0,
        )
        closest = np.argmin(distance_sq, axis=1)
        best_sq = distance_sq[np.arange(len(live)), closest]
        better = best_sq < nearest_sq[live]
        if better.any():
            improved = live[better]
            nearest_sq[improved] = best_sq[better]
            nearest[improved] = pending[closest[better]]

        # Reached: an attractor a node has come this close to has done its job.
        alive[live[nearest_sq[live] <= kill_sq]] = False

        pulling = np.flatnonzero(alive & (nearest >= 0) & (nearest_sq <= influence_sq))
        if not len(pulling):
            break

        # Sum of unit directions, accumulated per node. np.add.at rather than a loop: on a
        # filled region this runs over every attractor still alive, every iteration.
        pull = np.zeros((len(nodes), 3), dtype=np.float64)
        towards = attractors[pulling] - nodes.position[nearest[pulling]]
        length = np.linalg.norm(towards, axis=1)
        real = length > 1e-9
        towards[real] /= length[real, None]
        np.add.at(pull, nearest[pulling], towards)

        # The whole iteration steps at once: one direction array, one projection, one append.
        # Per node this was three numpy calls, an rng draw and a `project` -- 310us of call
        # overhead each against a median of 21 candidate triangles, which was most of the cook.
        owners = np.unique(nearest[pulling])[: settings["max_nodes"] - len(nodes)]
        direction = pull[owners]
        length = np.linalg.norm(direction, axis=1)
        moving = length > 1e-9
        owners, direction = owners[moving], direction[moving] / length[moving, None]
        if not len(owners):
            break

        direction = direction + nodes.direction[owners] * _MOMENTUM + up * climb
        if wander > 0.0:
            direction = direction + rng.normal(scale=wander, size=direction.shape)
        direction = _tangents(direction, nodes.normal[owners])

        landed, landed_normals, found = projector.project_many(
            nodes.position[owners] + direction * step
        )
        # Anything that did not land grew off the edge of what was painted, and that branch
        # ends there -- which is what keeps the vines inside the paint with no boundary test.
        reached = np.flatnonzero(found)
        pending = nodes.add_many(
            landed[reached], owners[reached], landed_normals[reached], direction[reached]
        )

    return nodes


def _prune(nodes, min_length):
    """Cut off side branches shorter than `min_length` nodes.

    Raw colonization is hairy. Every node that briefly had an attractor to itself sprouts a
    two-point stub, and on the wall this was first run against, half of the 245 branches were
    two or three points long -- which draws as fuzz on a vine rather than as a vine.

    A branch's *continuation* is never pruned however short it gets, so a vine still tapers to
    a tip; it is only the side shoots that have to earn their place. Pruning cascades, because a
    kept branch's own stubs are then judged the same way.
    """
    if min_length <= 1 or not len(nodes):
        return nodes

    kids = _children(nodes)
    depth = _subtree_depth(nodes, kids)
    keep = [False] * len(nodes)
    stack = [index for index in range(len(nodes)) if nodes.parent[index] < 0]
    for index in stack:
        keep[index] = True
    while stack:
        index = stack.pop()
        ordered = sorted(kids[index], key=lambda c: depth[c], reverse=True)
        for rank, child in enumerate(ordered):
            if rank == 0 or depth[child] >= min_length:
                keep[child] = True
                stack.append(child)

    remap = {-1: -1}
    kept = _Nodes()
    for index in range(len(nodes)):
        if not keep[index]:
            continue
        # A parent is always kept when its child is, so the lookup cannot miss.
        remap[index] = kept.add(
            nodes.position[index],
            remap[nodes.parent[index]],
            nodes.normal[index],
            nodes.direction[index],
        )
    return kept


def _lift(nodes, radii, factor):
    """Float each node off the skin by a multiple of *its own* radius.

    Lift is per node, not one distance for the whole network: taken from the base width, a tip
    a quarter as thick as the trunk would be pushed out four times further than it needs, and
    every increase in Thickness would translate the entire vine away from the surface instead
    of fattening it in place.

    Per node, `factor` is what the vine *rests* at rather than how far away it sits: at 1.0 the
    tube is exactly tangent to the wall whatever it is carrying, so Thickness only ever changes
    how thick it is. That is the same reason a fatter rope on a table has its centre higher and
    still touches the table.
    """
    if factor == 0.0:
        return
    nodes.position[:] += nodes.normal * (np.asarray(radii, dtype=np.float64) * factor)[:, None]


# ---------------------------------------------------------------------------
# skeleton to curves
# ---------------------------------------------------------------------------


def _children(nodes):
    kids = [[] for _ in range(len(nodes))]
    for index, parent in enumerate(nodes.parent):
        if parent >= 0:
            kids[parent].append(index)
    return kids


def _subtree_depth(nodes, kids):
    """How many nodes the longest chain below each node runs to.

    Bottom-up in one reversed pass, which works only because a node's index always exceeds its
    parent's -- see `_Nodes`.
    """
    depth = [1] * len(nodes)
    for index in range(len(nodes) - 1, -1, -1):
        for child in kids[index]:
            depth[index] = max(depth[index], depth[child] + 1)
    return depth


def _strands(nodes):
    """The skeleton as a list of index chains, one per curve.

    At a fork the longest child continues the strand and the others start their own -- the usual
    decomposition, and the one that matters here: continuing into the *first* child instead
    breaks a vine into stubs wherever a side shoot happens to have been created first, and a
    curve renderer draws every break.

    Each new strand begins at the fork rather than after it, so the two overlap by one point and
    the branch reads as joined rather than floating.
    """
    kids = _children(nodes)
    depth = _subtree_depth(nodes, kids)
    strands = []
    starts = [(index, None) for index in range(len(nodes)) if nodes.parent[index] < 0]
    while starts:
        index, attach = starts.pop()
        chain = [attach] if attach is not None else []
        chain.append(index)
        while kids[index]:
            ordered = sorted(kids[index], key=lambda c: depth[c], reverse=True)
            for side in ordered[1:]:
                starts.append((side, index))
            index = ordered[0]
            chain.append(index)
        if len(chain) >= 2:
            strands.append(chain)
    return strands


def _radii(nodes, base, tip):
    """A radius per node: the pipe model's *profile*, stretched to fit Thickness and Taper.

    The pipe model on its own gives a shape, not a size. A vine only thickens where it forks, so
    a chain of a hundred nodes carrying two branches comes out barely wider than its own tip --
    which meant Thickness, read as an absolute width and clamped to, did nothing at all on most
    vines. It sat at a figure the growth never reached.

    So the model is run with unit tips to get the relative profile, and that profile is then
    mapped onto the range the two params name: a tip is exactly Taper of the width, the thickest
    node is exactly Thickness, and everything between keeps the shape the branching produced. A
    vine that never forked is uniformly its tip width, which is the honest answer for it.
    """
    if not len(nodes):
        return []
    kids = _children(nodes)
    pipe = [1.0] * len(nodes)
    for index in range(len(nodes) - 1, -1, -1):
        if kids[index]:
            pipe[index] = sum(pipe[c] ** _PIPE_EXPONENT for c in kids[index]) ** (1.0 / _PIPE_EXPONENT)

    widest = max(pipe)
    if widest <= 1.0 + 1e-9:
        return [tip * 0.5] * len(nodes)
    scale = (base - tip) * 0.5 / (widest - 1.0)
    return [tip * 0.5 + (value - 1.0) * scale for value in pipe]


def _smooth_nodes(nodes, strands, passes):
    """Round the corners off every strand, in place on the skeleton.

    Done here rather than at authoring time so the result can be put back on the surface
    afterwards. Smoothing cuts corners, and on anything convex that cuts *into* the wall: on a
    sphere it pulled control points up to half a radius under the surface, worst on thin vines,
    because the correction it applies is a fraction of the step and has nothing to do with how
    fat the tube is.

    A fork node belongs to two strands -- it ends one and begins the next -- but `_smooth` pins
    the ends it is given, so the second pass over it never moves it.
    """
    if passes <= 0:
        return
    for chain in strands:
        nodes.position[chain] = _smooth(nodes.position[chain], passes)


def _resurface(nodes, projector):
    """Put every node back on the skin, and take the normal there.

    Smoothing moved them off it. Re-reading the normal matters as much as the position: it is
    what the lift and the leaves are measured along, and a node whose normal still describes
    where it used to be tilts a leaf that is no longer there.
    """
    if not len(nodes):
        return
    landed, normals, found = projector.project_many(nodes.position)
    reached = np.flatnonzero(found)
    nodes.position[reached] = landed[reached]
    nodes.normal[reached] = normals[reached]


def _smooth(points, passes):
    """Laplacian smoothing with the ends pinned.

    The colonization step turns by whatever the attractors say, and on a filled region that is
    visibly faceted at the step size. Two passes take the corners off without moving the vine
    anywhere it did not grow.
    """
    points = np.asarray(points, dtype=np.float64)
    for _ in range(int(passes)):
        if len(points) < 3:
            break
        middle = (points[:-2] + points[2:]) * 0.5
        points = points.copy()
        points[1:-1] = points[1:-1] * 0.5 + middle * 0.5
    return points


def _pin(points, widths):
    """Repeat both ends so a cubic B-spline actually reaches them.

    A B-spline stays inside its hull, so an unpinned vine starts and stops a segment short of
    where it grew -- most visible at the root, which lifts off the ground. Tripling the end
    control points is the standard fix and costs four vertices a curve.
    """
    points = np.concatenate((points[:1], points[:1], points, points[-1:], points[-1:]))
    widths = np.concatenate((widths[:1], widths[:1], widths, widths[-1:], widths[-1:]))
    return points, widths


# ---------------------------------------------------------------------------
# leaves
# ---------------------------------------------------------------------------


def _leaf_points(strands, positions, normals, radii, spacing):
    """Points along the vines at a fixed arc-length spacing, with the surface normal at each.

    Deliberately the same shape a Scatter Brush's Painted Points output has -- position plus
    normal -- so a Scatter Points chunk instances leaves along a vine with the code it already
    has, rather than this chunk growing leaf geometry nobody asked it for.

    Offset outward by the local radius so a leaf sits *on* the vine instead of inside it.
    """
    if spacing <= 0.0:
        return [], []
    points, facing = [], []
    for chain in strands:
        travelled = 0.0
        next_leaf = spacing
        for a, b in zip(chain[:-1], chain[1:]):
            start, end = positions[a], positions[b]
            segment = float(np.linalg.norm(end - start))
            if segment <= 1e-9:
                continue
            while next_leaf <= travelled + segment:
                t = (next_leaf - travelled) / segment
                normal = normals[b]
                at = start + (end - start) * t
                points.append(Gf.Vec3f(*(at + normal * radii[b])))
                facing.append(Gf.Vec3f(*normal))
                next_leaf += spacing
            travelled += segment
    return points, facing


# ---------------------------------------------------------------------------
# cook
# ---------------------------------------------------------------------------


def _settings(node, surface):
    """Everything the growth needs, with the scale-relative params resolved to scene units.

    **Two units, not one.** How far apart the growth's decisions are (Step) and how thick the
    result is (Thickness) are different questions, and measuring the second in multiples of the
    first would make a finer vine a thinner one -- at the default resolution, a stem at a
    five-hundredth of the object it grows on, around one pixel on screen.

    So the growth distances (influence, kill) are multiples of Step, which is the space
    colonization papers' own convention and genuinely is about resolution; and every visible
    size -- width, lift off the wall, leaf spacing -- hangs off Thickness, which is a share of
    the painted region. Changing Step now changes how smoothly a vine curves and nothing else.
    """
    def number(name):
        return float(node.get_param(name).value or 0.0)

    region = max(surface.diagonal(), 1e-6)

    step = number("step")
    if node.get_param("auto_step").value:
        # A hundred and fifty steps across the painted region: enough that a vine curves rather
        # than zig-zags, and few enough that a wall does not cost a hundred thousand nodes.
        step = region / 150.0
    step = max(step, 1e-6)

    # A percentage of the painted region, so it means the same on a doorway and on a cliff
    # face. One percent is a 5cm stem across a 5m wall -- a mature ivy, and legible in a
    # viewport, which the botanically honest figure is not.
    base_width = max(region * number("thickness") / 100.0, 1e-9)

    return {
        "step": step,
        "influence": step * number("influence"),
        "kill": step * number("kill"),
        "climb": number("climb"),
        "wander": number("wander"),
        "density": number("density"),
        "roots": max(1, int(number("root_count"))),
        "min_branch": max(1, int(number("min_branch"))),
        "max_nodes": max(2, min(_MAX_NODES, int(number("max_points")))),
        "base_width": base_width,
        "taper": number("taper"),
        # A multiple of each node's own radius, applied per node -- not a distance. See `_lift`.
        "lift": number("lift"),
        "smooth": max(0, int(number("smooth"))),
        "form": node.get_param("curve_form").value,
        "leaves": bool(node.get_param("leaves").value),
        "leaf_spacing": base_width * number("leaf_spacing"),
        "leaf_size": base_width * number("leaf_size"),
    }


def _painted_roots(stage, points_path, projector, limit):
    """Roots the brush placed by hand, projected onto the surface.

    A dab lands on the skin already, but the brush records where the *cursor* hit and this
    chunk's idea of the surface is a sampled reconstruction of it -- so they are reconciled here
    rather than trusted to agree. A dab outside the painted region has nowhere to grow and is
    dropped, which is the same rule the growth itself follows.

    Only a point cloud is read. A mesh has points too, and a mesh path wired here by mistake
    made every vertex a root: a scanned rock froze the session, then crashed it.

    Args:
        stage: The stage to read the points from.
        points_path: The Painted Points path.
        projector: Puts each point on the painted surface.
        limit: The most roots to take. More are thinned evenly.

    Returns:
        (roots, note): the roots as (position, normal) pairs, and what to tell the user --
        empty when there is nothing to say.
    """
    if not points_path or stage is None:
        return [], ""
    prim = stage.GetPrimAtPath(points_path)
    if not prim or not prim.IsValid():
        return [], ""
    if not prim.IsA(UsdGeom.Points):
        return [], f"Painted Points is a {prim.GetTypeName() or 'prim'}, not a point cloud: {points_path}"
    points = UsdGeom.Points(prim).GetPointsAttr().Get()
    if not points:
        return [], ""
    note = ""
    if len(points) > limit:
        note = f"used {limit} of {len(points)} painted points"
        points = [points[int(i)] for i in np.linspace(0, len(points) - 1, limit)]
    roots = []
    for point in points:
        landed, normal = projector.project(np.asarray(point, dtype=np.float64))
        if normal is not None:
            roots.append((landed, normal))
    return roots, note


def _attractor_count(surface, settings):
    """How many attractors the painted region gets.

    Derived from its area and the kill distance rather than typed: an attractor's only job is to
    be consumed, so the number that matters is how many fit in the region at the spacing the
    kill distance sets. Density then scales that -- a quarter of the natural figure leaves gaps
    between the vines, four times it packs them.
    """
    area = float((surface.area * (surface.w0 + surface.w1 + surface.w2) / 3.0).sum())
    kill = max(settings["kill"], 1e-9)
    natural = area / (kill * kill)
    return int(max(8, min(_MAX_ATTRACTORS, natural * (0.25 + 3.75 * settings["density"]))))


def _surface_sample_count(surface, settings):
    """How many samples the projection set gets: about four per step across the region."""
    area = float(surface.area.sum())
    spacing = max(settings["step"] * 0.5, 1e-9)
    return int(max(16, min(_MAX_SURFACE_SAMPLES, area / (spacing * spacing))))


def _up_axis(stage):
    return (
        np.asarray([0.0, 1.0, 0.0])
        if UsdGeom.GetStageUpAxis(stage).upper() == "Y"
        else np.asarray([0.0, 0.0, 1.0])
    )


def execute(node):
    """Grow the vines. Reactive: any param change, or an upstream brush stroke, re-runs this."""
    stage = node.get_stage()

    _sync_params(node)

    layer = node.get_edit_context()
    if not layer:
        return

    root_path = (node.get_param("vine_path").value or "").strip() or "/Vine"
    where = (root_path, Sdf.Path(root_path).AppendChild(CURVES_PRIM),
             Sdf.Path(root_path).AppendChild(LEAVES_PRIM))
    _publish(node, "out_curves", str(where[1]))
    _publish(node, "out_leaves", str(where[2]))

    paths = node.get_param("surface_prim").resolve_paths()
    if not paths:
        _bail(node, stage, layer, where, "pick a surface to grow on")
        return

    # Read from a composition that excludes this chunk's own layer, the same way Scatter Points
    # does: a chunk wired *after* this one may be editing the same mesh, and its edit is not
    # part of what this cook is growing on.
    upstream = chunk_stage.get_upstream_stage(stage, node.get_scene(), node)
    field = (node.get_param("density_primvar").value or "").strip()
    surface = _collect_surface(upstream.GetPrimAtPath(paths[0]), field).painted()
    if not surface:
        _bail(node, stage, layer, where, "nothing painted — paint the surface with a Scatter Brush")
        return

    settings = _settings(node, surface)
    settings["up"] = _up_axis(stage)
    rng = np.random.default_rng(int(node.get_param("seed").value or 0))

    sample_count = _surface_sample_count(surface, settings)
    positions, _sn, _sw, triangle_of = _sample(surface, sample_count, rng, weighted=False)
    if not len(positions):
        _bail(node, stage, layer, where, "nothing painted — paint the surface with a Scatter Brush")
        return
    # Two cells have to reach a sample from anywhere on the surface. Normally that is the step
    # size; on a region big enough to hit the sample ceiling the samples are further apart than
    # the steps are, and sizing to the step would leave projections landing in empty cells.
    spacing = math.sqrt(float(surface.area.sum()) / max(sample_count, 1))
    projector = _Projector(surface, positions, triangle_of, max(settings["step"], spacing) * 2.0)

    wanted = _attractor_count(surface, settings)
    attractors, attractor_normals, weights, _ai = _sample(surface, wanted * 2, rng, weighted=True)
    # Thinned by the paint a second time, per sample rather than per triangle. Choosing the
    # triangle by weight already puts the attractors in the painted region; this is what softens
    # its *edge*, where one corner of a face is painted and another is not.
    keep = np.flatnonzero(rng.random(len(attractors)) < weights)[:wanted]
    attractors, attractor_normals = attractors[keep], attractor_normals[keep]
    if not len(attractors):
        _bail(node, stage, layer, where, "nothing painted — paint the surface with a Scatter Brush")
        return

    # Every root is a node, and the vine needs room past them to grow.
    roots, roots_note = _painted_roots(
        upstream, (node.get_param("seed_points").value or "").strip(), projector,
        limit=max(1, settings["max_nodes"] // 4),
    )
    if roots_note and not roots:
        _bail(node, stage, layer, where, roots_note)
        return
    hand_placed = bool(roots)
    if not roots:
        # Spread across the painted region, not merely out of each other's reach. Spacing them
        # by the influence radius put all eight roots of a boulder in its lowest few
        # centimetres, and a vine cannot jump from there to the far side of the paint -- so one
        # half grew and the other stayed bare. Sharing the region out between them is what
        # "five vines" is asking for.
        spread = max(settings["influence"], surface.diagonal() / (settings["roots"] + 1))
        chosen = _choose_roots(attractors, settings["up"], settings["roots"], spread)
        roots = [(attractors[i], attractor_normals[i]) for i in chosen]
    if not roots:
        _bail(node, stage, layer, where, "no root found on the painted surface")
        return

    nodes = _grow(roots, attractors, projector, settings, rng)
    grown = len(nodes)
    nodes = _prune(nodes, settings["min_branch"])
    strands = _strands(nodes)
    if not strands:
        _bail(node, stage, layer, where, "nothing grew — try a larger Influence or more Density")
        return

    # Smooth, put back on the surface, then lift -- in that order, so every control point ends
    # up exactly its own radius off the skin. Widths come first because the lift is measured in
    # radii; see `_lift`.
    radii = _radii(nodes, settings["base_width"], settings["base_width"] * settings["taper"])
    _smooth_nodes(nodes, strands, settings["smooth"])
    _resurface(nodes, projector)
    _lift(nodes, radii, settings["lift"])

    leaves = (
        _leaf_points(strands, nodes.position, nodes.normal, radii, settings["leaf_spacing"])
        if settings["leaves"]
        else ([], [])
    )

    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        UsdGeom.Scope.Define(stage, where[0])
        _author_curves(stage, where[1], strands, nodes, radii, settings, node)
        _author_leaves(stage, where[2], leaves, settings, node)

    hit_cap = grown >= settings["max_nodes"]
    _report(
        node,
        f"{len(roots)} vine{'s' if len(roots) != 1 else ''}"
        f"{' from painted points' if hand_placed else ''} · "
        f"{len(strands)} branches · {len(leaves[0])} leaves"
        + (f"\ncapped at {settings['max_nodes']} points — raise Max Points" if hit_cap else "")
        + (f"\n{roots_note}" if roots_note else ""),
    )


def _bail(node, stage, layer, where, message):
    """Say why nothing grew, and leave the two output prims standing.

    Empty, but present. A cook that authored nothing left the Vine Curves and Leaf Points
    outputs pointing at prims that did not exist, so anything wired downstream resolved to
    nothing at all rather than to an empty result -- and un-painting a surface would break the
    graph below it rather than emptying it.
    """
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        UsdGeom.Scope.Define(stage, where[0])
        curves = UsdGeom.BasisCurves.Define(stage, where[1])
        curves.CreateCurveVertexCountsAttr().Set(Vt.IntArray())
        curves.CreatePointsAttr().Set(Vt.Vec3fArray())
        curves.CreateWidthsAttr().Set(Vt.FloatArray())
        cloud = UsdGeom.Points.Define(stage, where[2])
        cloud.CreatePointsAttr().Set(Vt.Vec3fArray())
        cloud.CreateNormalsAttr().Set(Vt.Vec3fArray())
    _report(node, message)


def _author_curves(stage, path, strands, nodes, radii, settings, node):
    """One BasisCurves holding every strand."""
    smooth = settings["form"] == "Smooth"
    counts, points, widths = [], [], []
    for chain in strands:
        # Already smoothed and re-seated on the surface, back when the normals could still be
        # refreshed to match -- see `_smooth_nodes`.
        chain_points = nodes.position[chain]
        chain_widths = np.asarray([radii[i] * 2.0 for i in chain], dtype=np.float64)
        if smooth:
            chain_points, chain_widths = _pin(chain_points, chain_widths)
        counts.append(len(chain_points))
        points.append(chain_points)
        widths.append(chain_widths)

    all_points = np.concatenate(points)
    all_widths = np.concatenate(widths)

    curves = UsdGeom.BasisCurves.Define(stage, path)
    curves.CreateTypeAttr().Set(UsdGeom.Tokens.cubic if smooth else UsdGeom.Tokens.linear)
    if smooth:
        curves.CreateBasisAttr().Set(UsdGeom.Tokens.bspline)
    curves.CreateWrapAttr().Set(UsdGeom.Tokens.nonperiodic)
    curves.CreateCurveVertexCountsAttr().Set(Vt.IntArray([int(c) for c in counts]))
    curves.CreatePointsAttr().Set(
        Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(all_points, dtype=np.float32))
    )
    curves.CreateWidthsAttr().Set(
        Vt.FloatArray.FromNumpy(np.ascontiguousarray(all_widths, dtype=np.float32))
    )
    curves.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
    colour = node.get_param("vine_color").value or (0.18, 0.42, 0.15)
    UsdGeom.Primvar(curves.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
    curves.GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*colour)]))
    _set_extent(curves, all_points, float(all_widths.max()) if len(all_widths) else 0.0)


def _author_leaves(stage, path, leaves, settings, node):
    """The leaf cloud, in the shape Scatter Points already reads.

    Authored even when it is empty, so the Leaf Points output resolves to a real prim the moment
    the chunk exists -- the same reason the brush publishes its points path before anything has
    been painted. Wiring an output is how you decide to use it; it cannot require having used it.
    """
    points, normals = leaves
    cloud = UsdGeom.Points.Define(stage, path)
    cloud.CreatePointsAttr().Set(Vt.Vec3fArray(points))
    cloud.CreateNormalsAttr().Set(Vt.Vec3fArray(normals))
    cloud.CreateWidthsAttr().Set(Vt.FloatArray([settings["leaf_size"]] * len(points)))
    colour = node.get_param("leaf_color").value or (0.35, 0.62, 0.22)
    UsdGeom.Primvar(cloud.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
    cloud.GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*colour)]))
    if points:
        _set_extent(cloud, np.asarray([[p[0], p[1], p[2]] for p in points]), settings["leaf_size"])


def _set_extent(gprim, points, width):
    """A bound that accounts for the width, since a curve is drawn fatter than its control points."""
    if not len(points):
        return
    pad = width * 0.5
    low = points.min(axis=0) - pad
    high = points.max(axis=0) + pad
    gprim.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*low), Gf.Vec3f(*high)]))


def _publish(node, name, value):
    param = node.get_param(name)
    if param is not None and param.value != value:
        param.value = value


def _report(node, text):
    if readout := node.get_param("readout"):
        readout.value = text
    logger.debug("%s", text.replace(chr(10), " | "))


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------

_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def _sync_params(node):
    """Hide the controls that cannot affect the result as things are wired.

    A control that does nothing is worse than an absent one -- it invites you to reach for it
    when something looks wrong. Scatter Points hides its distribution settings the moment a
    painted cloud is wired in, for the same reason and by the same rule.
    """
    _show(node, "root_count", not (node.get_param("seed_points").value or "").strip())
    _show(node, "step", not node.get_param("auto_step").value)
    leaves = bool(node.get_param("leaves").value)
    _show(node, "leaf_spacing", leaves)
    _show(node, "leaf_size", leaves)
    _show(node, "leaf_color", leaves)


def _show(node, name, visible):
    param = node.get_param(name)
    if param is None:
        return
    if visible:
        param.unset_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        param.set_state(chunk.ChunkFlags.State.HIDDEN)


def _wiring_changed(owner, notice, sender):
    _sync_params(owner.get_chunk() or owner)


def main():
    chunk_api = chunk.Chunk("Vine")

    chunk_api.add_param(
        chunk.LineEdit(
            "surface_prim",
            "Surface Mesh",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="The mesh the vines climb — or any prim above one. Point this and the\n"
            "Scatter Brush at the same thing.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "density_primvar",
            "Density Field",
            flags=_PF.LABELED,
            tooltip="Which painted field says where vines may grow. Wire this from a Scatter\n"
            "Brush's Density Field output. Empty means no field — grow over the whole mesh.",
        )
    )
    seeds = chunk.LineEdit(
        "seed_points",
        "Painted Points",
        flags=_PF.LABELED,
        tooltip="Start one vine at each point a Scatter Brush placed, instead of choosing\n"
        "roots automatically. Wire this from the brush's Painted Points output.",
    )
    seeds.add_callback(_wiring_changed)
    chunk_api.add_param(seeds)
    chunk_api.add_param(
        chunk.LineEdit(
            "vine_path",
            "Vine Path",
            default_value="/Vine",
            tooltip="USD path of the scope holding the result. The curves land at\n"
            "<path>/Curves and the leaf points at <path>/Leaves.",
        )
    )
    chunk_api.add_param(
        chunk.Label(
            "readout",
            # The widget paints the label over the value at construction, so the starting text
            # has to be the label; the value takes over from the first cook.
            "nothing grown yet",
            default_value="nothing grown yet",
            tooltip="What the last cook produced.",
        )
    )

    # --- the two outputs ---
    chunk_api.add_param(
        chunk.LineEdit(
            "out_curves",
            "Vine Curves",
            flags=_PF.OUTPUT | _PF.LABELED,
            tooltip="The BasisCurves this chunk grew.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "out_leaves",
            "Leaf Points",
            flags=_PF.OUTPUT | _PF.LABELED,
            tooltip="A point per leaf, with the surface normal at each. Wire it into a Scatter\n"
            "Points chunk's Painted Points input to instance real leaf geometry along the vine.",
        )
    )

    # --- growth ---
    grow = chunk.Group("grow_grp", "Growth", icon="sprout", flags=_GF)
    auto = chunk.Checkbox(
        "auto_step",
        "Auto Step",
        default_value=True,
        flags=_PF.LABELED,
        tooltip="Size the step from the painted region, so a vine is made of about the same\n"
        "number of points whatever the scene scale. Untick to set it in scene units.",
    )
    auto.add_callback(_wiring_changed)
    grow.add_param(auto)
    grow.add_param(
        chunk.Slider(
            "step",
            "Step",
            min_value=0.001,
            max_value=100.0,
            default_value=1.0,
            flags=_PF.LABELED,
            tooltip="Distance between points on a vine, in scene units — how far apart the\n"
            "growth makes its decisions. It changes how smoothly a vine curves, not how thick\n"
            "one is: that is Thickness, under Shape.",
        )
    )
    grow.add_param(
        chunk.Slider(
            "density",
            "Density",
            min_value=0.0,
            max_value=1.0,
            default_value=0.5,
            flags=_PF.LABELED,
            tooltip="How thickly the vines fill what was painted. Low leaves gaps between them,\n"
            "high packs them against each other.",
        )
    )
    grow.add_param(
        chunk.Slider(
            "climb",
            "Climb",
            min_value=0.0,
            max_value=2.0,
            default_value=0.35,
            flags=_PF.LABELED,
            tooltip="Pull toward the stage's up axis — the plant's phototropism. Zero lets the\n"
            "vines wander the painted region evenly; high sends them straight up a wall.",
        )
    )
    grow.add_param(
        chunk.Slider(
            "wander",
            "Wander",
            min_value=0.0,
            max_value=1.0,
            default_value=0.15,
            flags=_PF.LABELED,
            tooltip="Randomness in each step. A little keeps two vines from growing the same\n"
            "shape; a lot turns them into scribble.",
        )
    )
    grow.add_param(
        chunk.Spinbox(
            "root_count",
            "Roots",
            min_value=1,
            default_value=5,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="How many vines to start, lowest-first across the painted region. Ignored\n"
            "while Painted Points is wired — then every dab is a root.",
        )
    )
    grow.add_param(
        chunk.Spinbox(
            "min_branch",
            "Min Branch",
            min_value=1,
            max_value=40,
            default_value=5,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="Shortest side branch to keep, in steps. Colonization sprouts a stub\n"
            "wherever a node briefly had an attractor to itself; low leaves those in and the\n"
            "vine reads as fuzz, high leaves only the long runs.",
        )
    )
    grow.add_param(
        chunk.Spinbox(
            "seed",
            "Seed",
            default_value=0,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="Change it for a different vine from the same paint.",
        )
    )
    chunk_api.add_param(grow)

    # --- shape ---
    shape = _collapsed(chunk.Group("shape_grp", "Shape", icon="curve", flags=_GF))
    shape.add_param(
        chunk.Menu(
            "curve_form",
            "Form",
            menu_items=_FORMS,
            default_value="Smooth",
            tooltip="Smooth is a cubic B-spline through the grown points; Linear is the\n"
            "polyline itself, which is cheaper and shows exactly where it grew.",
        )
    )
    shape.add_param(
        chunk.Slider(
            "thickness",
            "Thickness %",
            min_value=0.05,
            max_value=5.0,
            default_value=1.0,
            flags=_PF.LABELED,
            tooltip="Width at the base of a vine, as a percentage of the painted region — so it\n"
            "means the same on a doorway and on a cliff face. Everything else you can see (lift\n"
            "off the wall, leaf spacing and size) is a multiple of this, not of Step.",
        )
    )
    shape.add_param(
        chunk.Slider(
            "taper",
            "Taper",
            min_value=0.05,
            max_value=1.0,
            default_value=0.25,
            flags=_PF.LABELED,
            tooltip="How thin a tip gets, as a fraction of Thickness. One is a constant width.",
        )
    )
    shape.add_param(
        chunk.Slider(
            "lift",
            "Lift",
            min_value=0.0,
            max_value=4.0,
            default_value=1.1,
            flags=_PF.LABELED,
            tooltip="Where the vine rests, as a multiple of its own radius — so Thickness makes\n"
            "it fatter in place rather than pushing it off the mesh. 1.0 sits the tube exactly\n"
            "on the surface, below that it sinks in, above that it floats.",
        )
    )
    shape.add_param(
        chunk.Spinbox(
            "smooth",
            "Smoothing",
            min_value=0,
            max_value=10,
            default_value=2,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="Passes of corner-rounding over each strand, ends pinned.",
        )
    )
    chunk_api.add_param(shape)

    # --- leaves ---
    leaf = _collapsed(chunk.Group("leaf_grp", "Leaves", icon="sprout", flags=_GF))
    leaves = chunk.Checkbox(
        "leaves",
        "Leaf Points",
        default_value=True,
        flags=_PF.LABELED,
        tooltip="Emit a point per leaf along the vines. The points are what a Scatter Points\n"
        "chunk instances — this chunk grows no leaf geometry of its own.",
    )
    leaves.add_callback(_wiring_changed)
    leaf.add_param(leaves)
    leaf.add_param(
        chunk.Slider(
            "leaf_spacing",
            "Spacing",
            min_value=1.0,
            max_value=40.0,
            default_value=3.0,
            flags=_PF.LABELED,
            tooltip="Distance along a vine between leaves, as a multiple of Thickness — about\n"
            "three stem-widths apart by default, which is roughly how ivy sets them.",
        )
    )
    leaf.add_param(
        chunk.Slider(
            "leaf_size",
            "Point Size",
            min_value=0.25,
            max_value=20.0,
            default_value=1.5,
            flags=_PF.LABELED,
            tooltip="How big the leaf points are drawn, as a multiple of Thickness. Display\n"
            "only — the size of an instanced leaf is the Scatter Points chunk's business.",
        )
    )
    chunk_api.add_param(leaf)

    # --- advanced ---
    advanced = _collapsed(chunk.Group("advanced_grp", "Advanced", icon="sliders", flags=_GF))
    advanced.add_param(
        chunk.Slider(
            "influence",
            "Influence",
            min_value=1.0,
            max_value=40.0,
            default_value=12.0,
            flags=_PF.LABELED,
            tooltip="How far a node reaches for attractors, as a multiple of Step. Larger makes\n"
            "long, purposeful runs; smaller makes a dense, wandering mat.",
        )
    )
    advanced.add_param(
        chunk.Slider(
            "kill",
            "Kill Distance",
            min_value=0.5,
            max_value=10.0,
            default_value=2.0,
            flags=_PF.LABELED,
            tooltip="How close a vine has to come before that part of the paint counts as\n"
            "covered, as a multiple of Step. It sets how far apart branches settle.",
        )
    )
    advanced.add_param(
        chunk.Spinbox(
            "max_points",
            "Max Points",
            min_value=16,
            max_value=_MAX_NODES,
            default_value=6000,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="Safety cap on the whole network. The readout says when it has been hit.",
        )
    )
    chunk_api.add_param(advanced)

    # --- display ---
    display = _collapsed(chunk.Group("display_grp", "Display", icon="eye", flags=_GF))
    display.add_param(
        chunk.ColorPicker("vine_color", "Vine Color", default_value=(0.18, 0.42, 0.15))
    )
    display.add_param(
        chunk.ColorPicker("leaf_color", "Leaf Color", default_value=(0.35, 0.62, 0.22))
    )
    chunk_api.add_param(display)

    return chunk_api


def _collapsed(group):
    """Collapse a group by default -- the chunk is tall, and growth is what people reach for."""
    group.set_state(chunk.ChunkFlags.State.COLLAPSED)
    return group


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="sprout",
        # Growth is array work over thousands of nodes and attractors -- see `_grow`.
        requirements=["numpy>=2,<3"],
        description="""
        Vine — grows climbing vines across the part of a mesh a Scatter Brush
        painted.

        Wire a Scatter Brush's Density Field output into this chunk's Density
        Field input, with both pointed at the same mesh, and vines fill exactly
        what was painted. Wire its Painted Points output into Painted Points as
        well and every dab becomes the root of its own vine.

        The result is two prims: a BasisCurves of the vines, and a Leaf Points
        cloud carrying a surface normal per point. That cloud is the same shape
        the brush publishes, so feeding it to a Scatter Points chunk's Painted
        Points input instances real leaf geometry along the vine, laid flat on
        the surface.

        Parameters:
          - Surface Mesh: the mesh the vines climb.
          - Auto Step / Step: the length of one growth step — how smoothly a
            vine curves. It does not change how thick one is.
          - Density: how thickly the vines fill what was painted.
          - Climb: pull toward the up axis — the plant's phototropism.
          - Wander: randomness per step.
          - Roots: how many vines to start when no painted points are wired.
          - Thickness %: width at the base, as a share of the painted region,
            so the defaults hold at any scene scale. Lift, leaf spacing and
            leaf size are all multiples of it.
          - Taper: how thin a tip gets, as a fraction of Thickness.

        Growth is space colonization (Runions et al., 2007) constrained to the
        surface, with the momentum, phototropism and randomness of Luft's ivy
        generator. Vines never leave the mesh, and never leave the paint.
        """,
    )
