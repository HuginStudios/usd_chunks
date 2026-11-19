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
* ``Leaves`` -- a guide-purpose ``Points`` cloud with a surface normal per point, which is
  deliberately the same shape a Scatter Brush publishes. Wire it into a **Scatter Points** chunk's *Painted
  Points* input and its prototypes are instanced along the vine, laid flat on the wall. Leaf
  geometry is somebody's asset, not something to generate.

Requires: pxr (OpenUSD), usd_chunks, numpy (growth is array work over thousands of nodes).
"""

from __future__ import annotations

import hashlib
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

#: Per-point primvar on the curves: when the point grew, 0 at the roots to 1 at the last tip.
GROWTH_PRIMVAR = "growth"

_FORMS = ["Smooth", "Linear"]

#: How much of the previous step's direction is carried into the next one.
#:
#: Not a parameter, deliberately. It is the difference between a vine and a ball of wire, not a
#: look to dial: at zero the colonization step turns freely and the result reads as scribble;
#: much past one it overwhelms the attractors and the vine walks straight out of the paint.
#: This sits where a vine stops looking like a random walk and still goes where it is sent.
_MOMENTUM = 0.6

#: The most children one node grows. After that its attractors stop pulling on it.
#:
#: Momentum and climb can outweigh the pull of attractors that lie behind a node. Its child
#: then lands to the side, no nearer to them, so they still pick the same node next step,
#: and it sprouts again -- on a sphere, 50 to 80 stubs from one node, and a dense rock spent
#: a third of its 20,000 points on one. A vine forks into two or three; four leaves room.
_MAX_CHILDREN = 4

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

    @classmethod
    def join(cls, parts):
        """Several surfaces as one."""
        parts = [part for part in parts if len(part)]
        if not parts:
            return cls.empty()
        if len(parts) == 1:
            return parts[0]
        return cls(*(
            np.concatenate([getattr(part, name) for part in parts])
            for name in ("v0", "v1", "v2", "n", "w0", "w1", "w2")
        ))

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

    return _Surface.join(parts)


def _outermost(paths):
    """`paths` without any that sit under another one, which would add its meshes twice."""
    picked = [Sdf.Path(path) for path in paths]
    return [str(path) for path in picked
            if not any(other != path and path.HasPrefix(other) for other in picked)]


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


#: Up to this many points are projected as one padded block; see `_Projector.project_many`.
_ONE_BLOCK = 32

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
    # np.add.reduce is what np.sum calls, without the wrapper -- a third of the cost of a
    # small array's dot, and the projection takes six of them every growth step.
    return np.add.reduce(a * b, axis=-1)


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
    # One errstate for the whole test rather than one per division: entering it costs about
    # what the arithmetic on a growth step's few dozen triangles does.
    with np.errstate(divide="ignore", invalid="ignore"):
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

        inv = np.where(np.abs(denom) > 1e-20, 1.0 / denom, 0.0)
        out = v0 + ab * (vb * inv)[..., None] + ac * (vc * inv)[..., None]
        # A degenerate triangle has no interior to land in; its first corner is as good as any.
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
    """numerator / denominator clamped to 0..1, and 0 where the denominator is ~0.

    The caller holds the errstate that silences the division by zero `np.where` throws away.
    """
    ratio = np.where(np.abs(denominator) > 1e-20, numerator / denominator, 0.0)
    return np.minimum(np.maximum(ratio, 0.0), 1.0)


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

    def lowest(self, up):
        """How far along `up` the lowest corner of the surface is."""
        s = self._surface
        if not len(s):
            return -np.inf
        return float(min((s.v0 @ up).min(), (s.v1 @ up).min(), (s.v2 @ up).min()))

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
        # padded column can only tie with a real answer and never beat one. A big batch goes
        # in blocks of about the same length, shortest first: one block padded every row to
        # the longest, about twice the work. A growth step's handful of points goes as one
        # block, where sorting them into blocks cost more than the padding it saved. Each
        # row's answer is the same either way.
        lengths = lengths[live]
        if len(live) <= _ONE_BLOCK:
            self._answer(points, per_point, live, lengths, landed, normals)
        else:
            by_length = np.argsort(lengths, kind="stable")
            sorted_lengths = lengths[by_length]
            start = 0
            while start < len(by_length):
                stop = int(np.searchsorted(sorted_lengths, sorted_lengths[start] * 1.25, side="right"))
                stop = max(stop, start + 1)
                block = by_length[start:stop]
                self._answer(points, per_point, live[block], lengths[block], landed, normals)
                start = stop
        found[live] = True
        return landed, normals, found

    def _answer(self, points, per_point, rows_live, block_lengths, landed, normals):
        """Project one block of rows, writing into `landed` and `normals`."""
        s = self._surface
        width = int(block_lengths.max())
        flat = per_point[rows_live[0]] if len(rows_live) == 1 else np.concatenate([per_point[i] for i in rows_live])
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

    `born` is the growth step each node was made in, 0 for the roots. It is the vine's clock:
    Growth % keeps the nodes born before a cut-off, and the `growth` primvar is it, scaled.

    `hanging` marks the nodes of a tendril hanging in the air. They are not snapped back onto
    the surface or lifted off it; their normal is the one their tip left the surface with,
    which is the way their leaves face.
    """

    __slots__ = ("parent", "_position", "_normal", "_direction", "_born", "_hanging", "_count")

    def __init__(self, capacity=256):
        capacity = max(int(capacity), 8)
        self.parent: list[int] = []
        self._position = np.zeros((capacity, 3), dtype=np.float64)
        self._normal = np.zeros((capacity, 3), dtype=np.float64)
        self._direction = np.zeros((capacity, 3), dtype=np.float64)
        self._born = np.zeros(capacity, dtype=np.int64)
        self._hanging = np.zeros(capacity, dtype=bool)
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

    @property
    def born(self):
        return self._born[: self._count]

    @property
    def hanging(self):
        return self._hanging[: self._count]

    def _reserve(self, extra):
        capacity = len(self._position)
        if self._count + extra <= capacity:
            return
        while capacity < self._count + extra:
            capacity *= 2
        for name in ("_position", "_normal", "_direction", "_born", "_hanging"):
            old = getattr(self, name)
            grown = np.zeros((capacity,) + old.shape[1:], dtype=old.dtype)
            grown[: self._count] = old[: self._count]
            setattr(self, name, grown)

    def add(self, position, parent, normal, direction, born=0):
        self._reserve(1)
        index = self._count
        self._position[index] = position
        self._normal[index] = normal
        self._direction[index] = direction
        self._born[index] = born
        self.parent.append(int(parent))
        self._count += 1
        return index

    def add_many(self, positions, parents, normals, directions, born=0, hanging=False):
        """Append a whole growth iteration at once; the indices they were given.

        `born` is the growth step and `hanging` the tendril flag, one for all or one each.
        """
        count = len(positions)
        if not count:
            return np.zeros(0, dtype=np.int64)
        self._reserve(count)
        start = self._count
        self._position[start:start + count] = positions
        self._normal[start:start + count] = normals
        self._direction[start:start + count] = directions
        self._born[start:start + count] = born
        self._hanging[start:start + count] = hanging
        self.parent.extend(int(p) for p in parents)
        self._count += count
        return np.arange(start, start + count)

    def copy(self):
        """A copy whose positions and normals can be moved without touching this one."""
        return self.subset(np.ones(self._count, dtype=bool))

    def subset(self, keep):
        """The nodes where `keep` is True, as a new skeleton, parents renumbered.

        Every kept node's parent must be kept too, which holds for anything cut by age or
        by pruning: a parent is older than its child and is on the path to it.
        """
        index = np.flatnonzero(keep)
        renumber = np.full(self._count + 1, -1, dtype=np.int64)
        renumber[index] = np.arange(len(index))
        parents = np.asarray(self.parent, dtype=np.int64)[index]
        twin = _Nodes(len(index))
        twin.add_many(self.position[index], renumber[parents], self.normal[index],
                      self.direction[index], self.born[index], self.hanging[index])
        return twin

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


#: Meander's wavelength, in growth steps: how far a vine goes before it swings back.
_MEANDER_WAVELENGTH = 24.0


def _value_noise(points, seed):
    """Smooth noise in -1..1 over 3D space, one value per point; about one swing per unit.

    Value noise on an integer lattice, blended with smoothstep: cheap, the same everywhere a
    point lands whatever order it is asked in, and smooth, so neighbouring steps of one vine
    turn the same way. Per-step random noise (Wander) cannot do that -- it averages out over a
    few steps, which leaves a straight vine with a jitter on it.
    """
    points = np.asarray(points, dtype=np.float64)
    cell = np.floor(points)
    frac = points - cell
    fade = frac * frac * (3.0 - 2.0 * frac)
    base = cell.astype(np.int64)
    total = np.zeros(len(points))
    for corner in range(8):
        offset = np.array([(corner >> 0) & 1, (corner >> 1) & 1, (corner >> 2) & 1])
        lattice = base + offset
        # A 64-bit integer hash of the lattice point and the seed, folded to 0..1.
        h = (lattice[:, 0] * 73856093) ^ (lattice[:, 1] * 19349663) ^ (lattice[:, 2] * 83492791)
        h = (h ^ (seed * 2654435761)) & 0xFFFFFFFF
        h = (h * 2246822519) & 0xFFFFFFFF
        h ^= h >> 13
        value = (h & 0xFFFF) / 32767.5 - 1.0
        weight = np.prod(np.where(offset == 1, fade, 1.0 - fade), axis=1)
        total += value * weight
    return total


def _meander(directions, normals, positions, amount, wavelength, seed):
    """Turn each direction about its surface normal by a smoothly varying angle.

    The angle comes from `_value_noise` over space, so a vine swings one way for a while and
    then the other, and two vines that pass the same spot bend alike, as if following the
    wall's grain. At 1 the turn reaches about 70 degrees either way.
    """
    if amount <= 0.0 or not len(directions):
        return directions
    angle = _value_noise(positions / max(wavelength, 1e-9), seed) * amount * 1.2
    turned = np.cross(normals, directions)
    return directions * np.cos(angle)[:, None] + turned * np.sin(angle)[:, None]


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


def _choose_roots(points, up, count, spacing, normals=None):
    """Which attractors vines start from when nobody painted the roots by hand.

    Lowest first, then spread: a vine that starts at the bottom of what was painted and climbs
    is the picture people have of ivy, and starting them all in one corner -- which picking at
    random cheerfully does -- reads as one vine, not five. Spacing is relaxed rather than
    enforced, so a narrow painted strip still gets the roots it was asked for.

    Faces that look straight down go last. The lowest paint on a wall or a rock is usually
    its underside, sitting on the ground: a brush stroke along the foot of a wall reaches it,
    and vines rooted there grew along the buried face, out of sight.

    Indices, not positions, so the caller can take the normal it already sampled alongside each
    attractor rather than projecting thousands of candidates to find five.
    """
    if not len(points) or count <= 0:
        return []
    height = points @ up
    if normals is not None:
        underside = (np.asarray(normals) @ up) < -0.7
        order = np.lexsort((height, underside))
    else:
        order = np.argsort(height)
    chosen: list[int] = []
    taken: set[int] = set()
    for gap in (spacing, spacing * 0.25, 0.0):
        for i in order.tolist():
            if len(chosen) >= count:
                break
            if i in taken or (gap > 0.0 and chosen and _too_near(points, i, chosen, gap)):
                continue
            chosen.append(i)
            taken.add(i)
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
    # Whether each attractor pulls right now, and the unit vector from its nearest node to it.
    # Both change only when its nearest node does, so they are kept rather than rebuilt.
    pulls = np.zeros(len(attractors), dtype=bool)
    towards = np.zeros((len(attractors), 3), dtype=np.float64)
    attractor_sq = _dot(attractors, attractors)
    pending = np.arange(len(nodes))
    # Children per node, and the nodes that have grown all they may; see _MAX_CHILDREN. One
    # spare slot at the end, so the -1 of an attractor with no nearest node reads as open.
    slots = max(len(nodes), settings["max_nodes"]) + 1
    children = np.zeros(slots, dtype=np.int64)
    closed = np.zeros(slots, dtype=bool)

    up = settings["up"]
    step = settings["step"]
    influence_sq = settings["influence"] ** 2
    kill_sq = settings["kill"] ** 2
    climb = settings["climb"]
    wander = settings["wander"]
    meander = settings.get("meander", 0.0)

    # A new node can only change an attractor within reach of it: one further away than both
    # the influence and the kill distance is neither pulled nor consumed by it. So an
    # iteration looks only at the attractors in the cells around its new nodes, not at all of
    # them. An attractor whose nearest node is within reach gets the same nearest node a
    # search over everything would give it, so the vine is the same.
    grid = _PointGrid(attractors, max(settings["influence"], settings["kill"]))

    step_count = 0
    while len(pending) and len(nodes) < settings["max_nodes"]:
        step_count += 1
        fresh = nodes.position[pending]
        # Neighbourhoods of nodes in nearby cells overlap. A mask removes the repeats and
        # sorts what is left in one pass, several times faster than np.unique on thousands.
        seen = np.zeros(len(attractors), dtype=bool)
        for key in np.unique(grid.keys_of(fresh)).tolist():
            seen[grid.near_key(key)] = True
        live = np.flatnonzero(seen & alive)

        if len(live):
            # |a - f|^2 expanded, so the distances are one matrix product rather than an
            # (attractors x new nodes x 3) difference built before it is reduced. Clipped at
            # zero: the expansion can go a few ulps negative where a node sits exactly on an
            # attractor, and would then lose the comparison against the kill distance.
            distance_sq = np.maximum(
                attractor_sq[live][:, None] - 2.0 * (attractors[live] @ fresh.T) + _dot(fresh, fresh),
                0.0,
            )
            closest = np.argmin(distance_sq, axis=1)
            best_sq = distance_sq[np.arange(len(live)), closest]
            better = best_sq < nearest_sq[live]
            if better.any():
                improved = live[better]
                owner = pending[closest[better]]
                nearest_sq[improved] = best_sq[better]
                nearest[improved] = owner
                unit = attractors[improved] - nodes.position[owner]
                length = np.linalg.norm(unit, axis=1)
                real = length > 1e-9
                unit[real] /= length[real, None]
                towards[improved] = unit

            # Reached: an attractor a node has come this close to has done its job.
            alive[live[nearest_sq[live] <= kill_sq]] = False
            pulls[live] = alive[live] & (nearest_sq[live] <= influence_sq) & ~closed[nearest[live]]

        pulling = np.flatnonzero(pulls)
        if not len(pulling):
            break

        # Sum of unit directions, per node that has any. Summed into one row per owner rather
        # than one per node: zeroing a row for every node in the vine, every iteration, cost
        # as much as the sums. np.add.at adds in the same order either way.
        owners, owner_of = np.unique(nearest[pulling], return_inverse=True)
        pull = np.zeros((len(owners), 3), dtype=np.float64)
        np.add.at(pull, owner_of, towards[pulling])

        # The whole iteration steps at once: one direction array, one projection, one append.
        # Per node this was three numpy calls, an rng draw and a `project` -- 310us of call
        # overhead each against a median of 21 candidate triangles, which was most of the cook.
        room = settings["max_nodes"] - len(nodes)
        owners, direction = owners[:room], pull[:room]
        length = np.linalg.norm(direction, axis=1)
        moving = length > 1e-9
        owners, direction = owners[moving], direction[moving] / length[moving, None]
        if not len(owners):
            break

        direction = direction + nodes.direction[owners] * _MOMENTUM + up * climb
        if wander > 0.0:
            direction = direction + rng.normal(scale=wander, size=direction.shape)
        direction = _tangents(direction, nodes.normal[owners])
        if meander > 0.0:
            direction = _tangents(
                _meander(direction, nodes.normal[owners], nodes.position[owners], meander,
                         step * _MEANDER_WAVELENGTH, settings["seed"]),
                nodes.normal[owners],
            )

        landed, landed_normals, found = projector.project_many(
            nodes.position[owners] + direction * step
        )
        # Anything that did not land grew off the edge of what was painted, and that branch
        # ends there -- which is what keeps the vines inside the paint with no boundary test.
        reached = np.flatnonzero(found)
        pending = nodes.add_many(
            landed[reached], owners[reached], landed_normals[reached], direction[reached], step_count
        )

        # Owners are unique, so a plain += counts each one once.
        parents = owners[reached]
        children[parents] += 1
        full = parents[children[parents] >= _MAX_CHILDREN]
        if len(full):
            closed[full] = True
            pulls[pulling[closed[nearest[pulling]]]] = False

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

    # A parent is always kept when its child is, so the renumbering cannot miss.
    return nodes.subset(keep)


#: How hard a hanging tendril is pulled down each step, against carrying on the way it went.
_GRAVITY = 0.45


def _hang(nodes, projector, settings, seed):
    """Drop a tendril from some of the vine's tips: it leaves the surface and hangs.

    A share of the tips (Hang) each grow Hang Length further, first out from the surface and
    then down under gravity. A tendril that would pass into the painted surface is pushed back
    out onto it, so one that falls over a bulge drapes across it instead of through it. Past
    the painted region there is no surface to collide with, and it falls freely.

    Its nodes are born after the tip, so Growth % and the growth primvar grow them last.

    Args:
        nodes: The pruned skeleton.
        projector: The painted surface, for collisions.
        settings: From `_settings`: hang, hang_length, step and up are read.
        seed: The random seed; tendrils draw from their own stream, so turning Hang on does not
            change the vine they hang from.

    Returns:
        The skeleton with the tendrils added, or `nodes` itself when there are none.
    """
    if settings["hang"] <= 0.0 or settings["hang_length"] <= 0.0 or not len(nodes):
        return nodes
    rng = np.random.default_rng([seed, 1])
    parents = np.asarray(nodes.parent, dtype=np.int64)
    has_child = np.zeros(len(nodes), dtype=bool)
    has_child[parents[parents >= 0]] = True
    tips = np.flatnonzero(~has_child & (parents >= 0) & ~nodes.hanging)
    tips = tips[rng.random(len(tips)) < settings["hang"]]
    if not len(tips):
        return nodes

    step, up = settings["step"], settings["up"]
    # About the radius of a vine at the default Thickness. Not Thickness itself: that is a
    # drawing setting, and changing it must not grow the tendrils again.
    clearance = step
    steps = max(1, int(round(settings["hang_length"] / step)))
    away = nodes.normal[tips].copy()
    position = nodes.position[tips].copy()
    # Out from the wall first, with a touch of the way the tip was heading. More of the tip's
    # heading than that, and a tip that was climbing loops back over itself as it falls.
    direction = away + nodes.direction[tips] * 0.15
    direction /= np.maximum(np.linalg.norm(direction, axis=1), 1e-9)[:, None]
    parent = tips.copy()
    born = nodes.born[tips].copy()
    # The ground, near enough: nothing hangs below the lowest point of what it grows on. A
    # tendril reaching it stops, instead of falling on through the floor under the mesh.
    floor = projector.lowest(up)
    out = nodes.copy()
    for _ in range(steps):
        direction = direction + (-up) * _GRAVITY + rng.normal(scale=0.08, size=direction.shape)
        direction /= np.maximum(np.linalg.norm(direction, axis=1), 1e-9)[:, None]
        position = position + direction * step
        landed, normals, found = projector.project_many(position)
        inside = found & (_dot(position - landed, normals) < clearance)
        if inside.any():
            position[inside] = landed[inside] + normals[inside] * clearance
            # Slide along the surface instead of pressing into it.
            direction[inside] = direction[inside] - normals[inside] * np.minimum(
                _dot(direction[inside], normals[inside]), 0.0)[:, None]
        going = position @ up >= floor
        if not going.any():
            break
        position, direction, away = position[going], direction[going], away[going]
        parent, born = parent[going], born[going] + 1
        parent = out.add_many(position, parent, away, direction, born, True)
    return out


def _growth_of(nodes):
    """When each node grew, from 0 (the roots) to 1 (the last tip), as an array."""
    last = int(nodes.born.max()) if len(nodes) else 0
    return nodes.born / float(max(last, 1))


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
    # Not the hanging nodes: they are in the air already, and lifting them along a normal that
    # belongs to the wall they left would kink each tendril where it starts.
    lift = np.asarray(radii, dtype=np.float64) * factor * ~nodes.hanging
    nodes.position[:] += nodes.normal * lift[:, None]


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
    # A hanging tendril is in the air on purpose; snapping it back would glue it to the wall.
    reached = np.flatnonzero(found & ~nodes.hanging)
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


def _leaf_points(strands, positions, normals, radii, growth, settings):
    """Points along the vines at a fixed arc-length spacing, with the surface normal at each.

    Deliberately the same shape a Scatter Brush's Painted Points output has -- position plus
    normal -- so a Scatter Points chunk instances leaves along a vine with the code it already
    has, rather than this chunk growing leaf geometry nobody asked it for.

    Each leaf sits on the stem's surface (lifted by the local radius) and is pushed off to one
    side by Spread, alternating left and right along the strand the way ivy sets its leaves.
    Straight down the middle, every instanced leaf covered the stem it grew from. Bare Base
    drops the leaves on the oldest part of the vine, where real ivy has lost them.

    Args:
        strands: Index chains, from `_strands`.
        positions: (N, 3) node positions.
        normals: (N, 3) surface normals at the nodes.
        radii: A radius per node.
        growth: When each node grew, 0..1, from `_growth_of`.
        settings: From `_settings`: leaf_spacing, leaf_spread and leaf_bare are read.

    Returns:
        (points, normals, growth): (L, 3), (L, 3) and (L,) arrays.
    """
    spacing = settings["leaf_spacing"]
    found_points, found_normals, found_growth = [], [], []
    radii = np.asarray(radii, dtype=np.float64)
    bare = settings["leaf_bare"] * (float(growth.max()) if len(growth) else 0.0)
    for chain in strands if spacing > 0.0 else ():
        chain = np.asarray(chain)
        start, end = positions[chain[:-1]], positions[chain[1:]]
        segment = np.linalg.norm(end - start, axis=1)
        travelled = np.concatenate(([0.0], np.cumsum(segment)))
        distance = np.arange(1, int(travelled[-1] / spacing + 1e-9) + 1) * spacing
        if not len(distance):
            continue
        # The segment each leaf falls on: the first one whose far end reaches it. A
        # zero-length segment is never chosen, since the one before it reaches as far.
        on = np.clip(np.searchsorted(travelled, distance, side="left") - 1, 0, len(segment) - 1)
        t = np.clip((distance - travelled[on]) / np.maximum(segment[on], 1e-12), 0.0, 1.0)
        tip = chain[1:][on]
        normal = normals[tip]
        along = (end[on] - start[on]) / np.maximum(segment[on], 1e-12)[:, None]
        side = np.cross(normal, along)
        side /= np.maximum(np.linalg.norm(side, axis=1), 1e-12)[:, None]
        sign = np.where(np.arange(len(distance)) % 2 == 0, 1.0, -1.0)[:, None]
        at = start[on] + (end[on] - start[on]) * t[:, None]
        at = at + normal * radii[tip][:, None] + side * sign * settings["leaf_spread"]
        age = growth[chain[:-1]][on] * (1.0 - t) + growth[tip] * t
        keep = age >= bare
        found_points.append(at[keep])
        found_normals.append(normal[keep])
        found_growth.append(age[keep])
    if not found_points:
        return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0)
    return np.concatenate(found_points), np.concatenate(found_normals), np.concatenate(found_growth)


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
        "meander": number("meander"),
        "seed": int(number("seed")),
        "density": number("density"),
        "roots": max(1, int(number("root_count"))),
        "min_branch": max(1, int(number("min_branch"))),
        "max_nodes": max(2, min(_MAX_NODES, int(number("max_points")))),
        "base_width": base_width,
        "hang": number("hang") / 100.0,
        # A share of the painted region, like Thickness, so it reads the same at any scale.
        "hang_length": region * number("hang_length") / 100.0,
        "taper": number("taper"),
        # A multiple of each node's own radius, applied per node -- not a distance. See `_lift`.
        "lift": number("lift"),
        "smooth": max(0, int(number("smooth"))),
        "growth": min(max(number("growth") / 100.0, 0.0), 1.0),
        "animate": bool(node.get_param("animate").value),
        "start_frame": number("start_frame"),
        "end_frame": number("end_frame"),
        "form": node.get_param("curve_form").value,
        "leaves": bool(node.get_param("leaves").value),
        "leaf_spacing": base_width * number("leaf_spacing"),
        "leaf_spread": base_width * number("leaf_spread"),
        "leaf_bare": min(max(number("leaf_bare") / 100.0, 0.0), 1.0),
        "leaf_size": base_width * number("leaf_size"),
    }


def _seed_cloud(stage, points_path):
    """The points wired into Painted Points, read but not yet placed on the surface.

    Only a point cloud is read. A mesh has points too, and a mesh path wired here by mistake
    made every vertex a root: a scanned rock froze the session, then crashed it.

    Args:
        stage: The stage to read the points from.
        points_path: The Painted Points path.

    Returns:
        (points, problem): the points as an (N, 3) array, or None when there are none, and
        what to tell the user when the path is not a point cloud -- empty otherwise.
    """
    if not points_path or stage is None:
        return None, ""
    prim = stage.GetPrimAtPath(points_path)
    if not prim or not prim.IsValid():
        return None, ""
    if not prim.IsA(UsdGeom.Points):
        return None, f"Painted Points is a {prim.GetTypeName() or 'prim'}, not a point cloud: {points_path}"
    points = UsdGeom.Points(prim).GetPointsAttr().Get()
    if not points:
        return None, ""
    return np.asarray(points, dtype=np.float64).reshape(-1, 3), ""


def _painted_roots(cloud, projector, limit):
    """Roots the brush placed by hand, projected onto the surface.

    A dab lands on the skin already, but the brush records where the *cursor* hit and this
    chunk's idea of the surface is a sampled reconstruction of it -- so they are reconciled here
    rather than trusted to agree. A dab outside the painted region has nowhere to grow and is
    dropped, which is the same rule the growth itself follows.

    Args:
        cloud: The painted points, (N, 3), from `_seed_cloud`; None when there are none.
        projector: Puts each point on the painted surface.
        limit: The most roots to take. More are thinned evenly.

    Returns:
        (roots, note): the roots as (position, normal) pairs, and what to tell the user --
        empty when there is nothing to say.
    """
    if cloud is None or not len(cloud):
        return [], ""
    note = ""
    if len(cloud) > limit:
        note = f"used {limit} of {len(cloud)} painted points"
        cloud = cloud[[int(i) for i in np.linspace(0, len(cloud) - 1, limit)]]
    landed, normals, found = projector.project_many(cloud)
    return [(landed[i], normals[i]) for i in np.flatnonzero(found).tolist()], note


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


#: The last skeleton each Vine grew, by its layer identifier; see `_growth_key`.
_GROWN: dict = {}

#: The settings that change what grows. The rest only change how it is drawn.
_GROWTH_SETTINGS = ("step", "influence", "kill", "climb", "wander", "meander", "density", "roots",
                    "min_branch", "max_nodes", "hang", "hang_length")


class _Grown:
    """One cook's growth: the pruned skeleton before smoothing, and what the readout says.

    `seated` is the skeleton after smoothing and re-seating on the surface, for the Smoothing
    it was done at: (passes, positions, normals). Re-seating projects every node, and is most
    of what is left of a cook once the growth is reused.
    """

    __slots__ = ("key", "nodes", "projector", "roots", "roots_note", "hand_placed", "grown", "seated")

    def __init__(self, nodes, projector, roots, roots_note, hand_placed, grown):
        self.key = None
        self.nodes, self.projector = nodes, projector
        self.roots, self.roots_note, self.hand_placed, self.grown = roots, roots_note, hand_placed, grown
        self.seated = None


def _growth_key(surface, settings, seed, cloud):
    """A digest of everything the growth reads: the painted surface, the settings, the roots.

    The surface is hashed in full, not by size: a brush stroke changes the paint and not the
    triangle count, and that has to grow a new vine. Hashing costs a few milliseconds on a
    100k-triangle rock; growing it again costs hundreds.
    """
    digest = hashlib.blake2b(digest_size=16)
    for array in (surface.v0, surface.v1, surface.v2, surface.n, surface.w0, surface.w1, surface.w2):
        digest.update(np.ascontiguousarray(array).data)
    if cloud is not None:
        digest.update(np.ascontiguousarray(cloud).data)
    digest.update(repr((seed, cloud is None, tuple(settings["up"].tolist()),
                        tuple(settings[name] for name in _GROWTH_SETTINGS))).encode())
    return digest.digest()


def _remember(layer_id, grown):
    """Keep `grown` as the last growth of the Vine whose layer is `layer_id`."""
    if len(_GROWN) > 64:
        # A deleted chunk leaves its entry behind. Rather than track chunk lifetimes, drop the
        # lot now and then; the next cook of each Vine grows again.
        _GROWN.clear()
    _GROWN[layer_id] = grown


def _grow_skeleton(surface, settings, seed, cloud):
    """Scatter the attractors, pick the roots and grow.

    Args:
        surface: The painted surface.
        settings: From `_settings`, with "up" set.
        seed: The random seed.
        cloud: The Painted Points, from `_seed_cloud`; None for automatic roots.

    Returns:
        A `_Grown`, or a str saying why nothing grew.
    """
    rng = np.random.default_rng(seed)
    nothing = "nothing painted — paint the surface with a Scatter Brush"

    sample_count = _surface_sample_count(surface, settings)
    positions, _sn, _sw, triangle_of = _sample(surface, sample_count, rng, weighted=False)
    if not len(positions):
        return nothing
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
        return nothing

    # Every root is a node, and the vine needs room past them to grow.
    roots, roots_note = _painted_roots(cloud, projector, limit=max(1, settings["max_nodes"] // 4))
    hand_placed = bool(roots)
    if not roots:
        # Spread across the painted region, not merely out of each other's reach. Spacing them
        # by the influence radius put all eight roots of a boulder in its lowest few
        # centimetres, and a vine cannot jump from there to the far side of the paint -- so one
        # half grew and the other stayed bare. Sharing the region out between them is what
        # "five vines" is asking for.
        spread = max(settings["influence"], surface.diagonal() / (settings["roots"] + 1))
        chosen = _choose_roots(attractors, settings["up"], settings["roots"], spread, attractor_normals)
        roots = [(attractors[i], attractor_normals[i]) for i in chosen]
    if not roots:
        return "no root found on the painted surface"

    nodes = _grow(roots, attractors, projector, settings, rng)
    grown = len(nodes)
    nodes = _prune(nodes, settings["min_branch"])
    nodes = _hang(nodes, projector, settings, seed)
    return _Grown(nodes, projector, len(roots), roots_note, hand_placed, grown)


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
    # Every picked prim, not only the first: a wall and the pillar beside it are one place to
    # grow, and a vine crosses from one to the other where they touch.
    surface = _Surface.join(
        [_collect_surface(upstream.GetPrimAtPath(path), field) for path in _outermost(paths)]
    ).painted()
    if not surface:
        _bail(node, stage, layer, where, "nothing painted — paint the surface with a Scatter Brush")
        return

    settings = _settings(node, surface)
    settings["up"] = _up_axis(stage)
    seed = int(node.get_param("seed").value or 0)
    cloud, cloud_problem = _seed_cloud(upstream, (node.get_param("seed_points").value or "").strip())
    if cloud_problem:
        _bail(node, stage, layer, where, cloud_problem)
        return

    # The growth is most of the cook, and only some params change it. Dragging Thickness,
    # Taper, Lift, Smoothing or anything under Leaves or Display reuses the last cook's
    # skeleton instead of growing the same one again.
    key = _growth_key(surface, settings, seed, cloud)
    grown = _GROWN.get(node.get_layer_identifier())
    if grown is None or grown.key != key:
        grown = _grow_skeleton(surface, settings, seed, cloud)
        if isinstance(grown, str):
            _bail(node, stage, layer, where, grown)
            return
        grown.key = key
        _remember(node.get_layer_identifier(), grown)
    # A copy: smoothing and lifting below move the nodes, and the kept skeleton must not move.
    nodes, projector = grown.nodes.copy(), grown.projector

    strands = _strands(nodes)
    if not strands:
        _bail(node, stage, layer, where, "nothing grew — try a larger Influence or more Density")
        return

    # Smooth and put back on the surface, the whole vine. Growth % cuts it after that, so a
    # vine drawn part-grown sits exactly where the grown one does, and animating Growth does
    # not make the older part wriggle as the tips move on.
    if grown.seated is not None and grown.seated[0] == settings["smooth"]:
        nodes.position[:], nodes.normal[:] = grown.seated[1], grown.seated[2]
    else:
        _smooth_nodes(nodes, strands, settings["smooth"])
        _resurface(nodes, projector)
        grown.seated = (settings["smooth"], nodes.position.copy(), nodes.normal.copy())
    growth = _growth_of(nodes)
    drawn = _draw(nodes, growth, settings["growth"], settings)
    if drawn is None:
        _bail(node, stage, layer, where, "nothing grown yet — raise Growth")
        return
    strands, leaves = drawn[0], drawn[4]

    frames = _frames(settings)
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        UsdGeom.Scope.Define(stage, where[0])
        # The default value is the vine at Growth %, for anything that reads without a time.
        _author_curves(stage, where[1], drawn, settings, node)
        _author_leaves(stage, where[2], drawn[4], settings, node)
        for frame, fraction in frames:
            at = _draw(nodes, growth, fraction, settings)
            time = Usd.TimeCode(frame)
            _author_curves(stage, where[1], at, settings, node, time)
            # The grown vine's leaves, each from the frame the vine reaches it -- not the
            # leaves of the part-grown vine. Those are spaced along strands that regroup as
            # the vine grows, so they would shuffle from frame to frame. Kept by index, which
            # is each leaf's id, so an instancer can follow them.
            _author_leaves(stage, where[2], drawn[4], settings, node, time, fraction)

    hit_cap = grown.grown >= settings["max_nodes"]
    _report(
        node,
        f"{grown.roots} vine{'s' if grown.roots != 1 else ''}"
        f"{' from painted points' if grown.hand_placed else ''} · "
        f"{len(strands)} branches · {len(leaves[0])} leaves"
        + (f" · grows over frames {frames[0][0]:g}-{frames[-1][0]:g}" if frames else "")
        + (f"\ncapped at {settings['max_nodes']} points — raise Max Points" if hit_cap else "")
        + (f"\n{grown.roots_note}" if grown.roots_note else ""),
    )


def _draw(nodes, growth, fraction, settings):
    """The vine as drawn at one moment of its growth.

    Shared by the still image and every animated frame, so frame N of an animation is exactly
    what Growth % set to that frame's share draws.

    Args:
        nodes: The smoothed, re-seated skeleton. Not changed.
        growth: When each node grew, from `_growth_of`.
        fraction: How far grown, 0..1.
        settings: From `_settings`.

    Returns:
        (strands, nodes, radii, growth, leaves) for what has grown, or None before anything
        has: a root on its own is not a curve.
    """
    keep = growth <= fraction + 1e-12
    cut, cut_growth = nodes.subset(keep), growth[keep]
    strands = _strands(cut)
    if not strands:
        return None
    # Widths from the vine as it stands, so a part-grown one still tapers to its tips. Then
    # lift, measured in radii; see `_lift`.
    radii = _radii(cut, settings["base_width"], settings["base_width"] * settings["taper"])
    _lift(cut, radii, settings["lift"])
    leaves = (
        _leaf_points(strands, cut.position, cut.normal, radii, cut_growth, settings)
        if settings["leaves"]
        else (np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0))
    )
    return strands, cut, radii, cut_growth, leaves


def _frames(settings):
    """(frame, growth fraction) for every frame of the animation; empty when it is off.

    The vine grows from nothing at Start Frame to Growth % at End Frame, evenly.
    """
    if not settings["animate"]:
        return []
    start, end = settings["start_frame"], settings["end_frame"]
    if end < start:
        start, end = end, start
    count = int(math.floor(end - start)) + 1
    if count <= 1:
        return [(float(start), settings["growth"])]
    return [(start + i, settings["growth"] * i / (count - 1)) for i in range(count)]


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
        cloud.CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        cloud.CreatePointsAttr().Set(Vt.Vec3fArray())
        cloud.CreateNormalsAttr().Set(Vt.Vec3fArray())
    _report(node, message)


def _author_curves(stage, path, drawn, settings, node, time=Usd.TimeCode.Default()):
    """One BasisCurves holding every strand, with a `growth` primvar per point.

    `growth` is when each point grew, 0 at the roots to 1 at the last tip of the fully grown
    vine. A shader that hides points above a threshold animates the vine growing -- the
    same cut Growth % makes here, but at render time.

    `drawn` is from `_draw`; None writes an empty curve set, which is a frame before anything
    grew. The topology, points, widths, growth and extent are written at `time`.
    """
    smooth = settings["form"] == "Smooth"
    curves = UsdGeom.BasisCurves.Define(stage, path)
    if time.IsDefault():
        curves.CreateTypeAttr().Set(UsdGeom.Tokens.cubic if smooth else UsdGeom.Tokens.linear)
        if smooth:
            curves.CreateBasisAttr().Set(UsdGeom.Tokens.bspline)
        curves.CreateWrapAttr().Set(UsdGeom.Tokens.nonperiodic)
        curves.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
        colour = node.get_param("vine_color").value or (0.18, 0.42, 0.15)
        UsdGeom.Primvar(curves.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
        curves.GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*colour)]))
    growth_primvar = UsdGeom.PrimvarsAPI(curves).CreatePrimvar(
        GROWTH_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    )
    if drawn is None:
        curves.CreateCurveVertexCountsAttr().Set(Vt.IntArray(), time)
        curves.CreatePointsAttr().Set(Vt.Vec3fArray(), time)
        curves.CreateWidthsAttr().Set(Vt.FloatArray(), time)
        growth_primvar.Set(Vt.FloatArray(), time)
        return
    strands, nodes, radii, growth = drawn[:4]
    diameters = np.asarray(radii, dtype=np.float64) * 2.0
    counts, points, widths, ages = [], [], [], []
    for chain in strands:
        # Already smoothed and re-seated on the surface, back when the normals could still be
        # refreshed to match -- see `_smooth_nodes`.
        chain_points = nodes.position[chain]
        chain_widths = diameters[chain]
        chain_ages = growth[chain]
        if smooth:
            chain_points, chain_widths = _pin(chain_points, chain_widths)
            chain_ages = np.concatenate((chain_ages[:1], chain_ages[:1], chain_ages, chain_ages[-1:], chain_ages[-1:]))
        counts.append(len(chain_points))
        points.append(chain_points)
        widths.append(chain_widths)
        ages.append(chain_ages)

    all_points = np.concatenate(points)
    all_widths = np.concatenate(widths)

    curves.CreateCurveVertexCountsAttr().Set(Vt.IntArray([int(c) for c in counts]), time)
    curves.CreatePointsAttr().Set(
        Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(all_points, dtype=np.float32)), time
    )
    curves.CreateWidthsAttr().Set(
        Vt.FloatArray.FromNumpy(np.ascontiguousarray(all_widths, dtype=np.float32)), time
    )
    growth_primvar.Set(
        Vt.FloatArray.FromNumpy(np.ascontiguousarray(np.concatenate(ages), dtype=np.float32)), time
    )
    _set_extent(curves, all_points, float(all_widths.max()) if len(all_widths) else 0.0, time)


def _author_leaves(stage, path, leaves, settings, node, time=Usd.TimeCode.Default(), grown_to=None):
    """The leaf cloud, in the shape Scatter Points already reads.

    Authored even when it is empty, so the Leaf Points output resolves to a real prim the moment
    the chunk exists -- the same reason the brush publishes its points path before anything has
    been painted. Wiring an output is how you decide to use it; it cannot require having used it.

    Guide purpose: the points are data for Scatter Points, and a renderer that draws Points
    (V-Ray) would show each leaf as a ball. usdview shows them with Display Guide on.

    `leaves` None is a frame before any leaf grew. Points, normals, widths, growth and extent
    are written at `time`; with `grown_to`, only the leaves grown by then (growth up to it).
    Every leaf carries its index in `leaves` as its id, so the same leaf keeps the same id on
    every frame.
    """
    if leaves is None:
        leaves = (np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0))
    points, normals, growth = leaves
    ids = np.arange(len(points), dtype=np.int64)
    if grown_to is not None:
        keep = growth <= grown_to + 1e-12
        points, normals, growth, ids = points[keep], normals[keep], growth[keep], ids[keep]

    def vec3f(array):
        return Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(array, dtype=np.float32).reshape(-1, 3))

    cloud = UsdGeom.Points.Define(stage, path)
    if time.IsDefault():
        cloud.CreatePurposeAttr().Set(UsdGeom.Tokens.guide)
        colour = node.get_param("leaf_color").value or (0.35, 0.62, 0.22)
        UsdGeom.Primvar(cloud.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
        cloud.GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*colour)]))
    cloud.CreatePointsAttr().Set(vec3f(points), time)
    cloud.CreateIdsAttr().Set(Vt.Int64Array.FromNumpy(ids), time)
    cloud.CreateNormalsAttr().Set(vec3f(normals), time)
    cloud.CreateWidthsAttr().Set(Vt.FloatArray([settings["leaf_size"]] * len(points)), time)
    # The same clock as the curves' growth primvar, so a shader can grow leaves in with them.
    UsdGeom.PrimvarsAPI(cloud).CreatePrimvar(
        GROWTH_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
    ).Set(Vt.FloatArray.FromNumpy(np.ascontiguousarray(growth, dtype=np.float32)), time)
    if len(points):
        _set_extent(cloud, np.asarray(points), settings["leaf_size"], time)


def _set_extent(gprim, points, width, time=Usd.TimeCode.Default()):
    """A bound that accounts for the width, since a curve is drawn fatter than its control points."""
    if not len(points):
        return
    pad = width * 0.5
    low = points.min(axis=0) - pad
    high = points.max(axis=0) + pad
    gprim.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*low), Gf.Vec3f(*high)]), time)


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
    animate = bool(node.get_param("animate").value)
    _show(node, "start_frame", animate)
    _show(node, "end_frame", animate)
    leaves = bool(node.get_param("leaves").value)
    _show(node, "leaf_spacing", leaves)
    _show(node, "leaf_spread", leaves)
    _show(node, "leaf_bare", leaves)
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
            tooltip="The meshes the vines climb — or prims above them. Pick several, or use a\n"
            "pattern like /World/Walls/*, and the vines grow over all of them, crossing where\n"
            "they touch. Point the Scatter Brush at the same thing.",
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
            "growth",
            "Growth %",
            min_value=0.0,
            max_value=100.0,
            default_value=100.0,
            flags=_PF.LABELED,
            tooltip="How far the vines have grown, from the roots (0) to fully grown (100).\n"
            "Cuts every vine at the same moment of its growth, so a low value is a young\n"
            "vine, not a shorter one. Drawing only: changing it does not grow again.",
        )
    )
    grow.add_param(
        chunk.Slider(
            "density",
            "Density",
            min_value=0.0,
            max_value=1.0,
            default_value=0.2,
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
            default_value=0.15,
            flags=_PF.LABELED,
            tooltip="Pull toward the stage's up axis — the plant's phototropism. Zero lets the\n"
            "vines wander the painted region evenly; high sends them straight up a wall.",
        )
    )
    grow.add_param(
        chunk.Slider(
            "meander",
            "Meander",
            min_value=0.0,
            max_value=1.0,
            default_value=0.8,
            flags=_PF.LABELED,
            tooltip="How much the vines swing from side to side as they grow, in long smooth\n"
            "curves. Zero grows them straight toward the unfilled paint, which reads as\n"
            "combed hair; high makes them wind like old ivy.",
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
        chunk.Slider(
            "root_count",
            "Roots",
            min_value=1,
            max_value=50,
            default_value=5,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="How many vines to start, lowest-first across the painted region. Ignored\n"
            "while Painted Points is wired — then every dab is a root.",
        )
    )
    grow.add_param(
        chunk.Slider(
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

    # --- animation ---
    animation = _collapsed(chunk.Group("anim_grp", "Animation", icon="film", flags=_GF))
    animate = chunk.Checkbox(
        "animate",
        "Animate Growth",
        default_value=False,
        flags=_PF.LABELED,
        tooltip="Write the growth as an animation: nothing at Start Frame, Growth % at End\n"
        "Frame. Every frame is what Growth % would draw at that point. Curves to Mesh sweeps\n"
        "each frame, so the mesh grows too.",
    )
    animate.add_callback(_wiring_changed)
    animation.add_param(animate)
    animation.add_param(
        chunk.Slider(
            "start_frame",
            "Start Frame",
            min_value=0,
            max_value=1000,
            default_value=1,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="The frame the vine starts growing from its roots.",
        )
    )
    animation.add_param(
        chunk.Slider(
            "end_frame",
            "End Frame",
            min_value=0,
            max_value=1000,
            default_value=100,
            is_int=True,
            flags=_PF.LABELED,
            tooltip="The frame it reaches Growth %.",
        )
    )
    chunk_api.add_param(animation)

    # --- hanging ---
    hang = _collapsed(chunk.Group("hang_grp", "Hanging", icon="sprout", flags=_GF))
    hang.add_param(
        chunk.Slider(
            "hang",
            "Hang %",
            min_value=0.0,
            max_value=100.0,
            default_value=0.0,
            flags=_PF.LABELED,
            tooltip="Share of the vine tips that drop a tendril off the surface. It falls under\n"
            "gravity and drapes over the painted mesh rather than through it, like ivy hanging\n"
            "off a ledge. Past the painted part it falls freely.",
        )
    )
    hang.add_param(
        chunk.Slider(
            "hang_length",
            "Hang Length %",
            min_value=1.0,
            max_value=50.0,
            default_value=12.0,
            flags=_PF.LABELED,
            tooltip="How long each hanging tendril is, as a percentage of the painted region.",
        )
    )
    chunk_api.add_param(hang)

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
        chunk.Slider(
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
            "leaf_spread",
            "Spread",
            min_value=0.0,
            max_value=6.0,
            default_value=1.0,
            flags=_PF.LABELED,
            tooltip="How far each leaf sits to the side of the stem, as a multiple of Thickness.\n"
            "Leaves alternate left and right; zero puts them all down the middle.",
        )
    )
    leaf.add_param(
        chunk.Slider(
            "leaf_bare",
            "Bare Base %",
            min_value=0.0,
            max_value=90.0,
            default_value=0.0,
            flags=_PF.LABELED,
            tooltip="No leaves on the oldest part of each vine: the share of its growth, from the\n"
            "root, left bare. Old ivy stems lose their leaves; 30 to 50 reads as mature.",
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
            default_value=24.0,
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
            default_value=5.0,
            flags=_PF.LABELED,
            tooltip="How close a vine has to come before that part of the paint counts as\n"
            "covered, as a multiple of Step. It sets how far apart branches settle.",
        )
    )
    advanced.add_param(
        chunk.Slider(
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
        Grows climbing vines over the painted part of a mesh, with points for leaves along them.

        Wire a Scatter Brush's Density Field output into this chunk's Density
        Field input, with both pointed at the same mesh, and vines fill exactly
        what was painted. Wire its Painted Points output into Painted Points as
        well and every dab becomes the root of its own vine.

        The result is two prims: a BasisCurves of the vines, and a Leaf Points
        cloud carrying a surface normal per point. That cloud is the same shape
        the brush publishes, so feeding it to a Scatter Points chunk's Painted
        Points input instances real leaf geometry along the vine, laid flat on
        the surface. Every curve point carries a "growth" primvar, 0 at the
        roots to 1 at the last tip, for shaders that animate the vine growing.

        Parameters:
          - Surface Mesh: the meshes the vines climb; several, or a pattern.
          - Growth %: how far the vines have grown, 0 to 100. Cuts them at one
            moment of their growth; changing it does not grow them again.
          - Animate Growth / Start Frame / End Frame: write the growth as time
            samples, from nothing to Growth %.
          - Auto Step / Step: the length of one growth step — how smoothly a
            vine curves. It does not change how thick one is.
          - Density: how thickly the vines fill what was painted.
          - Climb: pull toward the up axis — the plant's phototropism.
          - Meander: long, smooth side-to-side swings, so vines wind rather than
            run in parallel.
          - Wander: randomness per step.
          - Roots: how many vines to start when no painted points are wired.
          - Hang % / Hang Length %: tendrils that fall off the vine tips and
            drape over the mesh, like ivy hanging off a ledge.
          - Thickness %: width at the base, as a share of the painted region,
            so the defaults hold at any scene scale. Lift, leaf spacing and
            leaf size are all multiples of it.
          - Taper: how thin a tip gets, as a fraction of Thickness.

        Growth is space colonization (Runions et al., 2007) constrained to the
        surface, with the momentum, phototropism and randomness of Luft's ivy
        generator. Vines never leave the mesh, and never leave the paint.
        """,
    )
