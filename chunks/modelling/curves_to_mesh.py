"""Curves to Mesh chunk -- sweeps a tube (or lays a flat ribbon) along a BasisCurves and
gives it UVs.

Why this exists: **a curve cannot carry a texture in most renderers.** Measured across the
three delegates in this bundle, rendering the same `BasisCurves` with and without a
`primvars:st` and comparing the images: Storm honours curve UVs, V-Ray and Cycles return
pixel-identical renders -- they ignore them entirely. So a material bound to a curve shows its
base colour and nothing else, which is exactly what was reported of the Vine chunk.

A `Mesh` with `primvars:st` textures everywhere, V-Ray included (verified the same way). So
this turns curves into geometry rather than trying to talk renderers into shading curves.

Deliberately its own chunk rather than an output on Vine. There are already two curve
producers (`vine`, `add_curves`) and referenced curves are a third, and none of them should
each grow their own sweep. It is also the shape Vine already commits to: it publishes points
for Scatter Points to instance leaves rather than generating leaves itself.

**The centreline is evaluated, not guessed.** Vine's Smooth form writes a cubic B-spline, and a
B-spline does not pass through its control points -- laying a tube on the CVs would leave the
mesh visibly off the curve it came from. `_centreline` evaluates the basis, so the tube sits
where the curve is drawn.

**The frame is rotation-minimising** (Wang et al.'s double reflection). The naive alternative --
re-deriving a frame per sample from a fixed up-vector -- spins the cross-section as a strand
turns, which a texture shows up immediately as a barber-pole twist.

**A ribbon lies flat across the stage's up axis** -- a road, a river, a path -- the curve's
width wide. Its sideways direction is up x tangent rather than the rotation-minimising frame:
a ribbon that is meant to lie on the ground has to stay level however the curve winds, which
is exactly the twist the frame exists to keep out of a tube. u runs across it and v along, as
on a tube, so a texture laid out the other way (a road's lines running across the image) wants
a UV Transform rotation.

Requires: pxr (OpenUSD), usd_chunks, numpy (a sweep is array work over every sample).
"""

from __future__ import annotations

import logging
import math

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk
from usd_chunks import chunk_stage

logger = logging.getLogger(__name__)

_PF = chunk.ChunkFlags.ParamFlags
_GF = chunk.ChunkFlags.GroupFlags.COLLAPSABLE | chunk.ChunkFlags.GroupFlags.LABELED

#: How v is laid out along a strand.
#:
#: Proportional is the default because the other two are traps at a vine's proportions. A
#: strand 15 units long and 1.3 around gets the whole texture squeezed into its length by Per
#: Strand -- stretched twelve to one, which reads as no texture at all rather than as a
#: stretched one. Proportional measures v in circumferences, so one tile is square and a
#: checker looks like a checker.
_UV_PROPORTIONAL = "Proportional"
_UV_PER_STRAND = "Per Strand"
_UV_BY_LENGTH = "By Length"
_UV_MODES = [_UV_PROPORTIONAL, _UV_PER_STRAND, _UV_BY_LENGTH]

_PROFILE_TUBE = "Tube"
_PROFILE_RIBBON = "Ribbon"
_PROFILES = [_PROFILE_TUBE, _PROFILE_RIBBON]

#: Below this a segment is a duplicated point, not a direction -- vine pins its ends by
#: tripling them, so every strand it writes has a few of these.
_EPSILON = 1e-9


# ---------------------------------------------------------------------------
# reading the curves
# ---------------------------------------------------------------------------

def _curve_prims(prim, recurse: bool):
    """Every BasisCurves at or under prim."""
    if not (prim and prim.IsValid()):
        return []
    if not recurse:
        curves = UsdGeom.BasisCurves(prim)
        return [prim] if curves else []
    return [p for p in Usd.PrimRange(prim) if UsdGeom.BasisCurves(p)]


def _widths_per_vertex(curves, point_count: int) -> np.ndarray:
    """Widths as one value per control point, whatever interpolation they were authored at.

    A constant width is one number for the whole prim; vertex and varying are already per-CV
    for the purposes here. Anything missing falls back to a hairline rather than zero, since a
    tube of radius zero is invisible and reads as the chunk having failed.
    """
    widths = curves.GetWidthsAttr().Get()
    if not widths:
        return np.full(point_count, 0.1, dtype=np.float64)
    widths = np.asarray(widths, dtype=np.float64)
    if len(widths) == 1:
        return np.full(point_count, float(widths[0]), dtype=np.float64)
    if len(widths) < point_count:
        return np.pad(widths, (0, point_count - len(widths)), mode="edge")
    return widths[:point_count]


def _read_strands(prim):
    """(points, radii) per strand, in world space.

    World space because the output Mesh is authored without a transform of its own: the curves
    may sit under any xform, and a tube that ignored it would land somewhere else entirely.
    """
    curves = UsdGeom.BasisCurves(prim)
    points = curves.GetPointsAttr().Get()
    counts = curves.GetCurveVertexCountsAttr().Get()
    if not points or not counts:
        return [], "", ""

    points = np.asarray(points, dtype=np.float64)
    widths = _widths_per_vertex(curves, len(points))

    matrix = np.asarray(
        UsdGeom.XformCache().GetLocalToWorldTransform(prim), dtype=np.float64
    )
    # Row-vector convention, matching Gf: p * M, with the translation in the last row.
    points = points @ matrix[:3, :3] + matrix[3, :3]
    # A uniformly scaled xform has to scale the tube too, or a curve inside a scaled group
    # comes out the wrong thickness. Cube root of the determinant is the average scale, which
    # is the honest answer for a non-uniform one.
    scale = abs(float(np.linalg.det(matrix[:3, :3]))) ** (1.0 / 3.0)
    widths = widths * (scale if scale > _EPSILON else 1.0)

    kind = curves.GetTypeAttr().Get() or UsdGeom.Tokens.linear
    basis = curves.GetBasisAttr().Get() or UsdGeom.Tokens.bspline

    strands, at = [], 0
    for count in counts:
        count = int(count)
        strands.append((points[at:at + count], widths[at:at + count] * 0.5))
        at += count
    return strands, kind, basis


# ---------------------------------------------------------------------------
# the centreline
# ---------------------------------------------------------------------------

def _bspline_weights(t: np.ndarray) -> np.ndarray:
    """Uniform cubic B-spline basis at parameters t, as (len(t), 4)."""
    t2, t3 = t * t, t * t * t
    return np.stack([
        (1.0 - 3.0 * t + 3.0 * t2 - t3),
        (4.0 - 6.0 * t2 + 3.0 * t3),
        (1.0 + 3.0 * t + 3.0 * t2 - 3.0 * t3),
        t3,
    ], axis=1) / 6.0


def _catmullrom_weights(t: np.ndarray) -> np.ndarray:
    """Catmull-Rom basis at parameters t, as (len(t), 4)."""
    t2, t3 = t * t, t * t * t
    return np.stack([
        -t3 + 2.0 * t2 - t,
        3.0 * t3 - 5.0 * t2 + 2.0,
        -3.0 * t3 + 4.0 * t2 + t,
        t3 - t2,
    ], axis=1) / 2.0


def _centrelines(points, values, counts, kind, basis, per_segment: int):
    """The polylines the tubes are swept along, every strand at once.

    Linear curves are their own control points. Cubic ones are evaluated, because a B-spline
    does not pass through its CVs -- sweeping the CVs directly leaves the mesh beside the curve
    the renderer draws, worst at the ends, which is where Vine pins its roots.

    `values` are per-CV columns (the radius, the growth primvar) that ride the same basis as
    the positions, so the taper the pipe model produced survives into the tube instead of
    being re-sampled by eye.

    Args:
        points: (N, 3) control points of every strand, end to end.
        values: (N, k) per-CV values to carry.
        counts: CVs per strand.
        kind, basis: The curves' type and basis.
        per_segment: Samples per cubic segment.

    Returns:
        (points, values, counts) of the evaluated samples.
    """
    counts = np.asarray(counts, dtype=np.int64)
    if kind != UsdGeom.Tokens.cubic or not len(counts):
        return points, values, counts
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    # Strands shorter than a cubic segment are drawn as their control points, as before.
    cubic = counts >= 4
    weights_of = _catmullrom_weights if basis == UsdGeom.Tokens.catmullRom else _bspline_weights
    # vstep is 1 for both bases handled here; bezier (vstep 3) is not, and is evaluated as a
    # B-spline rather than wrongly in some third way.
    weights = weights_of(np.linspace(0.0, 1.0, per_segment + 1)[:-1])
    last = weights_of(np.asarray([1.0]))[0]
    segments = np.where(cubic, counts - 3, 0)
    out_counts = np.where(cubic, segments * per_segment + 1, counts)

    hull = np.repeat(starts, segments) + (np.arange(segments.sum()) - np.repeat(
        np.cumsum(segments) - segments, segments))
    corner = hull[:, None] + np.arange(4)
    both = np.concatenate((points, values), axis=1)
    sampled = np.matmul(weights, both[corner])                 # (S, per_segment, 3 + k)
    closing = last @ both[starts[cubic][:, None] + (counts[cubic] - 4)[:, None] + np.arange(4)]

    out = np.empty((int(out_counts.sum()), both.shape[1]))
    out_starts = np.concatenate(([0], np.cumsum(out_counts)[:-1]))
    # Each cubic strand's samples, then its closing sample; other strands copied as they are.
    seg_rows = np.repeat(out_starts, segments) + (np.arange(segments.sum()) - np.repeat(
        np.cumsum(segments) - segments, segments)) * per_segment
    out[(seg_rows[:, None] + np.arange(per_segment)).reshape(-1)] = sampled.reshape(-1, both.shape[1])
    out[out_starts[cubic] + out_counts[cubic] - 1] = closing
    for k in np.flatnonzero(~cubic).tolist():
        out[out_starts[k]:out_starts[k] + counts[k]] = both[starts[k]:starts[k] + counts[k]]
    return out[:, :3], out[:, 3:], out_counts


def _drop_repeats(points, values, counts):
    """Remove consecutive identical samples within a strand: they have no direction to frame."""
    if not len(points):
        return points, values, counts
    strand = np.repeat(np.arange(len(counts)), counts)
    step = np.linalg.norm(np.diff(points, axis=0), axis=1)
    keep = np.concatenate(([True], (step > _EPSILON) | (strand[1:] != strand[:-1])))
    return points[keep], values[keep], np.bincount(strand[keep], minlength=len(counts))


def _frames(points, counts=None):
    """A rotation-minimising frame per sample: (tangents, normals, binormals).

    Wang et al., *Computation of Rotation Minimizing Frames* (2008), double reflection. Each
    frame is carried from the previous one by reflecting twice, so the cross-section never
    spins about the curve. Re-deriving a frame per sample from a fixed up-vector is the usual
    shortcut and it twists -- on a textured tube that reads as a barber pole.

    The recurrence is sequential along a strand but independent between strands, so it runs
    down all of them at once: step i of every strand still that long, as arrays. Strands go
    longest first, so the ones still running are always a prefix and a slice, not a mask.
    The arithmetic is the per-strand loop's, term for term.

    Args:
        points: (N, 3) samples of every strand, end to end.
        counts: Samples per strand; None for one strand.
    """
    n = len(points)
    counts = np.asarray([n] if counts is None else counts, dtype=np.int64)
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    last = starts + counts - 1

    tangents = np.zeros((n, 3))
    tangents[:-1] = np.diff(points, axis=0)
    tangents[last] = tangents[np.maximum(last - 1, starts)]
    lengths = np.linalg.norm(tangents, axis=1, keepdims=True)
    tangents = tangents / np.where(lengths > _EPSILON, lengths, 1.0)

    # Any vector not parallel to the first tangent seeds the frame; which one is arbitrary,
    # it only fixes where the UV seam falls.
    first_t = tangents[starts]
    seed = np.where((np.abs(first_t[:, 2]) > 0.9)[:, None], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
    first = np.cross(first_t, seed)
    first /= np.maximum(np.linalg.norm(first, axis=1), _EPSILON)[:, None]

    steps = np.zeros((n, 3))
    steps[:-1] = np.diff(points, axis=0)
    step_sq = steps[:, 0] * steps[:, 0] + steps[:, 1] * steps[:, 1] + steps[:, 2] * steps[:, 2]

    order = np.argsort(-counts, kind="stable")
    ordered_starts = starts[order]
    running = counts[order] - 1               # steps each strand takes, longest first
    normals = np.empty((n, 3))
    normals[ordered_starts] = first[order]
    r = first[order].copy()
    steps_total = int(running.max()) if len(running) else 0
    i = 0
    while i < steps_total:
        # Strands still running at step i: running is sorted longest first.
        live = int(np.searchsorted(-running, -i, side="left"))
        if live < _ARRAY_FRAMES:
            # A step as arrays costs the same for one strand as for a thousand. With only a
            # few long strands left -- a vine's trunks -- plain floats finish them faster.
            for j in range(live):
                _carry_frame(normals, tangents, steps, step_sq, int(ordered_starts[j]) + i,
                             int(running[j]) - i, r[j])
            break
        at = ordered_starts[:live] + i
        r = r[:live]
        v = steps[at]
        c1 = step_sq[at]
        t = tangents[at]
        t_next = tangents[at + 1]
        moving = c1 >= _EPSILON
        safe = np.where(moving, c1, 1.0)
        k = 2.0 * (v[:, 0] * r[:, 0] + v[:, 1] * r[:, 1] + v[:, 2] * r[:, 2]) / safe
        r_l = r - k[:, None] * v
        k = 2.0 * (v[:, 0] * t[:, 0] + v[:, 1] * t[:, 1] + v[:, 2] * t[:, 2]) / safe
        t_l = t - k[:, None] * v
        v2 = t_next - t_l
        c2 = v2[:, 0] * v2[:, 0] + v2[:, 1] * v2[:, 1] + v2[:, 2] * v2[:, 2]
        k = 2.0 * (v2[:, 0] * r_l[:, 0] + v2[:, 1] * r_l[:, 1] + v2[:, 2] * r_l[:, 2]) / np.where(
            c2 < _EPSILON, 1.0, c2)
        reflected = np.where((c2 < _EPSILON)[:, None], r_l, r_l - k[:, None] * v2)
        norm = np.sqrt(reflected[:, 0] * reflected[:, 0] + reflected[:, 1] * reflected[:, 1]
                       + reflected[:, 2] * reflected[:, 2])
        reflected = np.where((norm > _EPSILON)[:, None],
                             reflected / np.where(norm > _EPSILON, norm, 1.0)[:, None], reflected)
        # A repeated point: no direction to reflect through, so carry the frame across.
        r = np.where(moving[:, None], reflected, r)
        normals[at + 1] = r
        i += 1

    binormals = np.cross(tangents, normals)
    return tangents, normals, binormals


#: Below this many strands still running, `_frames` finishes them one at a time.
_ARRAY_FRAMES = 16


def _carry_frame(normals, tangents, steps, step_sq, at, count, frame):
    """Carry one strand's frame `count` steps on from sample `at`, writing `normals`.

    The per-sample double reflection on plain floats: the same arithmetic as the array step
    in `_frames`, for when only a few strands are left and numpy's per-call cost would be
    most of the work.
    """
    rx, ry, rz = (float(c) for c in frame)
    t_list = tangents[at:at + count + 1].tolist()
    v_list = steps[at:at + count].tolist()
    c_list = step_sq[at:at + count].tolist()
    tx, ty, tz = t_list[0]
    out = []
    for i in range(count):
        c1 = c_list[i]
        nx, ny, nz = t_list[i + 1]
        if c1 < _EPSILON:
            out.append((rx, ry, rz))
            tx, ty, tz = nx, ny, nz
            continue
        v1x, v1y, v1z = v_list[i]
        k = 2.0 * (v1x * rx + v1y * ry + v1z * rz) / c1
        r_lx, r_ly, r_lz = rx - k * v1x, ry - k * v1y, rz - k * v1z
        k = 2.0 * (v1x * tx + v1y * ty + v1z * tz) / c1
        t_lx, t_ly, t_lz = tx - k * v1x, ty - k * v1y, tz - k * v1z
        v2x, v2y, v2z = nx - t_lx, ny - t_ly, nz - t_lz
        c2 = v2x * v2x + v2y * v2y + v2z * v2z
        if c2 < _EPSILON:
            rx, ry, rz = r_lx, r_ly, r_lz
        else:
            k = 2.0 * (v2x * r_lx + v2y * r_ly + v2z * r_lz) / c2
            rx, ry, rz = r_lx - k * v2x, r_ly - k * v2y, r_lz - k * v2z
        norm = math.sqrt(rx * rx + ry * ry + rz * rz)
        if norm > _EPSILON:
            rx, ry, rz = rx / norm, ry / norm, rz / norm
        out.append((rx, ry, rz))
        tx, ty, tz = nx, ny, nz
    if out:
        normals[at + 1:at + 1 + count] = out


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def _along(points, radii, counts, strand, uv_mode, tiles_along, proportional_unit):
    """v along every strand: per strand, per scene unit, or in `proportional_unit`s of each.

    Args:
        proportional_unit: Per strand; how long one Proportional tile is.
    """
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    step = np.zeros(len(points))
    step[1:] = np.linalg.norm(np.diff(points, axis=0), axis=1)
    step[starts] = 0.0
    arc = np.zeros(len(points))
    for s, c in zip(starts.tolist(), np.asarray(counts).tolist()):
        arc[s:s + c] = np.cumsum(step[s:s + c])
    total = arc[starts + np.asarray(counts) - 1]
    if uv_mode == _UV_PER_STRAND:
        # A strand of no length has nothing to spread the texture along; v stays 0 on it.
        safe = np.where(total > _EPSILON, total, 1.0)
        return (arc / safe[strand]) * tiles_along
    if uv_mode == _UV_BY_LENGTH:
        return arc * tiles_along
    return (arc / np.maximum(proportional_unit, _EPSILON)[strand]) * tiles_along


def _mean_per_strand(values, counts):
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    return np.asarray([float(np.mean(values[s:s + c])) for s, c in zip(starts.tolist(),
                                                                       np.asarray(counts).tolist())])


def _quads(counts, cols: int, across: int):
    """Quads joining each sample's ring to the next one's within a strand.

    `across` is how many quads go around (sides for a tube, 1 for a ribbon); a ring has `cols`
    vertices. Rows run in sample order and quads around in order, as the per-strand loop
    wrote them.
    """
    strand = np.repeat(np.arange(len(counts)), counts)
    rows = np.flatnonzero(strand[1:] == strand[:-1])
    a = rows[:, None] * cols + np.arange(across)[None, :]
    return np.stack([a, a + cols, a + cols + 1, a + 1], axis=-1).reshape(-1, 4)


def build_mesh(strands, kind, basis, sides: int, per_segment: int,
               uv_mode: str, tiles_along: float, width_scale: float,
               tiles_around: float = 1.0, profile: str = _PROFILE_TUBE, up=(0.0, 0.0, 1.0),
               carry=None):
    """Sweep every strand and concatenate them into one mesh.

    Every strand is handled in the same array pass rather than one at a time: a vine is
    hundreds of short strands, and per strand the numpy call overhead was most of the cost.

    The seam ring of a tube is duplicated -- `sides + 1` vertices rather than `sides` -- so u
    can run a clean 0..1 around it. Sharing one vertex at the seam would force it to hold both
    0 and 1, and the texture would run backwards across that column.

    A ribbon is a flat strip lying across `up`, the curve's width wide, faces wound counter-
    clockwise seen from `up`. Where the curve runs straight up, up x tangent has no direction
    and the rotation-minimising normal stands in.

    Args:
        strands: (points, radii) per strand.
        carry: Optional per-CV values per strand, (count,) each, swept along like the radius:
            one value per output point (the growth primvar).

    Returns:
        (points, normals, st, face_counts, face_indices), or None when nothing swept. With
        `carry`, a sixth item: the carried values, one per point.
    """
    strands = [(np.asarray(p, dtype=np.float64), np.asarray(r, dtype=np.float64))
               for p, r in strands]
    use = [i for i, (p, _) in enumerate(strands) if len(p) >= 2]
    if not use:
        return None
    counts = np.asarray([len(strands[i][0]) for i in use], dtype=np.int64)
    points = np.concatenate([strands[i][0] for i in use])
    columns = [np.concatenate([strands[i][1] for i in use]) * width_scale]
    if carry is not None:
        columns.append(np.concatenate([np.asarray(carry[i], dtype=np.float64) for i in use]))
    line, values, counts = _centrelines(points, np.stack(columns, axis=1), counts, kind, basis,
                                        per_segment)
    line, values, counts = _drop_repeats(line, values, counts)
    if (counts < 2).any():
        keep_strand = counts >= 2
        keep = np.repeat(keep_strand, counts)
        line, values, counts = line[keep], values[keep], counts[keep_strand]
    if not len(counts):
        return None
    radii = values[:, 0]
    tangents, normals, binormals = _frames(line, counts)
    strand = np.repeat(np.arange(len(counts)), counts)

    if profile == _PROFILE_RIBBON:
        up = np.asarray(up, dtype=np.float64)
        side = np.cross(up, tangents)
        flat = np.linalg.norm(side, axis=1) < 1e-6
        side[flat] = normals[flat]
        side /= np.maximum(np.linalg.norm(side, axis=1), _EPSILON)[:, None]
        # Proportional: v counts widths, so one tile is square on the strip.
        v = _along(line, radii, counts, strand, uv_mode, tiles_along,
                   2.0 * _mean_per_strand(radii, counts))
        ring = np.stack([line + side * radii[:, None], line - side * radii[:, None]], axis=1)
        u = np.array([0.0, tiles_around])
        facing = np.broadcast_to(up, ring.shape)
        cols, across = 2, 1
        faces = _quads(counts, cols, across)
        faces = faces[:, [0, 3, 2, 1]]        # a, a+1, a+3, a+2: counter-clockwise from up
        radial = facing
    else:
        # Proportional: v counts circumferences, so one tile covers a square patch of surface
        # and a texture keeps its aspect whatever the strand's length or thickness.
        v = _along(line, radii, counts, strand, uv_mode, tiles_along,
                   2.0 * np.pi * np.maximum(_mean_per_strand(radii, counts), _EPSILON))
        angles = np.linspace(0.0, 2.0 * np.pi, sides + 1)
        cos, sin = np.cos(angles), np.sin(angles)
        radial = cos[None, :, None] * normals[:, None, :] + sin[None, :, None] * binormals[:, None, :]
        ring = line[:, None, :] + radial * radii[:, None, None]
        u = (angles / (2.0 * np.pi)) * tiles_around
        cols, across = sides + 1, sides
        faces = _quads(counts, cols, across)

    st = np.stack([
        np.broadcast_to(u[None, :], (len(line), cols)),
        np.broadcast_to(v[:, None], (len(line), cols)),
    ], axis=-1)
    built = (
        ring.reshape(-1, 3),
        np.asarray(radial).reshape(-1, 3),
        st.reshape(-1, 2),
        np.full(len(faces), 4, dtype=np.int32),
        faces.reshape(-1).astype(np.int32),
    )
    return built + (np.repeat(values[:, 1], cols),) if carry is not None else built


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def _author(stage, path: str, built, source_prim) -> None:
    points, _normals, st, counts, indices = built[:5]
    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(path))
    mesh.CreatePointsAttr().Set(
        Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(points, dtype=np.float32))
    )
    mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(counts))
    mesh.CreateFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(indices))
    # Polygons, not a subdivision cage: left to the default the tube is smoothed into a
    # thinner one, and the silhouette stops matching the width the curve was authored at.
    mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)

    # No `normals`, deliberately, and this is not an oversight to tidy up later.
    #
    # hdVRay drops a mesh's primvars when `normals` is authored alongside
    # subdivisionScheme=none: the texture silently stops appearing and the surface renders
    # its fallback colour, which is the exact symptom this chunk exists to cure. Measured by
    # rendering this mesh five ways and diffing against an untextured render -- st at vertex,
    # varying and faceVarying interpolation all fail with normals present, and both removing
    # the normals and letting the mesh subdivide fix it. So the choice is smooth shading or
    # textures, and textures are the point.
    #
    # Little is lost: a swept tube's true normals are its radial directions, and a renderer
    # recovers those from the geometry. `sides` is what governs how round it looks.

    primvars = UsdGeom.PrimvarsAPI(mesh)
    uv = primvars.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
    uv.Set(Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(st, dtype=np.float32)))

    # Carry the curve's colour, so the tube looks like what it replaced before any material is
    # bound -- otherwise turning this on appears to lose the vine's green.
    source_colour = UsdGeom.Gprim(source_prim).GetDisplayColorAttr().Get() if source_prim else None
    if source_colour:
        UsdGeom.Primvar(mesh.CreateDisplayColorAttr()).SetInterpolation(UsdGeom.Tokens.constant)
        mesh.GetDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*source_colour[0])]))

    lo = points.min(axis=0)
    hi = points.max(axis=0)
    mesh.CreateExtentAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*lo.astype(float)),
                                               Gf.Vec3f(*hi.astype(float))]))


def execute(node) -> None:
    stage = node.get_stage()

    is_f = chunk.is_param_t(float)
    is_i = chunk.is_param_t(int)
    is_s = chunk.is_param_t(str)
    is_b = chunk.is_param_t(bool)

    out_path = node.get_param("primpath_location").value
    if not out_path:
        return

    paths = node.get_param("curves_prim").resolve_paths()
    if not paths:
        node.get_param("readout").value = "pick a BasisCurves to sweep"
        return

    # Read from a composition that excludes this chunk's own layer: the mesh we author sits on
    # the same stage, and a cook that could see it would be reading its own output.
    upstream = chunk_stage.get_upstream_stage(stage, node.get_scene(), node)
    recurse = node.get_param("recurse", is_b).value
    sources = [
        curve
        for path in paths
        for curve in _curve_prims(upstream.GetPrimAtPath(path), recurse)
    ]
    if not sources:
        node.get_param("readout").value = "no BasisCurves under that prim"
        return

    sides = max(3, int(node.get_param("sides", is_i).value))
    per_segment = max(1, int(node.get_param("segments", is_i).value))
    uv_mode = node.get_param("uv_mode", is_s).value
    tiles_along = float(node.get_param("tiles_along", is_f).value) or 1.0
    tiles_around = float(node.get_param("tiles_around", is_f).value) or 1.0
    width_scale = float(node.get_param("width_scale", is_f).value)
    profile = node.get_param("profile", is_s).value
    up = (0.0, 1.0, 0.0) if UsdGeom.GetStageUpAxis(upstream).upper() == "Y" else (0.0, 0.0, 1.0)

    all_strands, kind, basis, first = [], UsdGeom.Tokens.linear, UsdGeom.Tokens.bspline, None
    for source in sources:
        strands, kind, basis = _read_strands(source)
        if strands:
            all_strands.extend(strands)
            first = first or source

    built = build_mesh(all_strands, kind, basis, sides, per_segment,
                       uv_mode, tiles_along, width_scale, tiles_around, profile, up)
    if built is None:
        node.get_param("readout").value = "those curves have no sweepable strands"
        return

    _author(stage, out_path, built, first)
    node.get_param("out_mesh").value = out_path
    node.get_param("readout").value = (
        f"{len(all_strands)} strands, {len(built[3])} faces"
    )


def main():
    chunk_api = chunk.Chunk("Curves to Mesh")

    chunk_api.add_param(
        chunk.LineEdit(
            "curves_prim",
            "Curves",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="The BasisCurves to sweep — or any prim above one. Wire this from a Vine\n"
            "chunk's Vine Curves output.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "primpath_location",
            "Prim Path",
            default_value="/CurvesMesh",
            flags=_PF.LABELED,
            tooltip="Where the swept Mesh is written. Bind your material here.",
        )
    )
    chunk_api.add_param(
        chunk.Label("readout", "nothing swept yet", default_value="nothing swept yet",
                    tooltip="What the last cook produced.")
    )

    shape = chunk.Group("shape_grp", "Shape", icon="curve", flags=_GF)
    shape.add_param(chunk.Menu(
        "profile", "Profile", menu_items=_PROFILES, default_value=_PROFILE_TUBE,
        flags=_PF.LABELED,
        tooltip="Tube: round, around the curve -- a vine, a cable.\n"
        "Ribbon: a flat strip lying on the ground, the curve's width wide -- a road, a\n"
        "river, a path. u runs across it, v along.",
    ))
    shape.add_param(chunk.Spinbox(
        "sides", "Sides", is_int=True, min_value=3, max_value=64, default_value=6,
        flags=_PF.LABELED,
        tooltip="Faces around the tube. Six is plenty for a vine seen from a metre away;\n"
        "raise it for a hero strand, lower it for a field of them.",
    ))
    shape.add_param(chunk.Spinbox(
        "segments", "Segments", is_int=True, min_value=1, max_value=16, default_value=4,
        flags=_PF.LABELED,
        tooltip="Samples per curve segment, for cubic curves only. A linear curve is already\n"
        "a polyline and is swept exactly as authored.",
    ))
    shape.add_param(chunk.Slider(
        "width_scale", "Width Scale", min_value=0.01, max_value=10.0, default_value=1.0,
        flags=_PF.LABELED,
        tooltip="Multiplies the curve's own widths. The taper it was authored with is kept.",
    ))
    shape.add_param(chunk.Checkbox(
        "recurse", "Include Children", default_value=True, flags=_PF.LABELED,
        tooltip="Sweep every BasisCurves under the picked prim, not only the prim itself.",
    ))
    chunk_api.add_param(shape)

    uv = chunk.Group("uv_grp", "UVs", icon="image", flags=_GF)
    uv.add_param(chunk.Menu(
        "uv_mode", "Layout", menu_items=_UV_MODES, default_value=_UV_PROPORTIONAL,
        flags=_PF.LABELED,
        tooltip="Proportional: one tile is square, whatever the strand's length or\n"
        "thickness. Start here — a checker looks like a checker, and Repeats Along is\n"
        "then a straightforward 'how often'.\n\n"
        "Per Strand: the whole texture is squeezed into each strand, end to end. Right\n"
        "for a gradient from root to tip, badly stretched for anything that tiles.\n\n"
        "By Length: repeats per scene unit. Matches texel density across strands, but\n"
        "the useful number depends on whether the scene is in metres or centimetres.",
    ))
    uv.add_param(chunk.Slider(
        "tiles_along", "Repeats Along", min_value=0.01, max_value=50.0, default_value=1.0,
        flags=_PF.LABELED,
        tooltip="How often the texture repeats from root to tip. In Proportional this\n"
        "counts tube circumferences, so 1.0 is what keeps the texture square.",
    ))
    uv.add_param(chunk.Slider(
        "tiles_around", "Repeats Around", min_value=0.01, max_value=20.0, default_value=1.0,
        flags=_PF.LABELED,
        tooltip="How often the texture wraps around the tube. One wrap is usually what you\n"
        "want; raise it for finer bark on a thick strand.",
    ))
    chunk_api.add_param(uv)

    chunk_api.add_param(
        chunk.LineEdit(
            "out_mesh", "Mesh", flags=_PF.OUTPUT | _PF.LABELED,
            tooltip="The swept Mesh, with st. Bind a material here or feed it downstream.",
        )
    )
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="curve",
        requirements=["numpy>=2,<3"],
        description="""
        Turns curves (vines, hair) into tube meshes with UVs, so they can take a real material.

        Curves to Mesh — sweeps a tube along a BasisCurves and gives it UVs,
        so it can carry a real material.

        A curve cannot be textured in most renderers. Rendering the same
        BasisCurves with and without a primvars:st and comparing the images:
        Storm honours curve UVs, V-Ray and Cycles return pixel-identical
        renders. A Mesh with st textures in all three.

        Wire a Vine chunk's Vine Curves output into Curves, bind your material
        to the Mesh output, and the vines take bark, moss or whatever else.
        Works on any BasisCurves — Add Hair's, or curves referenced in.

        Parameters:
          - Curves: the BasisCurves to sweep, or a prim above some.
          - Prim Path: where the Mesh is written.
          - Sides: faces around the tube.
          - Segments: samples per curve segment, cubic curves only.
          - Width Scale: multiplies the curve's authored widths.
          - Along Strand / Tiling: how v is laid out.

        Cubic curves are evaluated rather than swept along their control
        points, so the tube sits on the curve the renderer draws. The frame is
        rotation-minimising, so a texture does not spiral as a strand turns.
        """,
    )
