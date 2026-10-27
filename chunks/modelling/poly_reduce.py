"""Poly Reduce chunk -- quadric-error decimation of a mesh, or a whole hierarchy of them.

The reduction itself is Garland & Heckbert's quadric error metric, via
`fast_simplification` (Sven Forstmann's C++ implementation, the one PyVista ships). QEM
is the method every DCC's decimator is built on: each edge collapse is scored by how far
it moves the surface away from the original planes meeting at that edge, and the cheapest
collapse is always taken next. That is what "reduce a lot without destroying the shape"
means in practice -- flat regions lose almost all of their triangles, silhouettes and
creases keep theirs, because moving them is expensive.

What actually makes or breaks a reducer on a real asset is not the collapse maths, though
 -- it's everything hanging off the topology. This one:

* **triangulates first** (fan), remembering which original face-corner each triangle
  corner came from, so every per-corner value can be carried back afterwards;
* **splits vertices at UV seams** before decimating, by welding on (point index, all
  faceVarying primvar values) rather than point index alone. A seam then *is* an open
  border to the decimator, which will not collapse a border vertex into an interior one --
  so the UV layout survives the reduction instead of smearing across the seam. The
  faceVarying primvars come back out as vertex primvars on the reduced mesh, which is
  exactly what the split bought us;
* **keeps materials** by putting each `GeomSubset`'s face membership into that same weld
  key. Two faces with different materials no longer share a vertex, so no collapse can
  ever straddle the boundary, and every surviving triangle still belongs to exactly one
  subset -- its `indices` are rewritten to match;
* **carries vertex primvars through** (`displayColor`, skinning weights, anything
  `vertex`/`varying`) by resolving the decimator's own collapse history into an
  original-vertex → reduced-vertex mapping (`collapse_mapping`).

Anything that genuinely cannot survive a change of topology is blocked rather than left
lying: authored `normals` (the renderer computes better ones for the new surface than a
remap of the old ones would), `uniform` primvars, and the subdivision crease/corner/hole
tags, whose indices point at points that no longer exist.

Reads its input through `get_upstream_stage()`. That is not optional here: this chunk
authors `points`/`faceVertexIndices` overrides on the *same* prim it reads, so a plain
composed-stage read would find its own previous output and reduce the reduction, and
again, and again on every cook.

A big reduction runs in a separate process (sim_bake.in_subprocess): fast_simplification
holds the GIL while it works, so even a thread would stop the host. The cook waits for it
(sim_bake.wait), behind a progress window with Cancel, so the chunks below cook once,
against the reduced mesh.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import NamedTuple

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils
from usd_chunks import chunk_stage
from usd_chunks import proxy_setup
from usd_chunks import sim_bake
from usd_chunks import surface_samples
from usd_chunks.mesh_math import fan_triangulate

try:
    import fast_simplification

    _HAS_SIMPLIFY = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheel
    fast_simplification = None
    _HAS_SIMPLIFY = False

logger = logging.getLogger(__name__)

MODE_PERCENT = "Percentage"
MODE_COUNT = "Triangle Count"
MODE_PLANAR = "Planar Only"
_MODES = [MODE_PERCENT, MODE_COUNT, MODE_PLANAR]

# The fewest triangles a closed surface can have. Asking for less is asking for nothing.
_MIN_TRIANGLES = 4

# Meshes with at least this many triangles in all are reduced in a separate process, behind
# a progress window: 4.5M triangles froze the host for 4.6s with no way out. Below this a
# plain mesh takes well under a second here, and starting the process (about 1.5s, mostly
# importing USD) would only make the result later.
_PROCESS_TRIANGLES = 500_000

# Topology-derived attributes with no meaning once the topology changes. `normals` is here
# rather than remapped on purpose: a remapped normal describes the *old* surface, and every
# renderer will compute a better one from the reduced mesh itself.
_STALE_ATTRS = (
    "normals",
    "velocities",
    "accelerations",
    "cornerIndices",
    "cornerSharpnesses",
    "creaseIndices",
    "creaseLengths",
    "creaseSharpnesses",
    "holeIndices",
)


# ---------------------------------------------------------------------------
# array maths -- no USD, no Qt, unit-testable on its own
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReducedMesh:
    """The result of one decimation, plus the two lookups needed to rebuild its attributes.

    `source_point[j]` / `source_corner[j]` name one original vertex and one original
    face-corner that ended up merged into reduced vertex `j` -- the representative whose
    primvar values that vertex inherits. Which of several merged sources is picked is
    arbitrary and always has been in every decimator; what matters is that it is one of
    them, so the value is real data from the neighbourhood rather than an average that
    belongs to nothing.

    Both are None when the caller did not ask for them, or when the collapse history could
    not be resolved. That is a "no attributes survive" answer, not a "use these anyway"
    one -- a wrong mapping would scatter UVs across the mesh, which is far worse than a
    reduced mesh that has lost them.
    """

    points: np.ndarray  # (n, 3) float64
    triangles: np.ndarray  # (m, 3) int64
    source_point: np.ndarray | None  # (n,) index into the original points array
    source_corner: np.ndarray | None  # (n,) index into the original face-corner array


# A packed weld key has to stay inside int64. The margin below 2**63 is there so the guard
# can be checked before the multiply that would overflow, not after.
_KEY_LIMIT = 1 << 62


def component_codes(values) -> list:
    """Label each *component* of a per-element value with a small integer, one array each.

    Labelling `st.x` and `st.y` separately and combining the two labels afterwards
    partitions the corners exactly the same way as labelling whole `(u, v)` rows would --
    the product of the two partitions is the row partition. It is worth the detour because
    a one-column `np.unique` is an ordinary sort, while the row version is a lexsort over a
    2-D array: on a million-triangle mesh that difference is seconds, not milliseconds.
    """
    arr = np.asarray(values)
    if arr.ndim <= 1:
        arr = arr.reshape(-1, 1)
    else:
        arr = arr.reshape(len(arr), -1)
    return [
        np.asarray(np.unique(arr[:, c], return_inverse=True)[1], dtype=np.int64).reshape(-1)
        for c in range(arr.shape[1])
    ]


def _width(column) -> int:
    """One past the largest label in a column -- how many values it can take."""
    return int(column.max()) + 1 if column.size else 1


def _pack_key(columns):
    """One non-negative int64 per row, distinct rows getting distinct keys.

    Returns None when the columns are together too wide to pack, which leaves the caller
    to fall back on comparing the rows themselves. Every column must be a dense label array
    (`component_codes`) or an index, or the packed range explodes and the guard trips on
    perfectly ordinary data.
    """
    key = np.zeros(len(columns[0]), dtype=np.int64)
    span = 1
    for column in columns:
        width = _width(column)
        if span > _KEY_LIMIT // width:
            return None
        key = key * width + column
        span *= width
    return key


def _densify(key) -> np.ndarray:
    """Renumber a key array so its labels run 0..n-1."""
    _, inverse = np.unique(key, return_inverse=True)
    return np.asarray(inverse, dtype=np.int64).reshape(-1)


def combine_codes(*code_arrays) -> np.ndarray:
    """Fold several per-element label arrays into one, again densely numbered.

    Folded one column at a time rather than all at once, because the packed range is the
    *product* of the column widths and three float components of a UV set blow past int64
    on their own: a mesh with a few million distinct u's, v's and w's gives 10**18 before
    the third column is even folded in. Renumbering what has been folded so far collapses
    that back to the number of combinations actually present -- normally orders of magnitude
    smaller -- and folding continues. Getting this wrong is not a correctness bug (the
    row-wise fallback is exact) but a 5x one: on a 4.2M-triangle tree the lexsort it fell
    back to cost 10.6s where the packed path costs ~2s.
    """
    columns = [np.asarray(c, dtype=np.int64).reshape(-1) for c in code_arrays if c is not None]
    if not columns:
        return None
    if len(columns) == 1:
        return columns[0]

    key = columns[0]
    span = _width(key)
    for column in columns[1:]:
        width = _width(column)
        if span > _KEY_LIMIT // width:
            key = _densify(key)
            span = _width(key)
            if span > _KEY_LIMIT // width:
                # Genuinely too wide even after renumbering: compare the rows themselves.
                return _densify_rows(columns)
        key = key * width + column
        span *= width
    return _densify(key)


def _densify_rows(columns) -> np.ndarray:
    _, inverse = np.unique(np.stack(columns, axis=1), axis=0, return_inverse=True)
    return np.asarray(inverse, dtype=np.int64).reshape(-1)


def _distinct_corners(triangles) -> np.ndarray:
    """Which triangles still have three different vertices.

    Both callers mean the same thing by it: a triangle two of whose corners have become the
    same vertex is not a triangle any more. Welding can do that (a degenerate face in the
    source), and so can an edge collapse -- which is exactly when the decimator deletes one.
    """
    return (
        (triangles[:, 0] != triangles[:, 1])
        & (triangles[:, 1] != triangles[:, 2])
        & (triangles[:, 0] != triangles[:, 2])
    )


def _weld(points, triangles, corners, split_codes):
    """Split vertices so that each distinct (point, split code) pair is its own vertex.

    With `split_codes` None this is a plain compaction -- unreferenced points dropped,
    indices renumbered -- which is still worth doing: the decimator's own bookkeeping
    assumes a dense vertex array.
    """
    flat_point = triangles.reshape(-1)
    flat_corner = corners.reshape(-1)

    columns = [flat_point] if split_codes is None else [flat_point, split_codes[flat_corner]]
    key = flat_point if split_codes is None else _pack_key(columns)
    if key is None:
        _, first, inverse = np.unique(
            np.stack(columns, axis=1), axis=0, return_index=True, return_inverse=True
        )
    else:
        _, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    welded = np.asarray(inverse, dtype=np.int64).reshape(-1, 3)
    source_point = flat_point[first]
    source_corner = flat_corner[first]

    # Welding can make two corners of a triangle the same vertex; the decimator's edge
    # bookkeeping has no meaningful collapse for those, so drop them and re-compact.
    keep = _distinct_corners(welded)
    if not keep.all():
        welded = welded[keep]
        if welded.size == 0:
            return None
        used, welded = np.unique(welded, return_inverse=True)
        welded = welded.reshape(-1, 3)
        source_point = source_point[used]
        source_corner = source_corner[used]

    return points[source_point], welded, source_point, source_corner


def reduce_triangles(
    points,
    triangles,
    corners,
    split_codes=None,
    *,
    target_count=None,
    planar_only=False,
    aggressiveness=7.0,
    lock_boundary=False,
    carry_attributes=True,
):
    """Decimate a triangle mesh, returning a `ReducedMesh` (or None if there is nothing to do).

    `target_count` is a triangle count, not a ratio, so the caller owns the "percentage of
    what" question. `planar_only` runs the library's lossless pass instead: only collapses
    that cost no error at all, which cleans up over-tessellated flat regions and leaves
    every curved one exactly as it was.
    """
    if not _HAS_SIMPLIFY:
        raise RuntimeError("fast_simplification is not installed")

    welded = _weld(np.asarray(points, dtype=np.float64), triangles, corners, split_codes)
    if welded is None:
        return None
    weld_points, weld_tris, weld_source_point, weld_source_corner = welded

    n_tris = len(weld_tris)
    if not planar_only:
        if target_count is None:
            return None
        target_count = int(max(_MIN_TRIANGLES, min(int(target_count), n_tris)))
        if target_count >= n_tris:
            return None

    kwargs = dict(
        preserve_border=bool(lock_boundary),
        return_collapses=True,
    )
    if planar_only:
        kwargs["lossless"] = True
    else:
        kwargs["target_count"] = target_count
        kwargs["agg"] = float(aggressiveness)

    out_points, out_tris, collapses = fast_simplification.simplify(
        weld_points, weld_tris.astype(np.int32), **kwargs
    )
    out_tris = np.asarray(out_tris, dtype=np.int64)
    if out_tris.size == 0:
        return None

    source_point = source_corner = None
    if carry_attributes:
        mapping = collapse_mapping(weld_tris, collapses, len(weld_points), len(out_points))
        if mapping is not None:
            representative = np.zeros(len(out_points), dtype=np.int64)
            live = np.flatnonzero(mapping >= 0)
            representative[mapping[live]] = live
            source_point = weld_source_point[representative]
            source_corner = weld_source_corner[representative]

    return ReducedMesh(np.asarray(out_points, dtype=np.float64), out_tris, source_point, source_corner)


def collapse_mapping(triangles, collapses, n_points, n_reduced):
    """Welded vertex -> reduced vertex, derived straight from the collapse history.

    `collapses[i] = [i0, i1]` says vertex `i1` was merged into `i0`. Each vertex can only
    be collapsed away once, so the pairs form a forest and resolving every chain to its
    root is pointer-jumping -- a handful of vectorised passes, no ordering to respect.
    A surviving triangle is then an original triangle whose three roots are still distinct
    (an edge collapse deletes a triangle exactly when two of its corners merge), and the
    decimator numbers the survivors in original-index order, which `np.searchsorted` over
    the sorted survivors reproduces.

    Not the library's `replay_simplification`, which returns the same answer
    -- but gets there via `np.delete` inside a Python loop over the isolated points it has
    to clean up. On a mesh made of thousands of disconnected cards (a tree's twigs, say)
    that list is enormous and the loop goes quadratic -- sub-second decimation becomes
    minutes. Deriving it here is exact, and free.

    Returns None if the derivation disagrees with what the decimator actually produced;
    attributes are then dropped rather than scattered onto the wrong vertices.
    """
    parent = np.arange(n_points, dtype=np.int64)
    collapses = np.asarray(collapses, dtype=np.int64).reshape(-1, 2)
    if collapses.size:
        parent[collapses[:, 1]] = collapses[:, 0]
        for _ in range(64):  # a cap, not an expected bound -- depth is logarithmic in practice
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
        else:  # pragma: no cover - would mean the collapse pairs are not a forest
            logger.debug("collapse chains did not settle; reduced mesh will carry no primvars")
            return None

    rooted = parent[triangles]
    survivors = np.unique(rooted[_distinct_corners(rooted)])
    if len(survivors) != n_reduced:
        logger.debug(
            "derived %d surviving vertices but the decimation produced %d; "
            "reduced mesh will carry no primvars",
            len(survivors),
            n_reduced,
        )
        return None

    # One search over the sorted survivors, then confirm each hit: `np.isin` would sort
    # the same array again to answer the very question this search already answers.
    position = np.minimum(np.searchsorted(survivors, parent), len(survivors) - 1)
    return np.where(survivors[position] == parent, position, -1)


def target_triangle_count(mode, n_tris, keep_percent, target_tris) -> int | None:
    """How many triangles the user asked for, or None for the modes that don't ask."""
    if mode == MODE_PLANAR:
        return None
    if mode == MODE_COUNT:
        return int(target_tris)
    return int(round(n_tris * max(0.0, min(100.0, float(keep_percent))) / 100.0))


# ---------------------------------------------------------------------------
# USD
# ---------------------------------------------------------------------------


def _mesh_prims(prim, recurse):
    """Every mesh this chunk should act on for one selected prim.

    An artist picks the asset, not the twelve meshes inside it, so a hierarchy is the
    normal case and each mesh in it is reduced on its own terms. Instance proxies are
    skipped: an override authored on one is not legal, and silently writing nothing is
    worse than saying so.
    """
    if not prim or not prim.IsValid() or surface_samples.is_passthrough(prim):
        return []  # a proxy kept as its source by Remesh Proxy: already under budget
    found = []
    for candidate in Usd.PrimRange(prim) if recurse else [prim]:
        if not UsdGeom.Mesh(candidate):
            continue
        if candidate.IsInstanceProxy():
            logger.warning("skipping %s: cannot author an override on an instance proxy", candidate.GetPath())
            continue
        found.append(candidate)
    return found


def _face_subsets(prim, n_faces):
    """`(subsets, code_per_face, membership_rows)` for the face subsets on `prim`.

    `code_per_face` labels each face by *which set of subsets it belongs to*; two faces
    sharing a label are interchangeable as far as material assignment goes.
    `membership_rows[code, i]` says whether that label is in subset `i`.
    """
    subsets = [
        s
        for s in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(prim))
        if s.GetElementTypeAttr().Get() == UsdGeom.Tokens.face
    ]
    if not subsets:
        return [], None, None

    membership = np.zeros((n_faces, len(subsets)), dtype=bool)
    for column, subset in enumerate(subsets):
        indices = subset.GetIndicesAttr().Get()
        if not indices:
            continue
        idx = np.asarray(indices, dtype=np.int64)
        membership[idx[(idx >= 0) & (idx < n_faces)], column] = True

    if len(subsets) < 63:
        # One bit per subset packs the whole membership row into an integer, so the labels
        # come from a 1-D `np.unique` (a sort) rather than a row-wise one (a lexsort over a
        # 2-million-row array, which cost seconds on a real tree).
        bits = membership.astype(np.int64) @ (1 << np.arange(len(subsets), dtype=np.int64))
        packed, code_per_face = np.unique(bits, return_inverse=True)
        rows = ((packed[:, None] >> np.arange(len(subsets), dtype=np.int64)) & 1).astype(bool)
    else:  # pragma: no cover - a mesh with 63+ face subsets
        rows, code_per_face = np.unique(membership, axis=0, return_inverse=True)
    return subsets, np.asarray(code_per_face, dtype=np.int64).reshape(-1), rows


def _primvar_values(primvar):
    """A primvar's values, flattened out of any indexing, or None if it has none."""
    try:
        values = primvar.ComputeFlattened()
    except Exception:  # pragma: no cover - a malformed primvar shouldn't take the cook down
        values = primvar.Get()
    return values if values else None


def _remap(values, picks):
    """Index a Vt array with a numpy index array, coming back out as the same Vt type."""
    picked = np.asarray(values)[picks]
    try:
        return type(values).FromNumpy(np.ascontiguousarray(picked))
    except Exception:
        return type(values)(picked.tolist())


class _Settings(NamedTuple):
    """The knobs one cook runs with.

    One object rather than a tuple for the fingerprint and six positional arguments for the
    planner: they described the same settings in a different order, which is a rename away
    from silently reducing to the aggressiveness value.
    """

    mode: str
    keep_percent: float
    target_tris: int
    aggressiveness: float
    lock_boundary: bool
    keep_uvs: bool
    recurse: bool


@dataclass
class _Plan:
    """Everything one mesh's cook decided, held until every mesh has decided.

    Nothing is authored while the reduction is still running. A chunk's layer is its
    output, and clearing it before the work is done leaves the stage showing a half-built
    mesh for as long as the cook takes -- and showing nothing at all if the cook then
    raises.
    """

    path: Sdf.Path
    reduced: ReducedMesh
    before: int
    vertex_primvars: list  # (name, type name, Vt values)
    blocked_primvars: list  # names
    subset_indices: list  # (subset path, Vt.IntArray)
    drop_subdivision: bool


@dataclass
class _MeshRead:
    """One mesh as read off the stage: the reduction's input, and what goes back on its result.

    `job` is everything the reduction itself needs (reduce_mesh's arguments), as plain
    arrays a separate process can take. The rest stays here, for _finish_plan.
    """

    path: Sdf.Path
    job: dict
    corner_subset: np.ndarray | None  # subset label of each face-corner
    subsets: list
    membership_rows: np.ndarray | None
    face_varying: list  # (name, type name, Vt values)
    vertex_varying: list  # (name, type name, Vt values)
    blocked: list  # names
    drop_subdivision: bool


def _read_mesh(prim, settings: _Settings) -> _MeshRead | None:
    """Read what reducing `prim` needs, without reducing it. Main thread: this reads USD."""
    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None

    points = np.asarray(points, dtype=np.float64)
    counts = np.asarray(counts, dtype=np.int64)
    n_faces = len(counts)
    n_corners = int(counts.sum())

    subsets, subset_code_per_face, membership_rows = _face_subsets(prim, n_faces)

    # Everything that has to stay unmixed after a collapse goes into the weld key: UV (and
    # other faceVarying) discontinuities, and material boundaries.
    split_columns = []
    corner_subset = None
    if subset_code_per_face is not None:
        corner_subset = np.repeat(subset_code_per_face, counts)
        split_columns.append(corner_subset)

    face_varying = []
    vertex_varying = []
    blocked = []
    for primvar in UsdGeom.PrimvarsAPI(prim).GetPrimvars():
        interpolation = primvar.GetInterpolation()
        if interpolation == UsdGeom.Tokens.constant:
            continue  # one value for the whole mesh -- topology cannot invalidate it
        values = _primvar_values(primvar)
        if values is None:
            continue
        name = primvar.GetPrimvarName()
        if interpolation == UsdGeom.Tokens.uniform:
            blocked.append(name)
        elif interpolation == UsdGeom.Tokens.faceVarying:
            if settings.keep_uvs and len(values) == n_corners:
                face_varying.append((name, primvar.GetTypeName(), values))
            else:
                blocked.append(name)
        elif len(values) == len(points):
            vertex_varying.append((name, primvar.GetTypeName(), values))
        else:
            blocked.append(name)

    job = dict(
        points=points,
        counts=counts,
        indices=np.asarray(indices, dtype=np.int64),
        split_columns=split_columns,
        face_varying=[np.asarray(values) for _, _, values in face_varying],
        mode=settings.mode,
        keep_percent=settings.keep_percent,
        target_tris=settings.target_tris,
        aggressiveness=settings.aggressiveness,
        lock_boundary=settings.lock_boundary,
        carry_attributes=bool(face_varying or vertex_varying or subsets),
    )
    scheme = mesh.GetSubdivisionSchemeAttr().Get()
    return _MeshRead(
        path=prim.GetPath(),
        job=job,
        corner_subset=corner_subset,
        subsets=subsets,
        membership_rows=membership_rows,
        face_varying=face_varying,
        vertex_varying=vertex_varying,
        blocked=blocked,
        drop_subdivision=bool(scheme) and scheme != UsdGeom.Tokens.none,
    )


def reduce_mesh(
    points,
    counts,
    indices,
    split_columns,
    face_varying,
    mode,
    keep_percent,
    target_tris,
    aggressiveness,
    lock_boundary,
    carry_attributes,
) -> dict | None:
    """Triangulate and decimate one mesh read by _read_mesh. No USD, so it runs in any process.

    Returns:
        The ReducedMesh's fields plus `before`, the triangle count it started from, as a
        dict of plain arrays so it can come back from another process. None if there is
        nothing to reduce.
    """
    triangles, corners, _ = fan_triangulate(counts, indices)
    if len(triangles) <= _MIN_TRIANGLES:
        return None
    columns = list(split_columns)
    for values in face_varying:
        columns.extend(component_codes(values))
    reduced = reduce_triangles(
        points,
        triangles,
        corners,
        combine_codes(*columns) if columns else None,
        target_count=target_triangle_count(mode, len(triangles), keep_percent, target_tris),
        planar_only=mode == MODE_PLANAR,
        aggressiveness=aggressiveness,
        lock_boundary=lock_boundary,
        carry_attributes=carry_attributes,
    )
    if reduced is None:
        return None
    return dict(vars(reduced), before=len(triangles))


def reduce_meshes(progress, jobs) -> list:
    """reduce_mesh for each job. This is what the separate process runs (sim_bake.in_subprocess).

    Args:
        progress: progress(done, total, text) before each mesh, for the progress window: the
            triangles reduced so far of all of them. One mesh is one call that says nothing
            until it ends, so for one the total is 0, which the window shows as busy.
        jobs: reduce_mesh's arguments, one dict per mesh.

    Returns:
        reduce_mesh's result for each job.
    """
    sizes = [int(np.maximum(np.asarray(job["counts"], dtype=np.int64) - 2, 0).sum()) for job in jobs]
    total = sum(sizes)
    results = []
    done = 0
    for i, (job, size) in enumerate(zip(jobs, sizes)):
        if len(jobs) == 1:
            progress(0, 0, f"reducing {_compact(size)} tris")
        else:
            progress(done, total, f"reducing mesh {i + 1} of {len(jobs)}, {_compact(size)} tris")
        results.append(reduce_mesh(**job))
        done += size
    return results


def _finish_plan(read: _MeshRead, result: dict | None) -> _Plan | None:
    """Turn one mesh's reduction into a plan, with its primvars and subsets moved onto it."""
    if result is None:
        return None
    result = dict(result)
    before = result.pop("before")
    reduced = ReducedMesh(**result)

    if reduced.source_corner is None:
        if read.subsets:
            # Without a mapping the subsets cannot be rewritten, and a subset left pointing
            # at the old face list is not stale but *out of range* -- Hydra drops it and says
            # so once per index. Leaving the mesh at full resolution is the lesser failure:
            # it is visibly not reduced, rather than quietly missing its materials.
            logger.warning(
                "%s: could not resolve the collapse history, so its %d face subsets cannot "
                "be rewritten — leaving this mesh unreduced",
                read.path,
                len(read.subsets),
            )
            return None
        # No usable mapping, so nothing per-vertex can be carried honestly. Block what the
        # old topology described rather than leaving arrays of the wrong length behind.
        carried = []
        blocked = read.blocked + [name for name, _, _ in read.face_varying + read.vertex_varying]
    else:
        carried = [
            (name, type_name, _remap(values, reduced.source_corner))
            for name, type_name, values in read.face_varying
        ]
        carried += [
            (name, type_name, _remap(values, reduced.source_point))
            for name, type_name, values in read.vertex_varying
        ]
        blocked = read.blocked

    subset_indices = []
    if read.subsets:
        # A surviving triangle inherits the material region of any of its corners: no
        # collapse can cross a region boundary, because the weld above gave the two sides
        # separate vertices in the first place.
        face_code = read.corner_subset[reduced.source_corner[reduced.triangles[:, 0]]]
        for column, subset in enumerate(read.subsets):
            kept = np.flatnonzero(read.membership_rows[face_code, column]).astype(np.int32)
            subset_indices.append((subset.GetPrim().GetPath(), Vt.IntArray.FromNumpy(kept)))

    return _Plan(
        path=read.path,
        reduced=reduced,
        before=before,
        vertex_primvars=carried,
        blocked_primvars=blocked,
        subset_indices=subset_indices,
        drop_subdivision=read.drop_subdivision,
    )


def _plan_mesh(prim, settings: _Settings) -> _Plan | None:
    """Work out what `prim` reduces to, here and now, without touching the stage."""
    read = _read_mesh(prim, settings)
    return None if read is None else _finish_plan(read, reduce_mesh(**read.job))


def _author(stage, plan: _Plan) -> None:
    """Write one plan into the current edit target, as a single published change.

    Split into resolve-then-write on purpose. The new topology and the rewritten
    `GeomSubset` indices have to become visible together: a subset still holding indices
    into the *old* face list is not merely stale, it is out of range, and Hydra reacts by
    throwing the subset away --

        Geom subset index 2107245 is larger than number of faces (1044205), removing.

    -- thousands of times on a dense mesh. Every `Set()` publishes its own
    change notice, so writing points first and subsets last guaranteed a window where the
    two disagreed. Batching the value writes into one `Sdf.ChangeBlock` closes it.

    Everything that touches the stage's *structure* -- looking prims up, creating primvar
    attributes, asking whether an attribute has an authored value -- happens before the
    block opens. Composed reads and namespace edits inside a change block are exactly what
    it is documented not to be safe for.
    """
    prim = stage.GetPrimAtPath(plan.path)
    if not prim or not prim.IsValid():
        return
    mesh = UsdGeom.Mesh(prim)
    reduced = plan.reduced

    points32 = np.ascontiguousarray(reduced.points, dtype=np.float32)
    writes = [
        (mesh.GetPointsAttr(), Vt.Vec3fArray.FromNumpy(points32)),
        (
            mesh.GetFaceVertexIndicesAttr(),
            Vt.IntArray.FromNumpy(np.ascontiguousarray(reduced.triangles.reshape(-1), dtype=np.int32)),
        ),
        (
            mesh.GetFaceVertexCountsAttr(),
            Vt.IntArray.FromNumpy(np.full(len(reduced.triangles), 3, dtype=np.int32)),
        ),
        # Straight from numpy: Gf.Vec3f refuses np.float32 scalars, so building the two
        # corners component-by-component would raise here rather than at the obvious place.
        (
            mesh.GetExtentAttr(),
            Vt.Vec3fArray.FromNumpy(
                np.ascontiguousarray(np.stack([points32.min(axis=0), points32.max(axis=0)]))
            ),
        ),
    ]
    if plan.drop_subdivision:
        # The reduction approximates the polygonal cage, not the limit surface. Leaving
        # subdivision on would smooth an already-approximate mesh a second time and shrink
        # it away from the shape the user was looking at.
        writes.append((mesh.GetSubdivisionSchemeAttr(), UsdGeom.Tokens.none))

    blocks = [
        attr
        for name in _STALE_ATTRS
        if (attr := prim.GetAttribute(name)) and attr.HasAuthoredValue()
    ]

    api = UsdGeom.PrimvarsAPI(prim)
    blocks += [
        existing.GetAttr() for name in plan.blocked_primvars if (existing := api.GetPrimvar(name))
    ]
    for name, type_name, values in plan.vertex_primvars:
        primvar = api.CreatePrimvar(name, type_name, UsdGeom.Tokens.vertex)
        writes.append((primvar.GetAttr(), values))
        # A remapped primvar is already one value per reduced vertex; an inherited index
        # array from the source would re-scramble it.
        blocks.append(primvar.CreateIndicesAttr())

    for subset_path, indices in plan.subset_indices:
        subset = UsdGeom.Subset(stage.GetPrimAtPath(subset_path))
        if subset:
            writes.append((subset.GetIndicesAttr(), indices))

    with Sdf.ChangeBlock():
        for attr, value in writes:
            attr.Set(value)
        for attr in blocks:
            attr.Block()


class _CookRecord(NamedTuple):
    """What the last cook of one chunk saw: its fingerprint, the arrays it signed
    (address -> (array, signature)) so the next one can recognise them without re-reading,
    and the plans it decided on, which a cook on the same fingerprint authors again.
    """

    fingerprint: tuple
    arrays: dict
    plans: list


# Keyed by the chunk's own layer identifier. See `_fingerprint` for why this exists.
_LAST_COOK: dict = {}


def _fingerprint(prims, settings, previous: dict, current: dict) -> tuple:
    """Everything this chunk's output depends on, cheaply.

    The graph re-runs a chunk more often than its inputs actually change: every cook
    upstream re-runs it, including one that leaves its mesh exactly as it was (a Transform
    upstream moving the mesh's parent, say). That is fine for a chunk that costs a
    millisecond and unusable for one that costs forty seconds -- a 4M-triangle reduction.
    Comparing this against the last cook's makes those re-runs cost a few array checksums
    (~0.4s on that same mesh) plus authoring the plans already made, rather than the whole
    decimation.

    It signs the geometry, every primvar and every subset -- not just the point count --
    because an upstream chunk editing a mesh in place is exactly the case that *must* still
    re-cook, and it usually leaves the array lengths alone.
    """
    parts = [tuple(settings)]
    for prim in prims:
        mesh = UsdGeom.Mesh(prim)
        entry = [str(prim.GetPath())]
        entry += [
            surface_samples.checksum(attr.Get(), previous, current)
            for attr in (
                mesh.GetPointsAttr(),
                mesh.GetFaceVertexCountsAttr(),
                mesh.GetFaceVertexIndicesAttr(),
            )
        ]
        entry += [
            (
                pv.GetPrimvarName(),
                pv.GetInterpolation(),
                surface_samples.checksum(_primvar_values(pv), previous, current),
            )
            for pv in UsdGeom.PrimvarsAPI(prim).GetPrimvars()
        ]
        entry += [
            (
                str(subset.GetPrim().GetPath()),
                surface_samples.checksum(subset.GetIndicesAttr().Get(), previous, current),
            )
            for subset in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(prim))
        ]
        parts.append(tuple(entry))
    return tuple(parts)


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------


def _compact(count: int) -> str:
    """1_044_205 -> "1.04M". The readout sits in a fixed-width node header, and a full
    "4,176,819 → 1,044,205 triangles (25.0%)" is simply cut off mid-word there.
    """
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 10_000:
        return f"{count / 1_000:.0f}k"
    return f"{count:,}"


def _report(node, plans, note=""):
    before = sum(p.before for p in plans)
    after = sum(len(p.reduced.triangles) for p in plans)
    if plans:
        share = f"{after * 100.0 / before:.0f}%" if before else "?"
        meshes = f" · {len(plans)} meshes" if len(plans) > 1 else ""
        text = f"{_compact(before)} → {_compact(after)} tris ({share}){meshes}"
    else:
        text = note or "nothing reduced — check the mesh path"
    if readout := node.get_param("readout"):
        readout.value = text
    if out_tris := node.get_param("out_triangles"):
        out_tris.value = after
    logger.debug("%s", text)


def _sync_mode_params(node) -> None:
    """Show only the controls the current settings read: the mode's amount, and Proxy Path
    while Write as Proxy is on."""
    mode = node.get_param("reduce_mode", chunk.is_param_t(str)).value
    visible = {
        MODE_PERCENT: {"keep_percent"},
        MODE_COUNT: {"target_tris"},
        MODE_PLANAR: set(),
    }.get(mode, {"keep_percent"})
    for name in ("keep_percent", "target_tris"):
        param = node.get_param(name)
        if param is None:
            continue
        if name in visible:
            param.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            param.set_state(chunk.ChunkFlags.State.HIDDEN)
    as_proxy = node.get_param("as_proxy", chunk.is_param_t(bool))
    if (proxy_path := node.get_param("proxy_path")) is not None and as_proxy is not None:
        if as_proxy.value:
            proxy_path.unset_state(chunk.ChunkFlags.State.HIDDEN)
        else:
            proxy_path.set_state(chunk.ChunkFlags.State.HIDDEN)


def mode_changed(owner, notice, sender) -> None:
    _sync_mode_params(owner.get_chunk() or owner)


def _settings(node) -> _Settings:
    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)
    return _Settings(
        mode=node.get_param("reduce_mode", is_s).value,
        keep_percent=node.get_param("keep_percent", is_f).value,
        target_tris=node.get_param("target_tris", is_i).value,
        aggressiveness=node.get_param("aggressiveness", is_f).value,
        lock_boundary=node.get_param("lock_boundary", is_b).value,
        keep_uvs=node.get_param("preserve_uvs", is_b).value,
        recurse=node.get_param("recurse", is_b).value,
    )


def _targets(node, settings: _Settings, paths) -> tuple[Usd.Stage, list]:
    """The meshes to reduce, read from a composition that excludes this chunk's own layer.

    So a cook never sees (and re-reduces) the result of the last one. See the module
    docstring. The scratch stage comes back too, and the caller must hold it for as long
    as it reads the prims: they go invalid the moment it is released.
    """
    upstream = chunk_stage.get_upstream_stage(node.get_stage(), node.get_scene(), node)
    return upstream, [
        prim
        for path in paths
        for prim in _mesh_prims(upstream.GetPrimAtPath(path), settings.recurse)
    ]


def cache_key(node):
    """What the cook cache keys a reduction on, beyond the params: the input meshes.

    The same signature a cook compares against its last (`_fingerprint`), so the disk
    cache and the in-memory one agree on what counts as the same input: geometry, every
    primvar and every subset, not just the point count.
    """
    paths = node.get_param("prim_path").resolve_paths()
    if not paths or not _HAS_SIMPLIFY:
        return ()
    settings = _settings(node)
    _upstream, targets = _targets(node, settings, paths)
    return _fingerprint(targets, settings, {}, {})


def execute(node: chunk.Chunk) -> None:
    """Reduce every mesh under the selected prim(s).

    Reactive: any param change, or an upstream chunk's edit to the mesh, re-runs this.
    """
    stage = node.get_stage()

    _sync_mode_params(node)

    layer = node.get_edit_context()
    if not layer:
        return

    if not _HAS_SIMPLIFY:
        _report(node, [], "fast-simplification is not installed — pip install fast-simplification")
        return

    settings = _settings(node)

    paths = node.get_param("prim_path").resolve_paths()
    if not paths:
        _report(node, [], "pick a mesh to reduce")
        return
    as_proxy = node.get_param("as_proxy", chunk.is_param_t(bool)).value
    proxy_paths, problem = _proxy_paths(paths, node.get_param("proxy_path").value) if as_proxy else ({}, "")
    if problem:
        _report(node, [], problem)
        return

    upstream, targets = _targets(node, settings, paths)
    previous = _LAST_COOK.get(layer.identifier)
    signed: dict = {}
    fingerprint = _fingerprint(targets, settings, previous.arrays if previous else {}, signed)
    if previous is not None and previous.fingerprint == fingerprint:
        # Same inputs, same settings: last cook's decimation still holds. The layer was
        # cleared for this cook like any other, so the plans are authored again below.
        plans = previous.plans
    else:
        try:
            plans = _reduce(node, targets, settings)
        except Exception as e:
            # Cancelled, or the process died: the last result stays in view, and the cook
            # fails, so the chunks below wait for a good one.
            if previous is not None:
                _write(stage, layer, previous.plans, proxy_paths)
            cancelled = isinstance(e, sim_bake.Cancelled)
            _report(node, [], "cancelled" if cancelled else f"reduce failed: {e}")
            raise

    # The refreshed record drops the previous generation's array references. Kept before it
    # is written: a first cook runs on a stage of the chunk's inputs alone (skip_cook), where
    # a proxy copy's prim can't be defined, and is run again on the whole stage. Kept first,
    # the second run finds these plans instead of reducing again.
    _remember(layer.identifier, _CookRecord(fingerprint, signed, plans))
    _write(stage, layer, plans, proxy_paths)
    _report(node, plans)


def _write(stage, layer, plans, proxy_paths: dict) -> None:
    """Author plans into the chunk's layer: onto the meshes, or onto their proxy copies."""
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        proxies = _write_proxies(stage, proxy_paths)
        for plan in plans:
            _author(stage, _moved(plan, proxies))
        for source, proxy in proxies.items():
            proxy_setup.set_up_proxy(stage, [source], proxy, copy_of=source)


def _reduce(node, targets, settings: _Settings) -> list:
    """Each target's plan: worked out here, or for a big total in a separate process.

    The cook waits for the process (sim_bake.wait): the chunks below read the reduced mesh,
    so they cook once, after it, and a progress window shows meanwhile, with Cancel.
    """
    if _triangle_total(targets) < _PROCESS_TRIANGLES:
        return [plan for prim in targets if (plan := _plan_mesh(prim, settings)) is not None]
    reads = [read for prim in targets if (read := _read_mesh(prim, settings)) is not None]
    jobs = [read.job for read in reads]
    triangles = _triangle_total(targets)

    def bake(ctx):
        ctx.status(f"reducing {_compact(triangles)} tris…")
        yield sim_bake.WORKER
        return sim_bake.in_subprocess(ctx, reduce_meshes, jobs=jobs)

    results = sim_bake.wait(node, bake, report=lambda text: _report(node, [], text), name="PolyReduce")
    return [plan for read, result in zip(reads, results) if (plan := _finish_plan(read, result)) is not None]


#: Stands for each selected prim's name in Proxy Path.
_NAME = "{name}"


def _proxy_paths(paths, pattern: str) -> tuple[dict, str]:
    """Where each selected prim's proxy copy goes, by Proxy Path.

    Empty, it goes beside the prim as `<name>_proxy`. One prim takes the path as it is;
    several need `{name}` in it, one copy each. A copy is a reference to its prim, so it
    can be neither inside it nor hold it.

    Args:
        paths: The selected prims' paths.
        pattern: Proxy Path, as typed.

    Returns:
        `(each prim's path mapped to its copy's, "")`, or `({}, what is wrong with the path)`.
    """
    sources = [s for s in map(Sdf.Path, map(str, paths)) if s != Sdf.Path.absoluteRootPath]
    pattern = pattern.strip()
    if not pattern:
        return {source: proxy_setup.sibling_path(source) for source in sources}, ""
    if len(sources) > 1 and _NAME not in pattern:
        return {}, f"Proxy Path needs {_NAME} in it for {len(sources)} assets"
    proxies = {}
    for source in sources:
        text = pattern.replace(_NAME, source.name)
        if not Sdf.Path.IsValidPathString(text) or not Sdf.Path(text).IsAbsolutePath() or not Sdf.Path(text).IsPrimPath():
            return {}, f"Proxy Path {text} is not a prim path"
        proxy = Sdf.Path(text)
        if proxy.HasPrefix(source) or source.HasPrefix(proxy):
            return {}, f"Proxy Path {proxy} can't be inside {source} or hold it"
        proxies[source] = proxy
    if len(set(proxies.values())) < len(proxies):
        return {}, "Proxy Path gives two assets the same copy"
    return proxies, ""


def _write_proxies(stage, proxy_paths: dict) -> dict:
    """A copy of each selected prim where Proxy Path puts it, to reduce instead of the prim.

    Args:
        stage: The stage, edited at the chunk's layer.
        proxy_paths: Each selected prim's path, mapped to its copy's (_proxy_paths).

    Returns:
        The same, for the prims that exist.
    """
    proxies = {}
    for source, proxy in proxy_paths.items():
        if not stage.GetPrimAtPath(source):
            continue
        stage.DefinePrim(proxy).GetReferences().AddInternalReference(source)
        proxies[source] = proxy
    return proxies


def _moved(plan: _Plan, proxies: dict) -> _Plan:
    """`plan` written onto the proxy copy of the prim it was read from, if it has one."""
    for source, proxy in proxies.items():
        if plan.path.HasPrefix(source):
            return replace(
                plan,
                path=plan.path.ReplacePrefix(source, proxy),
                subset_indices=[(path.ReplacePrefix(source, proxy), indices) for path, indices in plan.subset_indices],
            )
    return plan


def _remember(layer_id: str, record: _CookRecord) -> None:
    """Keep record as the last cook of the chunk whose layer is layer_id."""
    if len(_LAST_COOK) > 256:
        # A deleted chunk leaves its entry behind, and each one holds references to the
        # arrays it signed. Rather than track chunk lifetimes, drop the lot occasionally.
        _LAST_COOK.clear()
    _LAST_COOK[layer_id] = record


def _triangle_total(prims) -> int:
    """About how many triangles prims have between them, from their face counts alone."""
    total = 0
    for prim in prims:
        counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get()
        if counts:
            total += int(np.maximum(np.asarray(counts, dtype=np.int64) - 2, 0).sum())
    return total


_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def main():
    chunk_api = chunk.Chunk("Poly Reduce")

    prim_path = chunk.LineEdit(
        "prim_path",
        "Mesh",
        flags=_PF.LABELED | _PF.PRIM_SELECTION,
        tooltip="The mesh to reduce — or any prim above one. Every mesh underneath is\n"
        "reduced separately, each to its own share of the target.",
    )
    chunk_api.add_param(prim_path)

    chunk_api.add_param(
        chunk.Label(
            "readout",
            # The widget paints the *label* over the value at construction
            # (ChunkParamWidgetBase.setup_chunk_widget sets value then label), so the
            # starting text has to be the label; the value takes over from the first cook.
            "not reduced yet",
            default_value="not reduced yet",
            tooltip="What the last cook produced.",
        )
    )

    mode = chunk.Menu(
        "reduce_mode",
        "Mode",
        menu_items=_MODES,
        default_value=MODE_PERCENT,
        tooltip="Percentage: keep this share of the triangles.\n"
        "Triangle Count: reduce to roughly this many triangles, whatever the input size.\n"
        "Planar Only: collapse only what costs no error at all — thins out\n"
        "over-tessellated flat areas and leaves every curved one untouched.",
    )
    chunk_api.add_param(mode)

    chunk_api.add_param(
        chunk.Slider(
            "keep_percent",
            "Keep %",
            min_value=1,
            max_value=100,
            default_value=25,
            tooltip="Share of the original triangles to keep. Quadric decimation spends the\n"
            "budget where the shape needs it, so flat regions thin out first and\n"
            "silhouettes hold on longest.",
        )
    )
    chunk_api.add_param(
        chunk.Spinbox(
            "target_tris",
            "Target Triangles",
            min_value=_MIN_TRIANGLES,
            max_value=100_000_000,
            default_value=5000,
            is_int=True,
            tooltip="Reduce to about this many triangles. The count a game engine or a\n"
            "collision proxy asks for is absolute, not relative to whatever the\n"
            "source happened to be.",
        )
    )

    proxy_path = chunk.LineEdit(
        "proxy_path",
        "Proxy Path",
        flags=_PF.LABELED,
        tooltip="Where Write as Proxy puts the reduced copy. Empty: beside the asset, as\n"
        "<name>_proxy. With several assets, put {name} where each one's name goes,\n"
        "e.g. /proxies/{name}.",
    )
    proxy_path.set_state(chunk.ChunkFlags.State.HIDDEN)  # shown while Write as Proxy is on
    quality = chunk.Group("Quality", icon="sliders", flags=_GF)
    quality.add_params(
        [
            chunk.Slider(
                "aggressiveness",
                "Aggressiveness",
                min_value=0,
                max_value=10,
                default_value=5,
                tooltip="How fast the error threshold is allowed to grow. Lower is slower and\n"
                "holds the shape more closely; below about 5 it may stop short of the\n"
                "target. Higher is faster and looser: at 7, a 660k-triangle remeshed\n"
                "armchair taken to 3k lost 13% of its volume to slab-like cuts, where 5\n"
                "lost 1% and still hit the count.",
            ),
            chunk.Checkbox(
                "lock_boundary",
                "Lock Boundary",
                default_value=False,
                tooltip="Freeze open edges completely, instead of only stopping them from\n"
                "collapsing inward. Stops a cloth or leaf card's outline from\n"
                "eroding — but with Preserve UVs on, UV seams are boundaries too,\n"
                "so a heavily seamed asset will barely reduce.",
            ),
            chunk.Checkbox(
                "preserve_uvs",
                "Preserve UVs",
                default_value=True,
                tooltip="Split vertices at UV (and other faceVarying) discontinuities before\n"
                "reducing, so texturing survives. Turn it off for a collision or\n"
                "occlusion proxy that is never textured: fewer vertices, faster, and\n"
                "it reduces further.",
            ),
            chunk.Checkbox(
                "recurse",
                "Include Children",
                default_value=True,
                tooltip="Reduce every mesh under the selected prim, not just the prim itself.",
            ),
            chunk.Checkbox(
                "as_proxy",
                "Write as Proxy",
                default_value=False,
                tooltip="Leave the mesh as it is, and write a reduced copy as its proxy, at Proxy\n"
                "Path: purpose=proxy on the copy, purpose=render on the mesh, and the mesh's\n"
                "proxyPrim pointing at the copy. Viewports that draw proxies show the reduced\n"
                "copy; final renders show the full mesh. Its UVs and materials come along.",
            ),
            proxy_path,
        ]
    )
    chunk_api.add_param(quality)
    chunk_api.add_callback(mode_changed, sender=quality.get_param("as_proxy"))

    out_triangles = chunk.Slider(
        "out_triangles",
        "Triangles Out",
        is_int=True,
        max_value=100_000_000,
        flags=_PF.OUTPUT,
        tooltip="Read-only. Triangles the last cook produced, summed over every mesh.\n"
        "Other chunks can read it as @Poly Reduce.out_triangles@.",
    )
    out_triangles.set_state(chunk.ChunkFlags.State.DISABLED)
    chunk_api.add_param(out_triangles)

    chunk_api.add_callback(mode_changed, sender=mode)
    _sync_mode_params(chunk_api)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="cube",
        requirements=["fast-simplification>=0.2,<1", "numpy>=2,<3"],
        cache_cook=True,
        # A big one waits behind the progress window: a slider drag sets its value once.
        cook_on_release=True,
        description=(
            "Reduces a mesh's triangle count using quadric error metrics — the method that\n"
            "spends the triangle budget on silhouettes and creases and takes it out of flat\n"
            "regions.\n\n"
            "UV seams and material (GeomSubset) boundaries are preserved by splitting those\n"
            "vertices before the collapse, so a reduced asset still textures and still\n"
            "renders with the right materials. Vertex primvars — colours, skin weights —\n"
            "are carried across. Authored normals are dropped so the renderer computes them\n"
            "for the new surface."
        ),
    )
