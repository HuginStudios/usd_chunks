"""Edge Field chunk -- marks the sharp edges or inner corners of meshes, for wear and dirt.

Wear shows on edges: paint rubs off a box's corners, concrete chips where two walls meet. Dirt
does the opposite and collects in the inside corners. This chunk paints either as a 0..1
primvar on the meshes, from how sharply the surface bends at each edge.

* Find: Outer Edges (bent outward, where wear goes), Inner Corners (bent inward, where dirt
  goes), or Both.
* Angle / Softness: an edge bent by Angle degrees or more counts fully; one bent by less
  fades out over Softness degrees below it. A flat face, or a smooth curve made of many small
  bends, doesn't count.
* Width: how far the field spreads from an edge across the faces, in scene units. 0 marks only
  the vertices on the edge.
* Multiply With: multiply in another field on the same meshes (a Noise Field's), so the wear
  breaks up into patches instead of an even band.
* Show Field: tint the meshes by the field, to see it in the viewport.

Wire the primvar into a Scatter Points' Density Field to drop chips and rubble along the
edges, or read it in a material (Primvar Reader) to wear the paint away there.

The field is per vertex, so it is only as fine as the mesh: on an eight-point cube every
vertex is on an edge. Give the mesh some divisions (or subdivide it) for a band to show.
Only edges inside one mesh count, not where two meshes touch. Faces that each have their own
corner points still count where they meet: points in the same place are welded first.

Requires: pxr (OpenUSD), usd_chunks, numpy, scipy.
"""

from __future__ import annotations

import logging

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt
from scipy.spatial import cKDTree

from usd_chunks import chunk, chunk_stage, chunk_utils, kd_query

logger = logging.getLogger(__name__)

#: Show Field's tint: blue, since usdview highlights the selection in yellow (as Noise Field).
_COLOUR = (0.15, 0.55, 1.0)

_OUTER, _INNER, _BOTH = "Outer Edges", "Inner Corners", "Both"

#: Most points laid along the edges for Width. A finer spacing than this would allow is
#: widened to fit, which only makes the band's edge a little less smooth.
_MAX_SAMPLES = 2_000_000


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def weld(points, indices):
    """Merge the points that sit in the same place: ``(points, indices, inverse)``.

    Many meshes give each face its own corners (hard edges, UV seams, a model built from
    separate quads), so no two faces share an edge by index even where they touch. Welded,
    they do. ``inverse`` maps each original point to its welded one.
    """
    points = np.asarray(points, dtype=np.float64)
    if not len(points):
        return points, np.asarray(indices, dtype=np.int64), np.zeros(0, dtype=np.int64)
    tolerance = max(float(np.ptp(points, axis=0).max()) * 1e-6, 1e-9)
    keys = np.round(points / tolerance).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    indices = np.asarray(indices, dtype=np.int64)
    if len(indices) and (indices.min() < 0 or indices.max() >= len(points)):
        return points, indices, np.arange(len(points))  # broken indices: sharp_edges skips the mesh
    return points[first], inverse[indices], inverse


def sharp_edges(points, counts, indices, *, left_handed=False):
    """The edges two faces share, with how sharply the surface bends there.

    Returns ``(a, b, angle, outward)``: the two point indices of each shared edge, the angle
    between its faces' normals in degrees (0 = flat), and whether it bends outward (a convex
    edge, like a box's) rather than inward (a corner where two walls meet). An edge only one
    face has, or three or more, is left out.
    """
    points = np.asarray(points, dtype=np.float64)
    counts = np.asarray(counts, dtype=np.int64)
    indices = np.asarray(indices, dtype=np.int64)
    empty = np.zeros(0, dtype=np.int64)
    if (not len(counts) or counts.sum() != len(indices) or (counts < 0).any()
            or (len(indices) and (indices.min() < 0 or indices.max() >= len(points)))):
        return empty, empty, np.zeros(0), np.zeros(0, dtype=bool)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    face = np.repeat(np.arange(len(counts)), counts)
    corner = np.arange(len(indices))
    # The next corner round each face, wrapping from its last corner back to its first.
    nxt = corner + 1
    last = starts + counts - 1
    real = counts > 0
    nxt[last[real]] = starts[real]
    a, b = indices, indices[nxt]
    # Newell's normal: the sum of each side's cross product, right for any planar polygon.
    p, q = points[a], points[b]
    normals = np.zeros((len(counts), 3))
    centres = np.zeros((len(counts), 3))
    normals[real] = np.add.reduceat(np.cross(p, q), starts[real])
    centres[real] = np.add.reduceat(p, starts[real]) / counts[real, None]
    length = np.linalg.norm(normals, axis=1)
    normals /= np.where(length > 0.0, length, 1.0)[:, None]
    if left_handed:
        normals = -normals
    # Group the face sides by the edge they lie on; keep the edges exactly two faces share.
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    keep = lo != hi
    lo, hi, side_face = lo[keep], hi[keep], face[keep]
    key = lo * len(points) + hi
    order = np.argsort(key, kind="stable")
    key, lo, hi, side_face = key[order], lo[order], hi[order], side_face[order]
    first = np.flatnonzero(np.concatenate([[True], key[1:] != key[:-1]]))
    size = np.diff(np.concatenate([first, [len(key)]]))
    pair = first[size == 2]
    f1, f2 = side_face[pair], side_face[pair + 1]
    edge_a, edge_b = lo[pair], hi[pair]
    n1, n2 = normals[f1], normals[f2]
    angle = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", n1, n2), -1.0, 1.0)))
    # Outward when the second face's centre is behind the first face's plane.
    outward = np.einsum("ij,ij->i", n1, centres[f2] - points[edge_a]) < 0.0
    return edge_a, edge_b, angle, outward


def edge_strength(angle, outward, *, find, min_angle, softness):
    """How much each edge counts, 0..1: fully from *min_angle* degrees up, fading out over
    *softness* degrees below it, and 0 for the edges *find* doesn't ask for."""
    if softness <= 0.0:
        strength = (angle >= min_angle).astype(np.float64)
    else:
        strength = _smoothstep((angle - (min_angle - softness)) / softness)
    strength[angle < 1e-3] = 0.0  # flat stays flat, even with Angle at 0
    if find == _OUTER:
        strength[~outward] = 0.0
    elif find == _INNER:
        strength[outward] = 0.0
    return strength


def vertex_field(world, edge_a, edge_b, strength, width):
    """The field at each point: each edge's strength, fading to 0 *width* away from it."""
    values = np.zeros(len(world))
    on = strength > 0.0
    edge_a, edge_b, strength = edge_a[on], edge_b[on], strength[on]
    if not len(strength):
        return values
    np.maximum.at(values, edge_a, strength)
    np.maximum.at(values, edge_b, strength)
    if width <= 0.0:
        return values
    # Lay points along the edges a third of Width apart, and give each vertex the strongest
    # edge within reach, faded by its distance.
    a, b = world[edge_a], world[edge_b]
    length = np.linalg.norm(b - a, axis=1)
    step = width / 3.0
    if (np.ceil(length / step) + 1).sum() > _MAX_SAMPLES:
        step = length.sum() / max(_MAX_SAMPLES - 2 * len(length), len(length))
    steps = np.maximum(1, np.ceil(length / step)).astype(np.int64)
    edge = np.repeat(np.arange(len(steps)), steps + 1)
    first = np.concatenate([[0], np.cumsum(steps + 1)[:-1]])
    t = (np.arange(len(edge)) - first[edge]) / steps[edge]
    samples = a[edge] + (b[edge] - a[edge]) * t[:, None]
    k = min(8, len(samples))
    dist, nearest = kd_query.query(cKDTree(samples), world, k=k, distance_upper_bound=width)
    dist, nearest = dist.reshape(len(world), k), nearest.reshape(len(world), k)
    found = nearest < len(samples)
    reach = np.where(found, strength[edge[np.where(found, nearest, 0)]], 0.0)
    fade = 1.0 - _smoothstep(np.where(found, dist, width) / width)
    return np.maximum(values, (reach * fade).max(axis=1))


def _base_values(prim, name, count):
    """The *name* primvar on *prim*, one value per vertex, or None (with a warning)."""
    if not name:
        return None
    pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar(name)
    if not pv or not pv.HasValue():
        logger.warning("%s has no primvars:%s to multiply with", prim.GetPath(), name)
        return None
    values = np.asarray(pv.ComputeFlattened(), dtype=np.float64).reshape(-1)
    if pv.GetInterpolation() == UsdGeom.Tokens.constant or len(values) == 1:
        return np.full(count, values[0])
    if pv.GetInterpolation() not in (UsdGeom.Tokens.vertex, UsdGeom.Tokens.varying) or len(values) != count:
        logger.warning("%s: primvars:%s is %s with %d values, not one per point (%d); not multiplied in",
                       prim.GetPath(), name, pv.GetInterpolation(), len(values), count)
        return None
    return values


def _write_tint(out, mesh, values, colour):
    """Show the field: the mesh's own colour moved toward *colour* by the field, with the
    material set aside, since displayColor loses to any shader. The layer is rebuilt on every
    cook, so unticking Show Field puts the look back."""
    base = mesh.GetDisplayColorAttr().Get()
    under = np.asarray(base, dtype=np.float64).reshape(-1, 3) if base else np.full((1, 3), 0.5)
    if len(under) != len(values):
        under = np.repeat(under[-1:], len(values), axis=0)
    tint = under + (colour - under) * np.clip(values, 0.0, 1.0)[:, None]
    display = UsdGeom.Gprim(out.GetPrim()).CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex)
    display.Set(Vt.Vec3fArray.FromNumpy(tint.astype(np.float32)))
    out.GetPrim().CreateRelationship("material:binding", False).SetTargets([])


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    is_f, is_s = chunk.is_param_t(float), chunk.is_param_t(str)
    paths = [p.strip() for p in node.get_param("prim_path", is_s).value.split(",") if p.strip()]
    name = node.get_param("primvar", is_s).value.strip() or "edges"
    base_name = node.get_param("base_primvar", is_s).value.strip()
    find = node.get_param("find", is_s).value or _OUTER
    min_angle = max(0.0, node.get_param("angle", is_f).value)
    softness = max(0.0, node.get_param("softness", is_f).value)
    width = max(0.0, node.get_param("width", is_f).value)
    show = node.get_param("show_field", chunk.is_param_t(bool)).value
    colour = np.asarray(node.get_param("field_color").value or _COLOUR, dtype=np.float64) if show else None
    if not paths:
        _report(node, "set the meshes to paint")
        return
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    painted, total, lit, edges = 0, 0, 0.0, 0
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        for path in paths:
            root = upstream.GetPrimAtPath(path)
            if not root:
                continue
            for prim in Usd.PrimRange(root):
                mesh = UsdGeom.Mesh(prim)
                if not mesh:
                    continue
                pts = mesh.GetPointsAttr().Get()
                if not pts:
                    continue
                m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
                world = np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3]
                left = mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded
                # A mirroring transform turns the faces inside out, as left-handed winding does.
                left ^= bool(np.linalg.det(m[:3, :3]) < 0.0)
                welded, indices, inverse = weld(world, mesh.GetFaceVertexIndicesAttr().Get() or [])
                edge_a, edge_b, angle, outward = sharp_edges(
                    welded, mesh.GetFaceVertexCountsAttr().Get() or [], indices, left_handed=left)
                strength = edge_strength(angle, outward, find=find, min_angle=min_angle, softness=softness)
                values = vertex_field(welded, edge_a, edge_b, strength, width)[inverse]
                base = _base_values(prim, base_name, len(values))
                if base is not None:
                    values = values * np.clip(base, 0.0, 1.0)
                out = UsdGeom.PrimvarsAPI(stage.OverridePrim(prim.GetPath()))
                pv = out.CreatePrimvar(name, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
                pv.Set(Vt.FloatArray.FromNumpy(values.astype(np.float32)))
                if colour is not None:
                    _write_tint(out, mesh, values, colour)
                painted += 1
                total += len(values)
                lit += float(values.sum())
                edges += int((strength > 0.0).sum())
    if not painted:
        _report(node, "no meshes found to paint")
        return
    _report(node, f"primvars:{name} on {painted} mesh{'es' if painted != 1 else ''} · "
                  f"{edges} edge{'s' if edges != 1 else ''} · {100.0 * lit / max(total, 1):.0f}% covered")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Edge Field")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Meshes", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The meshes to paint, or prims with meshes under them. Comma-separate several.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primvar", "Primvar", default_value="edges", flags=labeled,
        tooltip="Name of the primvar written. Put it in a Scatter Points' Density Field, or read it in a "
        "material.",
    ))
    chunk_api.add_param(chunk.Menu(
        "find", "Find", menu_items=[_OUTER, _INNER, _BOTH], default_value=_OUTER,
        tooltip="Outer Edges: edges that bend outward, where paint wears off and corners chip.\n"
        "Inner Corners: where two faces meet inward, where dirt collects.\nBoth: either.",
    ))
    chunk_api.add_param(chunk.Slider("angle", "Angle", min_value=0.0, max_value=180.0, default_value=40.0,
                                     tooltip="An edge bent this many degrees or more counts fully. "
                                     "A box's edges are 90."))
    chunk_api.add_param(chunk.Slider("softness", "Softness", min_value=0.0, max_value=90.0, default_value=20.0,
                                     tooltip="How many degrees below Angle an edge fades out over. "
                                     "0 = an edge counts fully or not at all."))
    chunk_api.add_param(chunk.Slider("width", "Width", min_value=0.0, max_value=1.0, default_value=0.1,
                                     tooltip="How far the field spreads from an edge across the faces, in "
                                     "scene units. 0 = only the vertices on the edge."))
    chunk_api.add_param(chunk.LineEdit(
        "base_primvar", "Multiply With", flags=labeled,
        tooltip="Another primvar on the same meshes (a Noise Field's) to multiply in, to break the wear "
        "into patches. Empty: edges only.",
    ))
    display = chunk.Group("display_grp", "Display", icon="eye")
    display.add_param(chunk.ColorPicker("field_color", "Field Color", default_value=_COLOUR,
                                        tooltip="The colour Show Field tints the edges."))
    display.add_param(chunk.Checkbox(
        "show_field", "Show Field", default_value=False, flags=labeled,
        tooltip="Tint the meshes by the field to see the edges. It hides their material, so turn it off "
        "for a render.",
    ))
    chunk_api.add_param(display)
    chunk_api.add_param(chunk.Label("readout", "nothing painted yet", default_value="nothing painted yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        # Width as a handle on the meshes: pick one in the viewport and drag up to widen the
        # band. Each Edge Field gets its own, named after it (a style's Dirt, Chips, ...).
        gizmos=[chunk.Gizmo("width", type="cube", axis="Y", driver="width")],
        category=chunk.StandardCategory.MODELLING,
        icon="ruler",
        description="""
        Marks sharp edges or inner corners on meshes, for edge wear, chips and dirt.

        Paints a 0..1 primvar from how sharply each edge bends: Outer Edges for wear and
        chips, Inner Corners for dirt, or Both. Angle and Softness set how sharp an edge must
        be, Width how far the field spreads across the faces (a handle in the viewport too).
        Put it in a Scatter Points' Density Field to scatter rubble along the edges, or read
        it in a material to wear the paint there. Multiply With another field breaks it into
        patches. Show Field tints the meshes so you can see it.
        """,
        requirements=["numpy>=2,<3", "scipy>=1.11,<2"],
    )
