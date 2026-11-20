"""Around Field chunk -- the ground around an object's base, marked by how near it is, for debris.

Rubble piles up at the foot of a ruin, bushes crowd a cottage's walls, leaves blow into the base
of a crate. This chunk lays a flat ground patch at the object's base, reaching Reach past its
footprint, and paints it with a 0..1 primvar: 1 at the object, fading out over the last
Softness of Reach, broken into patches by Breakup. Wire the primvar into a Scatter Points'
Density Field, with the patch as its Surface Mesh, and the scatter lands around the object.

* The footprint is the object's lower part seen from above: what is within Reach of its
  lowest point. A ruin's open rooms are inside the footprint too, so they fill as well.
* The patch is hidden unless Show Patch is on: it is a surface to scatter on, not ground to
  render. With a Ground picked it follows that mesh's heights (a slope, a terrain); without,
  it is flat at the object's lowest point, which is under the ground for a house with a
  foundation.
* Base Height is an output: the height of the object's lowest point, for a Height Field that
  should start there (vines climbing from the ground).

Requires: pxr (OpenUSD), usd_chunks, numpy, scipy.
"""

from __future__ import annotations

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import cKDTree

from usd_chunks import chunk, chunk_stage, chunk_utils, kd_query, mesh_math

#: Most grid points along a side of the patch: a finer cell is widened to fit.
_MAX_SIDE = 220
#: Most points the object's surface is sampled at to find its footprint.
_MAX_SAMPLES = 300_000


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _hash(ix, iy, seed):
    h = (ix * 73856093) ^ (iy * 19349663) ^ (seed * 83492791)
    h = (h ^ (h >> 13)) * 1274126177
    return ((h ^ (h >> 16)) & 0xFFFFFF).astype(np.float64) / float(0xFFFFFF)


def noise2(p, seed):
    """Smooth value noise in 0..1 at (N, 2) points, two octaves, one unit per cell."""
    total = np.zeros(len(p))
    for octave, amp in ((0, 0.65), (1, 0.35)):
        q = np.asarray(p, dtype=np.float64) * (2.0 ** octave)
        i = np.floor(q).astype(np.int64)
        f = q - i
        f = f * f * (3.0 - 2.0 * f)
        s = seed + octave * 101
        a, b = _hash(i[:, 0], i[:, 1], s), _hash(i[:, 0] + 1, i[:, 1], s)
        c, d = _hash(i[:, 0], i[:, 1] + 1, s), _hash(i[:, 0] + 1, i[:, 1] + 1, s)
        total += amp * ((a + (b - a) * f[:, 0]) * (1 - f[:, 1]) + (c + (d - c) * f[:, 0]) * f[:, 1])
    return total


def footprint(points, tris, up, reach, spacing):
    """The object's lower part seen from above: (base height, (N, 2) points on the ground,
    (M, 2) points under it).

    Its surface is sampled every spacing, so a box's faces count, not only its corners, and
    what lies within reach of the lowest point is kept. The points under it are those of the
    faces that look down near its base: a box's bottom, not a ruin's open rooms.
    """
    heights = points[:, up]
    base = float(heights.min())
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    rng = np.random.default_rng(0)
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    # Evenly over the whole surface, within a budget: a cap per triangle left a box's two big
    # bottom triangles sampled so thinly that ground under it went unmasked.
    spacing = max(spacing, float(np.sqrt(area.sum() / _MAX_SAMPLES)))
    counts = np.ceil(area / max(spacing * spacing, 1e-12)).astype(np.int64) + 1
    tri = np.repeat(np.arange(len(tris)), counts)
    u, v = rng.random(len(tri)), rng.random(len(tri))
    flip = u + v > 1.0
    u[flip], v[flip] = 1.0 - u[flip], 1.0 - v[flip]
    samples = a[tri] + (b[tri] - a[tri]) * u[:, None] + (c[tri] - a[tri]) * v[:, None]
    normals = np.cross(b - a, c - a)
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    flat = [axis for axis in range(3) if axis != up]
    down = (normals[tri, up] < -0.5) & (samples[:, up] <= base + 0.1 * reach)
    under = samples[down][:, flat]
    samples = np.concatenate([samples, points])
    low = samples[samples[:, up] <= base + reach]
    return base, low[:, flat], under


def field(ground, foot, reach, softness):
    """1 at the footprint, fading to 0 at reach from it over the last softness of reach."""
    if not len(foot):
        return np.zeros(len(ground))
    dist, _ = kd_query.query(cKDTree(foot), ground, k=1, distance_upper_bound=reach * 1.01)
    dist = np.where(np.isfinite(dist), dist, reach * 2)
    fade = max(reach * softness, 1e-9)
    return 1.0 - _smoothstep((dist - (reach - fade)) / fade)


def ground_heights(upstream, paths, up, xy):
    """The height of the ground meshes at each (N, 2) point, None where none is under it."""
    flat = [axis for axis in range(3) if axis != up]
    points = []
    for path in paths:
        root = upstream.GetPrimAtPath(path)
        for prim in (Usd.PrimRange(root) if root else []):
            mesh = UsdGeom.Mesh(prim)
            if mesh and (pts := mesh.GetPointsAttr().Get()):
                points.append(mesh_math.world_points(prim, pts))
    if not points:
        return None
    points = np.concatenate(points)
    try:
        heights = LinearNDInterpolator(points[:, flat], points[:, up])(xy)
    except Exception:  # noqa: BLE001 - a ground with no area (a line of points)
        return None
    return heights


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    is_f, is_i, is_s, is_b = (chunk.is_param_t(t) for t in (float, int, str, bool))
    paths = [p.strip() for p in node.get_param("prim_path", is_s).value.split(",") if p.strip()]
    out_path = (node.get_param("primpath_location", is_s).value or "").strip()
    name = node.get_param("primvar", is_s).value.strip() or "around"
    reach = max(1e-3, node.get_param("reach", is_f).value)
    softness = float(np.clip(node.get_param("softness", is_f).value, 0.0, 1.0))
    breakup = float(np.clip(node.get_param("breakup", is_f).value, 0.0, 1.0))
    size = max(1e-3, node.get_param("breakup_size", is_f).value)
    seed = node.get_param("seed", is_i).value
    if not paths or not out_path:
        _report(node, "pick the object, and where the ground patch goes")
        return
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    up = 1 if UsdGeom.GetStageUpAxis(upstream).upper() == "Y" else 2
    points, tris = [], []
    for path in paths:
        root = upstream.GetPrimAtPath(path)
        for prim in (Usd.PrimRange(root) if root else []):
            mesh = UsdGeom.Mesh(prim)
            if not mesh or UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            pts = mesh.GetPointsAttr().Get()
            if not pts:
                continue
            tri, _, _ = mesh_math.fan_triangulate(mesh.GetFaceVertexCountsAttr().Get() or [],
                                                  mesh.GetFaceVertexIndicesAttr().Get() or [])
            tris.append(np.asarray(tri, dtype=np.int64) + sum(len(p) for p in points))
            points.append(mesh_math.world_points(prim, pts))
    if not points:
        _report(node, "no meshes found under the object")
        return
    points = np.concatenate(points)
    tris = np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)
    cell = node.get_param("cell", is_f).value
    cell = cell if cell > 0 else reach / 10.0
    base, foot, under = footprint(points, tris, up, reach, cell * 0.5)
    lo, hi = foot.min(axis=0) - reach, foot.max(axis=0) + reach
    cell = max(cell, float((hi - lo).max()) / _MAX_SIDE)
    nx, ny = (np.ceil((hi - lo) / cell).astype(int) + 1).tolist()
    gx, gy = np.meshgrid(lo[0] + np.arange(nx) * cell, lo[1] + np.arange(ny) * cell, indexing="ij")
    ground = np.stack([gx.ravel(), gy.ravel()], axis=1)
    values = field(ground, foot, reach, softness)
    if len(under):
        # Nothing under a closed object: it would never be seen.
        near, _ = kd_query.query(cKDTree(under), ground, k=1, distance_upper_bound=cell)
        values[np.isfinite(near)] = 0.0
    if breakup > 0.0:
        values *= 1.0 - breakup * (1.0 - _smoothstep((noise2(ground / size, seed) - 0.3) / 0.4))

    flat = [axis for axis in range(3) if axis != up]
    world = np.zeros((len(ground), 3))
    world[:, flat[0]], world[:, flat[1]] = ground[:, 0], ground[:, 1]
    heights = np.full(len(ground), base)
    ground_paths = [p.strip() for p in node.get_param("ground", is_s).value.split(",") if p.strip()]
    if ground_paths and (on := ground_heights(upstream, ground_paths, up, ground)) is not None:
        heights = np.where(np.isfinite(on), on, base)
        base = float(np.nanmin(on)) if np.isfinite(on).any() else base
    world[:, up] = heights + 1e-3 * reach  # a hair above, so it does not fight the ground in a viewport
    i = np.arange(nx - 1)[:, None] * ny + np.arange(ny - 1)[None, :]
    quads = np.stack([i, i + 1, i + ny + 1, i + ny], axis=-1).reshape(-1, 4)
    if up == 2:
        quads = quads[:, ::-1]  # faces up the stage's up axis

    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        parent = stage.GetPrimAtPath(Sdf.Path(out_path).GetParentPath())
        if parent and not parent.IsPseudoRoot():
            # Under the object (a style writes it there), in the object's own space.
            inverse = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(parent).GetInverse())
            world = world @ inverse[:3, :3] + inverse[3, :3]
        mesh = UsdGeom.Mesh.Define(stage, out_path)
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(world.astype(np.float32)))
        mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(quads), 4, dtype=np.int32)))
        mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(quads.ravel().astype(np.int32)))
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        lo3, hi3 = world.min(axis=0), world.max(axis=0)
        mesh.CreateExtentAttr([Gf.Vec3f(*lo3.tolist()), Gf.Vec3f(*hi3.tolist())])
        pv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(name, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
        pv.Set(Vt.FloatArray.FromNumpy(values.astype(np.float32)))
        if node.get_param("show_patch", is_b).value:
            tint = np.array([0.32, 0.28, 0.22]) * (0.6 + 0.4 * values[:, None])
            mesh.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex).Set(Vt.Vec3fArray.FromNumpy(tint.astype(np.float32)))
        else:
            mesh.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    if (out := node.get_param("base_height")) is not None and not np.isclose(out.value or 0.0, base):
        out.value = base
    _report(node, f"{nx} x {ny} ground points · {100.0 * float((values > 0.05).mean()):.0f}% around the object")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Around Field")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Object", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="What the ground goes around: a mesh, or a prim with meshes under it.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "ground", "Ground", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The ground the object stands on: the patch follows its heights. Empty: flat at the "
        "object's lowest point.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primpath_location", "Ground Patch", default_value="/around_ground", flags=labeled,
        tooltip="Where the ground patch mesh is written. Scatter on it.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primvar", "Primvar", default_value="around", flags=labeled,
        tooltip="Name of the field on the patch. Put it in a Scatter Points' Density Field.",
    ))
    chunk_api.add_param(chunk.Slider("reach", "Reach", min_value=0.05, max_value=10.0, default_value=1.0,
                                     tooltip="How far past the object the field reaches, in scene units.",
                                     gizmo="cube"))
    chunk_api.add_param(chunk.Slider("softness", "Softness", min_value=0.0, max_value=1.0, default_value=0.7,
                                     tooltip="How much of the Reach fades out. 0 = a hard edge."))
    chunk_api.add_param(chunk.Slider("breakup", "Breakup", min_value=0.0, max_value=1.0, default_value=0.5,
                                     tooltip="Break the band into patches. 0 = even all round."))
    chunk_api.add_param(chunk.Slider("breakup_size", "Breakup Size", min_value=0.05, max_value=5.0,
                                     default_value=0.5, tooltip="How big a patch is, in scene units."))
    chunk_api.add_param(chunk.Slider("seed", "Seed", min_value=0, max_value=100, default_value=0, is_int=True))
    chunk_api.add_param(chunk.Slider("cell", "Cell", min_value=0.0, max_value=1.0, default_value=0.0,
                                     tooltip="Spacing of the patch's grid, in scene units. 0 = a tenth of Reach."))
    chunk_api.add_param(chunk.Checkbox("show_patch", "Show Patch", default_value=False, flags=labeled,
                                       tooltip="Draw the patch, tinted by the field. Off: hidden, only scattered on."))
    chunk_api.add_param(chunk.Slider(
        "base_height", "Base Height", default_value=0.0, min_value=-1000.0, max_value=1000.0,
        flags=labeled | chunk.ChunkFlags.ParamFlags.OUTPUT | chunk.ChunkFlags.ParamFlags.TRANSIENT,
        tooltip="Read-only. The height of the object's lowest point.",
    ))
    chunk_api.add_param(chunk.Label("readout", "nothing marked yet", default_value="nothing marked yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="scatter",
        description="""
        Marks the ground around an object's base, to scatter rubble or plants around it.

        Lays a ground patch at the object's base (on Ground, if picked), Reach past its footprint, painted with
        a 0..1 primvar: 1 at the object, fading out over Softness, broken into patches by
        Breakup. Scatter Points on the patch, with the primvar as its Density Field, piles
        things around the object. The patch is hidden unless Show Patch is on. Base Height
        is the height of the object's lowest point.
        """,
        requirements=["numpy>=2,<3", "scipy>=1.11,<2"],
    )
