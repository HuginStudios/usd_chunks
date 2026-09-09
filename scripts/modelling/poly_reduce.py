"""Poly Reduce chunk — quadric-error decimation of a mesh, or a whole hierarchy of them.

The reduction itself is Garland & Heckbert's quadric error metric, via
`fast_simplification` (Sven Forstmann's C++ implementation, the one PyVista ships). QEM
is the method every DCC's decimator is built on: each edge collapse is scored by how far
it moves the surface away from the original planes meeting at that edge, and the cheapest
collapse is always taken next. That is what "reduce a lot without destroying the shape"
means in practice — flat regions lose almost all of their triangles, silhouettes and
creases keep theirs, because moving them is expensive.

What actually makes or breaks a reducer on a real asset is not the collapse maths, though
— it's everything hanging off the topology. This one:

* **triangulates first** (fan), remembering which original face-corner each triangle
  corner came from, so every per-corner value can be carried back afterwards;
* **splits vertices at UV seams** before decimating, by welding on (point index, all
  faceVarying primvar values) rather than point index alone. A seam then *is* an open
  border to the decimator, which will not collapse a border vertex into an interior one —
  so the UV layout survives the reduction instead of smearing across the seam. The
  faceVarying primvars come back out as vertex primvars on the reduced mesh, which is
  exactly what the split bought us;
* **keeps materials** by putting each `GeomSubset`'s face membership into that same weld
  key. Two faces with different materials no longer share a vertex, so no collapse can
  ever straddle the boundary, and every surviving triangle still belongs to exactly one
  subset — its `indices` are rewritten to match;
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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk
from usd_chunks.plugins import chunk_usd

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
# array maths — no USD, no Qt, unit-testable on its own
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReducedMesh:
    """The result of one decimation, plus the two lookups needed to rebuild its attributes.

    `source_point[j]` / `source_corner[j]` name one original vertex and one original
    face-corner that ended up merged into reduced vertex `j` — the representative whose
    primvar values that vertex inherits. Which of several merged sources is picked is
    arbitrary and always has been in every decimator; what matters is that it is one of
    them, so the value is real data from the neighbourhood rather than an average that
    belongs to nothing.

    Both are None when the caller did not ask for them, or when the collapse history could
    not be resolved. That is a "no attributes survive" answer, not a "use these anyway"
    one — a wrong mapping would scatter UVs across the mesh, which is far worse than a
    reduced mesh that has lost them.
    """

    points: np.ndarray  # (n, 3) float64
    triangles: np.ndarray  # (m, 3) int64
    source_point: np.ndarray | None  # (n,) index into the original points array
    source_corner: np.ndarray | None  # (n,) index into the original face-corner array


def fan_triangulate(counts, indices):
    """Fan-triangulate a polygon mesh: `(triangles, corners, face_of_triangle)`.

    `corners` is the same shape as `triangles` but indexes the flat face-corner array
    (i.e. positions within `faceVertexIndices`) instead of the point array — that is what
    lets a faceVarying primvar be read per triangle corner afterwards.

    Faces with fewer than three vertices contribute nothing but still consume their slots
    in `indices`, so offsets are computed over every face, not just the usable ones.
    """
    counts = np.asarray(counts, dtype=np.int64)
    indices = np.asarray(indices, dtype=np.int64)
    if counts.size == 0 or indices.size == 0:
        empty_i = np.zeros((0, 3), dtype=np.int64)
        return empty_i, empty_i, np.zeros(0, dtype=np.int64)

    offsets = np.concatenate(([0], np.cumsum(counts)[:-1]))
    per_face = np.maximum(counts - 2, 0)
    total = int(per_face.sum())
    if total == 0:
        empty_i = np.zeros((0, 3), dtype=np.int64)
        return empty_i, empty_i, np.zeros(0, dtype=np.int64)

    face_of_tri = np.repeat(np.arange(counts.size, dtype=np.int64), per_face)
    # Position of each triangle within its own face's fan, without a Python loop.
    run_start = np.repeat(np.cumsum(per_face) - per_face, per_face)
    step = np.arange(total, dtype=np.int64) - run_start

    base = offsets[face_of_tri]
    corners = np.stack([base, base + step + 1, base + step + 2], axis=1)
    return indices[corners], corners, face_of_tri


# A packed weld key has to stay inside int64. The margin below 2**63 is there so the guard
# can be checked before the multiply that would overflow, not after.
_KEY_LIMIT = 1 << 62


def component_codes(values) -> list:
    """Label each *component* of a per-element value with a small integer, one array each.

    Labelling `st.x` and `st.y` separately and combining the two labels afterwards
    partitions the corners exactly the same way as labelling whole `(u, v)` rows would —
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
        width = int(column.max()) + 1 if column.size else 1
        if width <= 0 or span > _KEY_LIMIT // width:
            return None
        key = key * width + column
        span *= width
    return key


def combine_codes(*code_arrays) -> np.ndarray:
    """Fold several per-element label arrays into one, again densely numbered."""
    columns = [np.asarray(c, dtype=np.int64).reshape(-1) for c in code_arrays if c is not None]
    if not columns:
        return None
    if len(columns) == 1:
        return columns[0]
    key = _pack_key(columns)
    if key is None:
        _, inverse = np.unique(np.stack(columns, axis=1), axis=0, return_inverse=True)
    else:
        _, inverse = np.unique(key, return_inverse=True)
    return np.asarray(inverse, dtype=np.int64).reshape(-1)


def _weld(points, triangles, corners, split_codes):
    """Split vertices so that each distinct (point, split code) pair is its own vertex.

    With `split_codes` None this is a plain compaction — unreferenced points dropped,
    indices renumbered — which is still worth doing: the decimator's own bookkeeping
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

    # Welding can make two corners of a triangle the same vertex (a degenerate face in the
    # original, or a sliver whose corners shared everything). The decimator's edge
    # bookkeeping has no meaningful collapse for those, so drop them and re-compact.
    keep = (
        (welded[:, 0] != welded[:, 1])
        & (welded[:, 1] != welded[:, 2])
        & (welded[:, 0] != welded[:, 2])
    )
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
    """welded vertex -> reduced vertex, derived straight from the collapse history.

    `collapses[i] = [i0, i1]` says vertex `i1` was merged into `i0`. Each vertex can only
    be collapsed away once, so the pairs form a forest and resolving every chain to its
    root is pointer-jumping — a handful of vectorised passes, no ordering to respect.
    A surviving triangle is then an original triangle whose three roots are still distinct
    (an edge collapse deletes a triangle exactly when two of its corners merge), and the
    decimator numbers the survivors in original-index order, which `np.searchsorted` over
    the sorted survivors reproduces.

    This used to call the library's `replay_simplification`, which returns the same answer
    — but gets there via `np.delete` inside a Python loop over the isolated points it has
    to clean up. On a mesh made of thousands of disconnected cards (a tree's twigs, say)
    that list is enormous and the loop goes quadratic: 0.4s of decimation turned into
    **698 seconds** on a 505k-triangle fir tree. Deriving it here is exact, and free.

    Returns None if the derivation disagrees with what the decimator actually produced;
    attributes are then dropped rather than scattered onto the wrong vertices.
    """
    parent = np.arange(n_points, dtype=np.int64)
    collapses = np.asarray(collapses, dtype=np.int64).reshape(-1, 2)
    if collapses.size:
        parent[collapses[:, 1]] = collapses[:, 0]
        for _ in range(64):  # a cap, not an expected bound — depth is logarithmic in practice
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
        else:  # pragma: no cover - would mean the collapse pairs are not a forest
            logger.debug("collapse chains did not settle; reduced mesh will carry no primvars")
            return None

    rooted = parent[triangles]
    kept = (
        (rooted[:, 0] != rooted[:, 1])
        & (rooted[:, 1] != rooted[:, 2])
        & (rooted[:, 0] != rooted[:, 2])
    )
    survivors = np.unique(rooted[kept])
    if len(survivors) != n_reduced:
        logger.debug(
            "derived %d surviving vertices but the decimation produced %d; "
            "reduced mesh will carry no primvars",
            len(survivors),
            n_reduced,
        )
        return None

    mapping = np.full(n_points, -1, dtype=np.int64)
    alive = np.flatnonzero(np.isin(parent, survivors))
    mapping[alive] = np.searchsorted(survivors, parent[alive])
    return mapping


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
    if not prim or not prim.IsValid():
        return []
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


@dataclass
class _Plan:
    """Everything one mesh's cook decided, held until every mesh has decided.

    Nothing is authored while the reduction is still running. A chunk's layer is its
    output, and clearing it before the work is done leaves the stage showing a half-built
    mesh for as long as the cook takes — and showing nothing at all if the cook then
    raises.
    """

    path: Sdf.Path
    reduced: ReducedMesh
    before: int
    vertex_primvars: list  # (name, type name, Vt values)
    blocked_primvars: list  # names
    subset_indices: list  # (subset path, Vt.IntArray)
    drop_subdivision: bool


def _plan_mesh(prim, keep_uvs, mode, keep_percent, target_tris, aggressiveness, lock_boundary):
    """Work out what `prim` reduces to, without touching the stage."""
    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None

    points = np.asarray(points, dtype=np.float64)
    counts = np.asarray(counts, dtype=np.int64)
    triangles, corners, face_of_tri = fan_triangulate(counts, indices)
    if len(triangles) <= _MIN_TRIANGLES:
        return None

    n_faces = len(counts)
    corner_face = np.repeat(np.arange(n_faces, dtype=np.int64), counts)
    n_corners = int(counts.sum())

    subsets, subset_code_per_face, membership_rows = _face_subsets(prim, n_faces)

    # Everything that has to stay unmixed after a collapse goes into the weld key: UV (and
    # other faceVarying) discontinuities, and material boundaries.
    split_columns = []
    if subset_code_per_face is not None:
        split_columns.append(subset_code_per_face[corner_face])

    face_varying = []
    vertex_varying = []
    blocked = []
    for primvar in UsdGeom.PrimvarsAPI(prim).GetPrimvars():
        interpolation = primvar.GetInterpolation()
        if interpolation == UsdGeom.Tokens.constant:
            continue  # one value for the whole mesh — topology cannot invalidate it
        values = _primvar_values(primvar)
        if values is None:
            continue
        name = primvar.GetPrimvarName()
        if interpolation == UsdGeom.Tokens.uniform:
            blocked.append(name)
        elif interpolation == UsdGeom.Tokens.faceVarying:
            if keep_uvs and len(values) == n_corners:
                face_varying.append((name, primvar.GetTypeName(), values))
                split_columns.extend(component_codes(values))
            else:
                blocked.append(name)
        elif len(values) == len(points):
            vertex_varying.append((name, primvar.GetTypeName(), values))
        else:
            blocked.append(name)

    split_codes = combine_codes(*split_columns) if split_columns else None

    reduced = reduce_triangles(
        points,
        triangles,
        corners,
        split_codes,
        target_count=target_triangle_count(mode, len(triangles), keep_percent, target_tris),
        planar_only=mode == MODE_PLANAR,
        aggressiveness=aggressiveness,
        lock_boundary=lock_boundary,
        carry_attributes=bool(face_varying or vertex_varying or subsets),
    )
    if reduced is None:
        return None

    if reduced.source_corner is None:
        # No usable mapping, so nothing per-vertex can be carried honestly. Block what the
        # old topology described rather than leaving arrays of the wrong length behind.
        carried = []
        blocked += [name for name, _, _ in face_varying + vertex_varying]
    else:
        carried = [
            (name, type_name, _remap(values, reduced.source_corner))
            for name, type_name, values in face_varying
        ]
        carried += [
            (name, type_name, _remap(values, reduced.source_point))
            for name, type_name, values in vertex_varying
        ]

    subset_indices = []
    if subsets and reduced.source_corner is not None:
        # A surviving triangle inherits the material region of any of its corners: no
        # collapse can cross a region boundary, because the weld above gave the two sides
        # separate vertices in the first place.
        face_code = subset_code_per_face[corner_face][reduced.source_corner[reduced.triangles[:, 0]]]
        for column, subset in enumerate(subsets):
            kept = np.flatnonzero(membership_rows[face_code, column]).astype(np.int32)
            subset_indices.append((subset.GetPrim().GetPath(), Vt.IntArray.FromNumpy(kept)))

    scheme = mesh.GetSubdivisionSchemeAttr().Get()
    return _Plan(
        path=prim.GetPath(),
        reduced=reduced,
        before=len(triangles),
        vertex_primvars=carried,
        blocked_primvars=blocked,
        subset_indices=subset_indices,
        drop_subdivision=bool(scheme) and scheme != UsdGeom.Tokens.none,
    )


def _author(stage, plan: _Plan) -> None:
    """Write one plan into the current edit target."""
    prim = stage.GetPrimAtPath(plan.path)
    if not prim or not prim.IsValid():
        return
    mesh = UsdGeom.Mesh(prim)
    reduced = plan.reduced

    points32 = np.ascontiguousarray(reduced.points, dtype=np.float32)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(points32))
    mesh.GetFaceVertexIndicesAttr().Set(
        Vt.IntArray.FromNumpy(np.ascontiguousarray(reduced.triangles.reshape(-1), dtype=np.int32))
    )
    mesh.GetFaceVertexCountsAttr().Set(
        Vt.IntArray.FromNumpy(np.full(len(reduced.triangles), 3, dtype=np.int32))
    )
    # Straight from numpy: Gf.Vec3f refuses np.float32 scalars, so building the two corners
    # component-by-component would raise here rather than at the obvious place.
    mesh.GetExtentAttr().Set(
        Vt.Vec3fArray.FromNumpy(
            np.ascontiguousarray(np.stack([points32.min(axis=0), points32.max(axis=0)]))
        )
    )
    if plan.drop_subdivision:
        # The reduction approximates the polygonal cage, not the limit surface. Leaving
        # subdivision on would smooth an already-approximate mesh a second time and shrink
        # it away from the shape the user was looking at.
        mesh.GetSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)

    for name in _STALE_ATTRS:
        attr = prim.GetAttribute(name)
        if attr and attr.HasAuthoredValue():
            attr.Block()

    api = UsdGeom.PrimvarsAPI(prim)
    for name in plan.blocked_primvars:
        existing = api.GetPrimvar(name)
        if existing:
            existing.GetAttr().Block()
    for name, type_name, values in plan.vertex_primvars:
        primvar = api.CreatePrimvar(name, type_name, UsdGeom.Tokens.vertex)
        primvar.Set(values)
        # A remapped primvar is already one value per reduced vertex; an inherited index
        # array from the source would re-scramble it.
        primvar.BlockIndices()

    for subset_path, indices in plan.subset_indices:
        subset = UsdGeom.Subset(stage.GetPrimAtPath(subset_path))
        if subset:
            subset.GetIndicesAttr().Set(indices)


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------


def _report(node, plans, note=""):
    before = sum(p.before for p in plans)
    after = sum(len(p.reduced.triangles) for p in plans)
    if plans:
        share = f"{after * 100.0 / before:.1f}%" if before else "—"
        text = f"{before:,} → {after:,} triangles ({share}) over {len(plans)} mesh(es)"
    else:
        text = note or "nothing reduced — check the mesh path"
    if readout := node.get_param("readout"):
        readout.value = text
    if out_tris := node.get_param("out_triangles"):
        out_tris.value = after
    logger.debug("%s", text)


def _sync_mode_params(node) -> None:
    """Show only the control the current mode actually reads."""
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


def mode_changed(owner, notice, sender) -> None:
    _sync_mode_params(owner.get_chunk() or owner)


def execute(node: chunk.Chunk) -> None:
    """Reduce every mesh under the selected prim(s).

    Reactive: any param change, or an upstream chunk's edit to the mesh, re-runs this.
    """
    stage = node.get_stage()
    if not stage:
        return

    _sync_mode_params(node)

    layer = node.get_edit_context()
    if not layer:
        return

    if not _HAS_SIMPLIFY:
        _report(node, [], "fast-simplification is not installed — pip install fast-simplification")
        return

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    mode = node.get_param("reduce_mode", is_s).value
    keep_percent = node.get_param("keep_percent", is_f).value
    target_tris = node.get_param("target_tris", is_i).value
    aggressiveness = node.get_param("aggressiveness", is_f).value
    lock_boundary = node.get_param("lock_boundary", is_b).value
    keep_uvs = node.get_param("preserve_uvs", is_b).value
    recurse = node.get_param("recurse", is_b).value

    paths = node.get_param("prim_path").resolve_paths()
    if not paths:
        _clear_layer(layer)
        _report(node, [], "pick a mesh to reduce")
        return

    # Read from a composition that excludes this chunk's own layer, so a cook never sees
    # (and re-reduces) the result of the last one. See the module docstring.
    upstream = chunk_usd.get_upstream_stage(stage, node.get_scene(), node)

    plans = []
    for path in paths:
        for prim in _mesh_prims(upstream.GetPrimAtPath(path), recurse):
            plan = _plan_mesh(
                prim, keep_uvs, mode, keep_percent, target_tris, aggressiveness, lock_boundary
            )
            if plan is not None:
                plans.append(plan)

    # Only now, with every mesh decided, does the old output go away.
    _clear_layer(layer)
    with Usd.EditContext(stage, layer):
        for plan in plans:
            _author(stage, plan)

    _report(node, plans)


def _clear_layer(layer) -> None:
    if lreg := chunk.get_layer_manager():
        lreg.clear_layer(layer.identifier)


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

    quality = chunk.Group("Quality", flags=_GF)
    quality.add_params(
        [
            chunk.Slider(
                "aggressiveness",
                "Aggressiveness",
                min_value=0,
                max_value=10,
                default_value=7,
                tooltip="How fast the error threshold is allowed to grow. 7 is the usual\n"
                "balance. Lower is slower and holds the shape more closely; it may\n"
                "also stop short of the target. Higher is faster and looser.",
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
        ]
    )
    chunk_api.add_param(quality)

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
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MODELLING,
        requirements=["fast-simplification>=0.2,<1", "numpy>=2,<3"],
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
