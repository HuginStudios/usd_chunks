"""Facing Field chunk -- marks where meshes face up, sideways or down, or are under cover.

Things settle by which way a surface faces. Moss and grass take hold on tops: ledges, sills,
the top of a wall. Ivy and posters go on walls. Cobwebs, nests and drips hang where something
covers them from above: under the eaves, under a balcony. This chunk paints any of those as a
0..1 primvar on the meshes:

* Find: Tops (facing up), Walls (facing sideways), Undersides (facing down), or Sheltered
  (with something above it: the meshes themselves, and the Cover prims).
* Angle / Softness: a face within Angle degrees of the direction counts fully; one further off
  fades out over Softness degrees more. Sheltered uses Reach instead: the field is strongest
  just under the cover and fades out Reach below it.
* Multiply With: multiply in another field on the same meshes (a Noise Field's), so it
  breaks into patches.
* Show Field: tint the meshes by the field, to see it in the viewport.

Wire the primvar into a Scatter Points' Density Field to place things there. Up is the
stage's up axis. The field is per vertex, so it is only as fine as the mesh's points.

Requires: pxr (OpenUSD), usd_chunks, numpy.
"""

from __future__ import annotations

import logging

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_stage, chunk_utils

logger = logging.getLogger(__name__)

_TOPS, _WALLS, _UNDER, _SHELTERED = "Tops", "Walls", "Undersides", "Sheltered"
_COLOUR = (0.15, 0.55, 1.0)
#: Most points laid on the faces to find what covers what, across all the meshes.
_MAX_SAMPLES = 400_000
#: The cover map's cells along its longer side.
_CELLS = 256


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def triangles(counts, indices):
    """The faces as a fan of triangles: an (N, 3) array of point indices."""
    counts = np.asarray(counts, dtype=np.int64)
    indices = np.asarray(indices, dtype=np.int64)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]]) if len(counts) else np.zeros(0, dtype=np.int64)
    tris = [np.stack([indices[starts[counts >= k]], indices[starts[counts >= k] + k - 2],
                      indices[starts[counts >= k] + k - 1]], axis=1)
            for k in range(3, int(counts.max(initial=0)) + 1)]
    return np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)


def vertex_normals(world, tris, left_handed=False):
    """Each point's normal, the area-weighted mean of the triangles round it."""
    a, b, c = world[tris[:, 0]], world[tris[:, 1]], world[tris[:, 2]]
    face = np.cross(b - a, c - a)
    if left_handed:
        face = -face
    normals = np.zeros_like(world)
    for k in range(3):
        np.add.at(normals, tris[:, k], face)
    length = np.linalg.norm(normals, axis=1)
    return normals / np.where(length > 0.0, length, 1.0)[:, None]


def facing(normals, up, find, angle, softness):
    """How much each normal faces the way find asks, 0..1."""
    cos_up = normals[:, up]
    off = {
        _TOPS: np.degrees(np.arccos(np.clip(cos_up, -1.0, 1.0))),
        _UNDER: np.degrees(np.arccos(np.clip(-cos_up, -1.0, 1.0))),
        _WALLS: np.abs(90.0 - np.degrees(np.arccos(np.clip(cos_up, -1.0, 1.0)))),
    }[find]
    if softness <= 0.0:
        return (off <= angle).astype(np.float64)
    return 1.0 - _smoothstep((off - angle) / softness)


def _samples(world, tris, budget):
    """Points spread over the triangles, about evenly by area, within budget."""
    a, b, c = world[tris[:, 0]], world[tris[:, 1]], world[tris[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    total = float(area.sum())
    if total <= 0.0:
        return world
    each = np.minimum(np.ceil(area / total * budget), budget).astype(np.int64)
    tri = np.repeat(np.arange(len(tris)), each)
    rng = np.random.default_rng(0)
    u, v = rng.random(len(tri)), rng.random(len(tri))
    flip = u + v > 1.0
    u[flip], v[flip] = 1.0 - u[flip], 1.0 - v[flip]
    pts = a[tri] + (b[tri] - a[tri]) * u[:, None] + (c[tri] - a[tri]) * v[:, None]
    return np.concatenate([world, pts])


def sheltered(world_list, cover_pts, up, reach):
    """For each mesh's points, how covered they are from above, 0..1: 1 just under a surface,
    fading out reach below it, 0 with nothing above."""
    flat = [i for i in range(3) if i != up]
    lo = cover_pts[:, flat].min(axis=0)
    span = max(float((cover_pts[:, flat].max(axis=0) - lo).max()), 1e-9)
    cell = span / _CELLS
    shape = np.floor((cover_pts[:, flat].max(axis=0) - lo) / cell).astype(np.int64) + 1
    # The lowest surface above each point is what shelters it.
    ij = np.clip(np.floor((cover_pts[:, flat] - lo) / cell).astype(np.int64), 0, shape - 1)
    key = ij[:, 0] * shape[1] + ij[:, 1]
    # One sorted number per sample: its cell, plus its height scaled into [0, 1). A search for
    # (a point's cell + its height) then lands on the first sample above it in its cell.
    h_lo = float(cover_pts[:, up].min())
    h_span = max(float(np.ptp(cover_pts[:, up])), 1e-9) * 1.0001
    ranked = np.sort(key + (cover_pts[:, up] - h_lo) / h_span)
    height = (ranked - np.floor(ranked)) * h_span + h_lo
    out = []
    margin = cell * 1.5  # a point's own face, and its neighbours in the same cell, are not cover
    for world in world_list:
        p = np.clip(np.floor((world[:, flat] - lo) / cell).astype(np.int64), 0, shape - 1)
        k = p[:, 0] * shape[1] + p[:, 1]
        over = np.clip((world[:, up] + margin - h_lo) / h_span, 0.0, 1.0 - 1e-12)
        raw = np.searchsorted(ranked, k + over, side="left")
        at = np.minimum(raw, len(ranked) - 1)
        found = (raw < len(ranked)) & (np.floor(ranked[at]).astype(np.int64) == k)
        gap = np.where(found, height[at] - world[:, up], np.inf)
        out.append(np.where(found, 1.0 - _smoothstep(gap / max(reach, 1e-9)), 0.0))
    return out


def _base_values(prim, name, count):
    if not name:
        return None
    pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar(name)
    if not pv or not pv.HasValue():
        return None
    values = np.asarray(pv.ComputeFlattened(), dtype=np.float64).reshape(-1)
    if len(values) == 1:
        return np.full(count, values[0])
    return values if len(values) == count else None


def _meshes(stage, paths, cache):
    """[(prim, mesh, world points, triangles, left handed)] for the meshes under paths."""
    found = []
    for path in paths:
        root = stage.GetPrimAtPath(path)
        if not root:
            continue
        for prim in Usd.PrimRange(root):
            mesh = UsdGeom.Mesh(prim)
            if not mesh or not (pts := mesh.GetPointsAttr().Get()):
                continue
            if UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            m = np.array(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
            world = np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3]
            indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64)
            if len(indices) and (indices.min() < 0 or indices.max() >= len(world)):
                continue
            left = (mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded) ^ bool(np.linalg.det(m[:3, :3]) < 0)
            found.append((prim, mesh, world, triangles(mesh.GetFaceVertexCountsAttr().Get() or [], indices), left))
    return found


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    is_f, is_s = chunk.is_param_t(float), chunk.is_param_t(str)
    split = chunk_utils.resolve_prim_paths
    name = node.get_param("primvar", is_s).value.strip() or "facing"
    find = node.get_param("find", is_s).value or _TOPS
    angle = max(0.0, node.get_param("angle", is_f).value)
    softness = max(0.0, node.get_param("softness", is_f).value)
    reach = max(0.0, node.get_param("reach", is_f).value)
    base_name = node.get_param("base_primvar", is_s).value.strip()
    show = node.get_param("show_field", chunk.is_param_t(bool)).value
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    paths = split(upstream, node.get_param("prim_path", is_s).value or "")
    if not paths:
        _report(node, "set the meshes to paint")
        return
    up = 2 if UsdGeom.GetStageUpAxis(upstream) == UsdGeom.Tokens.z else 1
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    meshes = _meshes(upstream, paths, cache)
    if not meshes:
        _report(node, "no meshes found to paint")
        return
    if find == _SHELTERED:
        cover = meshes + _meshes(upstream, split(upstream, node.get_param("cover", is_s).value or ""), cache)
        budget = max(_MAX_SAMPLES // max(len(cover), 1), 64)
        cover_pts = np.concatenate([_samples(w, t, budget) for _, _, w, t, _ in cover])
        fields = sheltered([w for _, _, w, _, _ in meshes], cover_pts, up, reach)
    else:
        fields = [facing(vertex_normals(w, t, left), up, find, angle, softness) for _, _, w, t, left in meshes]
    total, lit = 0, 0.0
    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        for (prim, mesh, _, _, _), values in zip(meshes, fields):
            base = _base_values(prim, base_name, len(values))
            if base is not None:
                values = values * np.clip(base, 0.0, 1.0)
            out = UsdGeom.PrimvarsAPI(stage.OverridePrim(prim.GetPath()))
            out.CreatePrimvar(name, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex).Set(
                Vt.FloatArray.FromNumpy(values.astype(np.float32)))
            if show:
                colour = np.asarray(node.get_param("field_color").value or _COLOUR, dtype=np.float64)
                tint = 0.5 + (colour - 0.5) * np.clip(values, 0.0, 1.0)[:, None]
                UsdGeom.Gprim(out.GetPrim()).CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex).Set(
                    Vt.Vec3fArray.FromNumpy(tint.astype(np.float32)))
                out.GetPrim().CreateRelationship("material:binding", False).SetTargets([])
            total += len(values)
            lit += float(values.sum())
    _report(node, f"primvars:{name} on {len(meshes)} mesh{'es' if len(meshes) != 1 else ''} · {find.lower()} · "
                  f"{100.0 * lit / max(total, 1):.0f}% covered")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Facing Field")
    flags = chunk.ChunkFlags.ParamFlags
    labeled = flags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Meshes", flags=labeled | flags.PRIM_SELECTION,
        tooltip="The meshes to paint, or prims with meshes under them.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primvar", "Primvar", default_value="facing", flags=labeled,
        tooltip="Name of the primvar written. Put it in a Scatter Points' Density Field.",
    ))
    chunk_api.add_param(chunk.Menu(
        "find", "Find", menu_items=[_TOPS, _WALLS, _UNDER, _SHELTERED], default_value=_TOPS, flags=labeled,
        tooltip="Tops: faces looking up (ledges, sills, roofs), where moss and grass take hold.\n"
        "Walls: faces looking sideways.\nUndersides: faces looking down.\n"
        "Sheltered: points with something above them, under the eaves or a balcony.",
    ))
    chunk_api.add_param(chunk.Slider(
        "angle", "Angle", min_value=0.0, max_value=90.0, default_value=30.0, flags=labeled,
        tooltip="A face within this many degrees of the direction counts fully.",
    ))
    chunk_api.add_param(chunk.Slider(
        "softness", "Softness", min_value=0.0, max_value=90.0, default_value=15.0, flags=labeled,
        tooltip="How many degrees further off a face fades out over. 0: it counts fully or not at all.",
    ))
    chunk_api.add_param(chunk.Slider(
        "reach", "Reach", min_value=0.0, max_value=10.0, default_value=1.0, flags=labeled,
        tooltip="Sheltered: how far below the cover the field reaches, in scene units.",
    ))
    # PRIM_PATHS: a roof or a tree over the object is outside it, so not put under it in a style.
    chunk_api.add_param(chunk.LineEdit(
        "cover", "Cover", flags=labeled | flags.PRIM_PATHS,
        tooltip="Sheltered: more prims that cover the meshes from above, besides the meshes themselves.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "base_primvar", "Multiply With", flags=labeled,
        tooltip="Another primvar on the same meshes (a Noise Field's) to multiply in, for patches.",
    ))
    display = chunk.Group("display_grp", "Display", icon="eye")
    display.add_param(chunk.ColorPicker("field_color", "Field Color", default_value=_COLOUR))
    display.add_param(chunk.Checkbox(
        "show_field", "Show Field", default_value=False, flags=labeled,
        tooltip="Tint the meshes by the field. It hides their material, so turn it off for a render.",
    ))
    chunk_api.add_param(display)
    chunk_api.add_param(chunk.Label("readout", "nothing painted yet", default_value="nothing painted yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="sun",
        description="""
        Marks where meshes face up, sideways or down, or are under cover, for placing things.

        Paints a 0..1 primvar: Tops (ledges, sills, roofs: moss, grass), Walls (ivy,
        posters), Undersides, or Sheltered (with something above: under the eaves or a
        balcony, for cobwebs and nests), faded by Angle and Softness, or by Reach below the
        cover. Put it in a Scatter Points' Density Field. Multiply With a Noise Field's
        primvar breaks it into patches; Show Field tints the meshes so you can see it.
        """,
        requirements=["numpy>=2,<3"],
    )
