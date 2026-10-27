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


def _centreline(points: np.ndarray, radii: np.ndarray, kind, basis, per_segment: int):
    """The polyline a tube is swept along, with a radius per sample.

    Linear curves are their own control points. Cubic ones are evaluated, because a B-spline
    does not pass through its CVs -- sweeping the CVs directly leaves the mesh beside the curve
    the renderer draws, worst at the ends, which is where Vine pins its roots.

    Radii ride the same basis as the positions, so the taper the pipe model produced survives
    into the tube instead of being re-sampled by eye.
    """
    if kind != UsdGeom.Tokens.cubic or len(points) < 4:
        return points, radii

    weights_of = _catmullrom_weights if basis == UsdGeom.Tokens.catmullRom else _bspline_weights
    # vstep is 1 for both bases handled here; bezier (vstep 3) is not, and falls back to its
    # control points above rather than being evaluated wrongly.
    segments = len(points) - 3
    t = np.linspace(0.0, 1.0, per_segment + 1)[:-1]
    weights = weights_of(t)

    out_p, out_r = [], []
    for s in range(segments):
        hull_p = points[s:s + 4]
        hull_r = radii[s:s + 4]
        out_p.append(weights @ hull_p)
        out_r.append(weights @ hull_r)
    # The closing sample of the last segment, which the half-open ranges above leave off.
    last = weights_of(np.asarray([1.0]))
    out_p.append(last @ points[segments - 1:segments + 3])
    out_r.append(last @ radii[segments - 1:segments + 3])
    return np.concatenate(out_p), np.concatenate(out_r)


def _drop_duplicates(points: np.ndarray, radii: np.ndarray):
    """Remove consecutive identical samples, which have no direction to frame."""
    if len(points) < 2:
        return points, radii
    step = np.linalg.norm(np.diff(points, axis=0), axis=1)
    keep = np.concatenate(([True], step > _EPSILON))
    return points[keep], radii[keep]


def _frames(points: np.ndarray):
    """A rotation-minimising frame per sample: (tangents, normals, binormals).

    Wang et al., *Computation of Rotation Minimizing Frames* (2008), double reflection. Each
    frame is carried from the previous one by reflecting twice, so the cross-section never
    spins about the curve. Re-deriving a frame per sample from a fixed up-vector is the usual
    shortcut and it twists -- on a textured tube that reads as a barber pole.

    The recurrence is sequential by nature, so the loop cannot go away; what can is numpy's
    per-call overhead inside it. Profiled at 83% of a 40-strand sweep, almost all of it in
    `np.linalg.norm` on three-element vectors -- so everything hoistable is computed as arrays
    up front and the loop itself runs on plain floats. Same arithmetic, same answer to the
    last bit, about seven times faster.
    """
    n = len(points)
    tangents = np.zeros((n, 3))
    tangents[:-1] = np.diff(points, axis=0)
    tangents[-1] = tangents[-2]
    lengths = np.linalg.norm(tangents, axis=1, keepdims=True)
    tangents = tangents / np.where(lengths > _EPSILON, lengths, 1.0)

    # Any vector not parallel to the first tangent seeds the frame; which one is arbitrary,
    # it only fixes where the UV seam falls.
    seed = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(seed, tangents[0]))) > 0.9:
        seed = np.array([1.0, 0.0, 0.0])
    first = np.cross(tangents[0], seed)
    first /= max(float(np.linalg.norm(first)), _EPSILON)

    # Hoisted out of the loop: the step vectors and their squared lengths.
    steps = np.diff(points, axis=0)
    step_sq = np.einsum("ij,ij->i", steps, steps)

    normals = np.empty((n, 3))
    normals[0] = first
    rx, ry, rz = float(first[0]), float(first[1]), float(first[2])
    tx, ty, tz = float(tangents[0][0]), float(tangents[0][1]), float(tangents[0][2])
    t_list = tangents.tolist()
    v_list = steps.tolist()
    c_list = step_sq.tolist()

    for i in range(n - 1):
        c1 = c_list[i]
        nx, ny, nz = t_list[i + 1]
        if c1 < _EPSILON:
            # A repeated point: no direction to reflect through, so carry the frame across.
            normals[i + 1] = (rx, ry, rz)
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
        normals[i + 1] = (rx, ry, rz)
        tx, ty, tz = nx, ny, nz

    binormals = np.cross(tangents, normals)
    return tangents, normals, binormals


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def _sweep(points, radii, sides: int, uv_mode: str, tiles_along: float,
           tiles_around: float):
    """One strand's ring vertices, normals, st and faces.

    The seam ring is duplicated -- `sides + 1` vertices rather than `sides` -- so u can run a
    clean 0..1 around the tube. Sharing one vertex at the seam would force it to hold both 0
    and 1, and the texture would run backwards across that column.
    """
    points, radii = _drop_duplicates(points, radii)
    if len(points) < 2:
        return None

    _, normals, binormals = _frames(points)

    step = np.linalg.norm(np.diff(points, axis=0), axis=1)
    arc = np.concatenate(([0.0], np.cumsum(step)))
    total = float(arc[-1])
    if uv_mode == _UV_PER_STRAND and total > _EPSILON:
        v = (arc / total) * tiles_along
    elif uv_mode == _UV_BY_LENGTH:
        v = arc * tiles_along
    else:
        # Proportional: v counts circumferences, so one tile covers a square patch of
        # surface and a texture keeps its aspect whatever the strand's length or thickness.
        circumference = 2.0 * np.pi * max(float(np.mean(radii)), _EPSILON)
        v = (arc / circumference) * tiles_along

    angles = np.linspace(0.0, 2.0 * np.pi, sides + 1)
    cos, sin = np.cos(angles), np.sin(angles)

    # (samples, sides+1, 3): each ring is the frame's normal/binormal circle at that radius.
    radial = cos[None, :, None] * normals[:, None, :] + sin[None, :, None] * binormals[:, None, :]
    ring = points[:, None, :] + radial * radii[:, None, None]

    u = (angles / (2.0 * np.pi)) * tiles_around
    st = np.stack([
        np.broadcast_to(u[None, :], (len(points), sides + 1)),
        np.broadcast_to(v[:, None], (len(points), sides + 1)),
    ], axis=-1)

    rows, cols = len(points), sides + 1
    i = np.arange(rows - 1)[:, None]
    j = np.arange(sides)[None, :]
    a = i * cols + j
    faces = np.stack([a, a + cols, a + cols + 1, a + 1], axis=-1).reshape(-1, 4)

    return (ring.reshape(-1, 3), radial.reshape(-1, 3), st.reshape(-1, 2), faces)


def _along(points, radii, uv_mode, tiles_along, proportional_unit):
    """v along a strand: per strand, per scene unit, or in `proportional_unit`s."""
    step = np.linalg.norm(np.diff(points, axis=0), axis=1)
    arc = np.concatenate(([0.0], np.cumsum(step)))
    total = float(arc[-1])
    if uv_mode == _UV_PER_STRAND and total > _EPSILON:
        return (arc / total) * tiles_along
    if uv_mode == _UV_BY_LENGTH:
        return arc * tiles_along
    return (arc / max(proportional_unit, _EPSILON)) * tiles_along


def _ribbon(points, radii, uv_mode: str, tiles_along: float, tiles_around: float, up):
    """One strand as a flat strip lying across `up`, the curve's width wide.

    Faces wind counter-clockwise seen from `up`, so the strip faces the sky. Where the curve
    runs straight up, up x tangent has no direction; the rotation-minimising normal stands in.
    """
    points, radii = _drop_duplicates(points, radii)
    if len(points) < 2:
        return None
    tangents, normals, _ = _frames(points)
    up = np.asarray(up, dtype=np.float64)
    side = np.cross(up, tangents)
    length = np.linalg.norm(side, axis=1)
    flat = length < 1e-6
    side[flat] = normals[flat]
    side /= np.maximum(np.linalg.norm(side, axis=1), _EPSILON)[:, None]

    # Proportional: v counts widths, so one tile is square on the strip.
    v = _along(points, radii, uv_mode, tiles_along, 2.0 * float(np.mean(radii)))
    left = points + side * radii[:, None]
    right = points - side * radii[:, None]
    ring = np.stack([left, right], axis=1)                  # (samples, 2, 3)
    st = np.stack([
        np.broadcast_to(np.array([0.0, tiles_around])[None, :], (len(points), 2)),
        np.broadcast_to(v[:, None], (len(points), 2)),
    ], axis=-1)
    a = np.arange(len(points) - 1) * 2
    faces = np.stack([a, a + 1, a + 3, a + 2], axis=-1)
    facing = np.broadcast_to(up, ring.shape)
    return (ring.reshape(-1, 3), facing.reshape(-1, 3), st.reshape(-1, 2), faces)


def build_mesh(strands, kind, basis, sides: int, per_segment: int,
               uv_mode: str, tiles_along: float, width_scale: float,
               tiles_around: float = 1.0, profile: str = _PROFILE_TUBE, up=(0.0, 0.0, 1.0)):
    """Sweep every strand and concatenate them into one mesh.

    Returns (points, normals, st, face_counts, face_indices), or None when nothing swept.
    """
    points_out, normals_out, st_out, faces_out = [], [], [], []
    offset = 0
    for raw_points, raw_radii in strands:
        if len(raw_points) < 2:
            continue
        line, radii = _centreline(raw_points, raw_radii * width_scale, kind, basis, per_segment)
        if profile == _PROFILE_RIBBON:
            swept = _ribbon(line, radii, uv_mode, tiles_along, tiles_around, up)
        else:
            swept = _sweep(line, radii, sides, uv_mode, tiles_along, tiles_around)
        if swept is None:
            continue
        ring, radial, st, faces = swept
        points_out.append(ring)
        normals_out.append(radial)
        st_out.append(st)
        faces_out.append(faces + offset)
        offset += len(ring)

    if not points_out:
        return None
    points = np.concatenate(points_out)
    indices = np.concatenate(faces_out).reshape(-1)
    return (
        points,
        np.concatenate(normals_out),
        np.concatenate(st_out),
        np.full(len(indices) // 4, 4, dtype=np.int32),
        indices.astype(np.int32),
    )


# ---------------------------------------------------------------------------
# authoring
# ---------------------------------------------------------------------------

def _author(stage, path: str, built, source_prim) -> None:
    points, _normals, st, counts, indices = built
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
