"""Texture baking: transfer an asset's look onto another mesh's UV layout.

A bake re-samples the *source* -- the full-detail asset, any number of meshes, materials
and textures -- into one texture set laid out on the *target*'s UVs: usually a proxy out of
Remesh Proxy / Poly Reduce, unwrapped by UV Unwrap. For every texel of the target's atlas:

1. find where on the target surface it sits (rasterize the target's UV triangles);
2. find what the source shows nearest that point;
3. write it into the texel -- the source's colour for the base-colour map; for the normal
   map, the source's surface direction expressed in the target's tangent frame, which is
   what restores the detail the low-poly surface smoothed away.

Then every empty texel is filled from its nearest baked one, so mip-mapping and filtering
never pull background into a chart's edge.

Who does step 2 is a **backend** (`BakeBackend`, a Protocol). The built-in one,
`NearestSampleBackend`, needs only numpy and scipy: it scatters coloured, oriented samples
over the source (`surface_samples`) and takes each texel from its nearest few. Closest
point rather than a ray along the normal -- for a proxy *enclosing* its source (a canopy
blob around its leaves) the nearest source surface is the one seen from outside anyway, and
no cage or ray distance has to be tuned. Ray-traced backends (Cycles, V-Ray) can register
themselves with `register_backend` and take the same request.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Protocol, runtime_checkable

import numpy as np

from usd_chunks import surface_samples

try:
    from scipy import ndimage
    from scipy.spatial import cKDTree

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheel
    ndimage = cKDTree = None
    _HAS_SCIPY = False

try:
    from PIL import Image
except ImportError:  # pragma: no cover - then nothing can be written
    Image = None

logger = logging.getLogger(__name__)

MAP_BASE_COLOUR = "basecolor"
MAP_NORMAL = "normal"
MAPS = (MAP_BASE_COLOUR, MAP_NORMAL)

# Candidate texels tested per rasterization batch: bounds memory at large resolutions.
_RASTER_BATCH = 4_000_000

# Most source samples one bake takes; past it the spacing widens instead of exhausting RAM.
_MAX_SAMPLES = 20_000_000

# Nearest source samples blended into one texel.
_NEIGHBOURS = 4


# ---------------------------------------------------------------------------
# the request a backend answers
# ---------------------------------------------------------------------------


class Target(NamedTuple):
    """The mesh baked onto, in world space, as triangles with per-corner UVs and normals.

    Several meshes sharing one atlas (UV Unwrap's One Atlas) are concatenated into one.
    """

    points: np.ndarray  # (n, 3) float64, world
    tris: np.ndarray  # (m, 3) int64
    corner_st: np.ndarray  # (m, 3, 2) float64
    corner_normals: np.ndarray  # (m, 3, 3) float64, world, unit length -- what it shades with


class BakeRequest(NamedTuple):
    sources: list  # [surface_samples.Source]
    target: Target
    resolution: int
    maps: frozenset  # subset of MAPS


@runtime_checkable
class BakeBackend(Protocol):
    """Answers a BakeRequest with one float32 (res, res, 3) image per requested map.

    Images are row 0 = top (v = 1), values in 0-1: base colour linear, normal map
    tangent-space encoded (n * 0.5 + 0.5, OpenGL convention: +Y along +v). Texels outside
    every chart may hold anything; `bake` fills them afterwards.
    """

    name: str

    def available(self) -> bool: ...

    def bake(self, request: BakeRequest) -> dict: ...


_BACKENDS: dict = {}


def register_backend(backend: BakeBackend) -> None:
    """Make a backend selectable by name. A later registration under a name replaces it."""
    _BACKENDS[backend.name] = backend


def backend_names() -> list:
    """Every registered backend's name, built-in first."""
    return list(_BACKENDS)


def get_backend(name: str) -> BakeBackend | None:
    return _BACKENDS.get(name)


# ---------------------------------------------------------------------------
# rasterizing the target's atlas
# ---------------------------------------------------------------------------


class Texels(NamedTuple):
    """The texels whose centres fall inside a UV triangle, and where inside it."""

    rows: np.ndarray  # (t,) int64
    cols: np.ndarray  # (t,) int64
    tri: np.ndarray  # (t,) int64, the target triangle
    bary: np.ndarray  # (t, 3) float64


def rasterize(corner_st, resolution) -> Texels:
    """Every texel centre inside a UV triangle. A texel on a shared edge goes to one side."""
    res = int(resolution)
    # Pixel space with texel centres on integers: x = u*res - 0.5, row = (1-v)*res - 0.5.
    px = np.empty(corner_st.shape, dtype=np.float64)
    px[..., 0] = corner_st[..., 0] * res - 0.5
    px[..., 1] = (1.0 - corner_st[..., 1]) * res - 0.5

    lo = np.clip(np.ceil(px.min(axis=1) - 1e-9), 0, res - 1).astype(np.int64)
    hi = np.clip(np.floor(px.max(axis=1) + 1e-9), -1, res - 1).astype(np.int64)
    span = np.maximum(hi - lo + 1, 0)
    count = span[:, 0] * span[:, 1]

    rows, cols, tris, barys = [], [], [], []
    order = np.flatnonzero(count)
    cumulative = np.cumsum(count[order])
    start = 0
    while start < len(order):
        # A batch of triangles whose candidate texels fit the budget (always at least one).
        base = cumulative[start - 1] if start else 0
        stop = max(start + 1, int(np.searchsorted(cumulative, base + _RASTER_BATCH, side="right")))
        batch = order[start:stop]
        start = stop

        n = count[batch]
        tri = np.repeat(batch, n)
        local = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
        width = np.repeat(span[batch, 0], n)
        x = lo[tri, 0] + local % width
        y = lo[tri, 1] + local // width

        a, b, c = px[tri, 0], px[tri, 1], px[tri, 2]
        denom = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        ok = np.abs(denom) > 1e-12
        denom = np.where(ok, denom, 1.0)
        w0 = ((b[:, 1] - c[:, 1]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (y - c[:, 1])) / denom
        w1 = ((c[:, 1] - a[:, 1]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (y - c[:, 1])) / denom
        w2 = 1.0 - w0 - w1
        inside = ok & (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        rows.append(y[inside])
        cols.append(x[inside])
        tris.append(tri[inside])
        barys.append(np.stack([w0[inside], w1[inside], w2[inside]], axis=1))

    if not rows:
        empty = np.zeros(0, dtype=np.int64)
        return Texels(empty, empty, empty, np.zeros((0, 3)))
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    tris, barys = np.concatenate(tris), np.concatenate(barys)
    # A texel centre exactly on an edge two triangles share passes both inside tests; the
    # first claim stands, so every texel is written once and counted once.
    _flat, first = np.unique(rows * res + cols, return_index=True)
    return Texels(rows[first], cols[first], tris[first], barys[first])


def _unit(vectors):
    length = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return np.divide(vectors, length, out=np.zeros_like(vectors), where=length > 1e-12)


def tangent_frames(target: Target, texels: Texels):
    """`(tangent, bitangent, normal)` per texel: the frame a renderer decodes the normal map in.

    The tangent and bitangent follow +u and +v across each triangle -- the frame Storm and
    other derivative-based shaders rebuild per pixel -- made orthonormal against the
    interpolated shading normal.
    """
    p = target.points[target.tris]  # (m, 3, 3)
    st = target.corner_st
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    d1, d2 = st[:, 1] - st[:, 0], st[:, 2] - st[:, 0]
    det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    inv = np.divide(1.0, det, out=np.zeros_like(det), where=np.abs(det) > 1e-20)[:, None]
    tri_tangent = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) * inv
    tri_bitangent = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) * inv

    normal = _unit(np.einsum("tk,tkc->tc", texels.bary, target.corner_normals[texels.tri]))
    tangent = tri_tangent[texels.tri]
    tangent = _unit(tangent - normal * np.einsum("tc,tc->t", tangent, normal)[:, None])
    bitangent = np.cross(normal, tangent)
    flip = np.einsum("tc,tc->t", bitangent, tri_bitangent[texels.tri]) < 0.0
    bitangent[flip] *= -1.0  # a mirrored chart
    return tangent, bitangent, normal


def fill_gutters(image, covered):
    """Every texel outside the charts takes its nearest baked texel's value."""
    if covered.all() or not covered.any():
        return image
    _distance, (rows, cols) = ndimage.distance_transform_edt(~covered, return_indices=True)
    return image[rows, cols]


# ---------------------------------------------------------------------------
# the built-in backend
# ---------------------------------------------------------------------------


def _texel_size(target: Target, n_texels: int) -> float:
    area = surface_samples.triangle_areas(target.points, target.tris).sum()
    return float(np.sqrt(area / max(1, n_texels)))


class NearestSampleBackend:
    """Each texel from the nearest few samples of the source surface. numpy + scipy only."""

    name = "Built-in"

    def available(self) -> bool:
        return _HAS_SCIPY

    def bake(self, request: BakeRequest) -> dict:
        res = int(request.resolution)
        target = request.target
        texels = rasterize(target.corner_st, res)
        images = {m: np.zeros((res, res, 3), dtype=np.float32) for m in request.maps}
        if not len(texels.rows) or not request.sources:
            return images

        # One sample per texel's width on the source keeps detail at texture resolution
        # without sampling finer than the map can hold.
        spacing = _texel_size(target, len(texels.rows))
        expected = surface_samples.surface_area(request.sources) / (spacing * spacing)
        if expected > _MAX_SAMPLES:
            spacing *= float(np.sqrt(expected / _MAX_SAMPLES))
        samples = surface_samples.sample_sources(request.sources, spacing)
        if not len(samples.points):
            return images

        where = np.einsum("tk,tkc->tc", texels.bary, target.points[target.tris[texels.tri]])
        k = min(_NEIGHBOURS, len(samples.points))
        distance, nearest = cKDTree(samples.points).query(where, k=k)
        if k == 1:
            distance, nearest = distance[:, None], nearest[:, None]
        weights = 1.0 / np.maximum(distance, 1e-12)
        weights /= weights.sum(axis=1, keepdims=True)

        if MAP_BASE_COLOUR in images:
            colour = np.einsum("tk,tkc->tc", weights, samples.colours[nearest])
            images[MAP_BASE_COLOUR][texels.rows, texels.cols] = colour
        if MAP_NORMAL in images:
            tangent, bitangent, normal = tangent_frames(target, texels)
            near = samples.normals[nearest].astype(np.float64)  # (t, k, 3)
            # Leaf cards and sheets are two-sided: point every sample's normal out of the
            # side the target sees it from before blending, or the two sides cancel out.
            facing = np.sign(np.einsum("tkc,tc->tk", near, normal))
            facing[facing == 0.0] = 1.0
            source_normal = _unit(np.einsum("tk,tkc->tc", weights * facing, near))
            local = np.stack(
                [
                    np.einsum("tc,tc->t", source_normal, tangent),
                    np.einsum("tc,tc->t", source_normal, bitangent),
                    np.einsum("tc,tc->t", source_normal, normal),
                ],
                axis=1,
            )
            images[MAP_NORMAL][texels.rows, texels.cols] = local * 0.5 + 0.5
        return images


register_backend(NearestSampleBackend())


# ---------------------------------------------------------------------------
# running a bake, writing its maps
# ---------------------------------------------------------------------------


def bake(request: BakeRequest, backend_name: str = NearestSampleBackend.name) -> dict:
    """Run `request` on the named backend and fill the gutters: {map: (res, res, 3) float32}."""
    backend = get_backend(backend_name)
    if backend is None or not backend.available():
        raise RuntimeError(f"bake backend {backend_name!r} is not available")
    images = backend.bake(request)
    covered = np.zeros((request.resolution, request.resolution), dtype=bool)
    texels = rasterize(request.target.corner_st, request.resolution)
    covered[texels.rows, texels.cols] = True
    return {name: fill_gutters(image, covered) for name, image in images.items()}


def linear_to_srgb(values):
    values = np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)
    return np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1.0 / 2.4) - 0.055)


def write_map(path, image, srgb: bool) -> None:
    """Save one map as an 8-bit PNG; base colour sRGB-encoded, data maps (normals) raw."""
    data = linear_to_srgb(image) if srgb else np.clip(image, 0.0, 1.0)
    Image.fromarray(np.round(data * 255.0).astype(np.uint8), "RGB").save(path)
