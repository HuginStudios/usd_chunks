"""Remesh Proxy chunk -- one closed, coloured stand-in mesh for a whole asset.

Decimation can only take triangles away from the topology it is given. That is the right
tool for a solid prop and the wrong one for an *aggregate*: a tree is ten thousand leaf
cards, a CAD assembly is two thousand parts with screws inside, and neither can be reduced
to a few hundred triangles by collapsing edges, because there is nothing connecting the
pieces to collapse across. This chunk throws the topology away and rebuilds the surface
from a volume instead:

1. **Sample** points over every triangle under the picked prim, area-weighted, each one
   carrying the colour the asset shows there -- `displayColor`, or the bound material's
   diffuse / base colour, read from its texture when it has one. Instance proxies and
   PointInstancer copies are included, so a scattered canopy counts every leaf.
   Alpha-cutout leaves drop the samples that land on transparent texels, or the cut-away
   card would become volume.
2. **Distance field** on a voxel grid: the *unsigned* distance to the nearest sample. Leaf
   cards and sheet metal are open surfaces with no inside, so a signed field is undefined
   for them; meshing the unsigned one at `thickness` inflates every card into a shell
   instead.
3. **Close** (dilate by `close`, then erode by `close`): gaps narrower than about twice
   `close` fill in, so a canopy becomes one blob rather than a cloud of fragments, without
   the blob growing. **Fill interior** then fills every cavity not connected to the
   outside, which is what deletes a CAD assembly's hidden internals and stops a closed
   shell from coming out double-walled.
4. **Marching cubes** turns the field into a triangle mesh. It is dense and even -- feed it
   to Poly Reduce for the triangle budget, which spends it on the silhouette.
5. **Bake** each vertex's colour as the distance-weighted average of its nearest samples,
   into `primvars:displayColor`. Poly Reduce carries vertex primvars through, so the colour
   survives the reduction.

Sizes are in voxels, not scene units, so the same settings behave the same on a tree in
centimetres and a building in metres; Resolution is the one knob that sets the scale.

Reads through `get_upstream_stage()`, like every chunk that writes near what it reads: the
proxy lands beside the source by default, and a composed read would sample it on the next
cook.
"""

from __future__ import annotations

import logging
import os
import zlib
from typing import NamedTuple

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk
from usd_chunks import chunk_stage
from usd_chunks.mesh_math import fan_triangulate

try:
    from scipy import ndimage
    from scipy.spatial import cKDTree
    from skimage.measure import marching_cubes

    _HAS_DEPS = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheels
    ndimage = cKDTree = marching_cubes = None
    _HAS_DEPS = False

try:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # 16k texture sheets are normal, not a decompression bomb
except ImportError:  # pragma: no cover - textures then fall back to the material's colour
    Image = None

logger = logging.getLogger(__name__)

PURPOSE_DEFAULT = "default"
PURPOSE_RENDER = "render"
PURPOSE_PROXY = "proxy"
_PURPOSES = [PURPOSE_DEFAULT, PURPOSE_RENDER, PURPOSE_PROXY]

# The colour a surface gets when nothing on it says otherwise -- USD's own fallback grey.
_FALLBACK_COLOUR = (0.18, 0.18, 0.18)

# Samples per voxel edge. Two keeps every voxel a surface passes through occupied, which
# is what stops a thin card from coming out as a dotted line of blobs.
_SAMPLES_PER_VOXEL = 2.0

# The most surface samples one cook will take. A 4M-triangle tree at resolution 384 asks
# for less than this; past it the spacing is widened rather than running out of memory.
_MAX_SAMPLES = 30_000_000

# Nearest samples averaged into one baked vertex colour.
_BAKE_NEIGHBOURS = 8

# Inputs a surface shader's colour can come from: UsdPreviewSurface, then the MaterialX
# surfaces (standard_surface, open_pbr_surface).
_COLOUR_INPUTS = ("diffuseColor", "base_color")
_OPACITY_INPUTS = ("opacity", "geometry_opacity")


# ---------------------------------------------------------------------------
# array maths -- no USD, unit-testable on its own
# ---------------------------------------------------------------------------


def triangle_areas(points, tris) -> np.ndarray:
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


def sample_triangles(points, tris, spacing, rng):
    """Area-weighted points over a triangle soup: `(tri_index, barycentrics)`.

    Every triangle gets at least one sample, however small, so a mesh of tiny triangles
    is not sampled into nothing; a large one gets one per `spacing**2` of area.
    """
    if len(tris) == 0:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 3))
    per_tri = np.maximum(1, np.ceil(triangle_areas(points, tris) / (spacing * spacing))).astype(np.int64)
    tri_index = np.repeat(np.arange(len(tris), dtype=np.int64), per_tri)
    # Uniform over the triangle: the square-root fold of two uniforms.
    r1 = np.sqrt(rng.random(len(tri_index)))
    r2 = rng.random(len(tri_index))
    bary = np.stack([1.0 - r1, r1 * (1.0 - r2), r1 * r2], axis=1)
    return tri_index, bary


class Field(NamedTuple):
    """A scalar grid -- positive inside the proxy -- and where its voxel centres sit."""

    values: np.ndarray  # (nx, ny, nz) float32
    origin: np.ndarray  # world position of voxel (0, 0, 0)
    voxel: float


def build_field(samples, voxel, thickness, close, fill, smooth) -> Field:
    """The proxy's volume, from surface samples. Sizes are in voxels; see the module doc.

    `thickness` is how far the shell stands off the samples, `close` how wide a gap gets
    bridged, `fill` whether enclosed cavities become solid, `smooth` a Gaussian sigma that
    takes the voxel staircase out before the surface is extracted.
    """
    pad = int(np.ceil(thickness + close + 3.0 * smooth)) + 2
    lo = samples.min(axis=0)
    origin = lo - pad * voxel
    shape = np.ceil((samples.max(axis=0) - lo) / voxel).astype(np.int64) + 1 + 2 * pad
    occupied = np.zeros(tuple(shape), dtype=bool)
    cells = np.rint((samples - origin) / voxel).astype(np.int64)
    occupied[cells[:, 0], cells[:, 1], cells[:, 2]] = True

    # Distance to the nearest sample, then everything within thickness + close of one:
    # the dilation half of the closing.
    grown = ndimage.distance_transform_edt(~occupied) <= (thickness + close)
    if fill:
        grown = ndimage.binary_fill_holes(grown)
    # A signed distance to that grown region, so eroding it by `close` is a level set rather
    # than another binary pass -- and the surface comes out smooth instead of in voxel steps.
    inside = ndimage.distance_transform_edt(grown)
    outside = ndimage.distance_transform_edt(~grown)
    values = (inside - outside - close).astype(np.float32)
    if smooth > 0.0:
        values = ndimage.gaussian_filter(values, sigma=smooth)
    return Field(values, origin, float(voxel))


def extract_surface(field: Field):
    """`(points, triangles)` of the field's zero level, outward-facing, or None if empty."""
    if not (field.values.max() > 0.0 > field.values.min()):
        return None
    verts, faces, _normals, _values = marching_cubes(
        field.values, 0.0, spacing=(field.voxel,) * 3, allow_degenerate=False
    )
    # marching_cubes winds a positive-inside field's faces inward; USD's default
    # orientation (rightHanded) wants them counter-clockwise seen from outside.
    faces = np.ascontiguousarray(faces[:, ::-1], dtype=np.int64)
    return verts.astype(np.float64) + field.origin, faces


def bake_colours(points, sample_points, sample_colours, neighbours=_BAKE_NEIGHBOURS):
    """Each point's colour, the inverse-distance average of its nearest coloured samples."""
    k = min(neighbours, len(sample_points))
    distance, nearest = cKDTree(sample_points).query(points, k=k)
    if k == 1:
        distance, nearest = distance[:, None], nearest[:, None]
    weights = 1.0 / np.maximum(distance, 1e-9)
    weights /= weights.sum(axis=1, keepdims=True)
    return np.einsum("ij,ijc->ic", weights, sample_colours[nearest])


def srgb_to_linear(values):
    values = np.asarray(values, dtype=np.float32)
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


# ---------------------------------------------------------------------------
# reading the asset: geometry and the colour it shows
# ---------------------------------------------------------------------------


class _Texture(NamedTuple):
    """One texture lookup: a file, the channel(s) to read, how to decode them."""

    path: str
    channels: str  # "rgb", or one of "r", "g", "b", "a" for a scalar read
    linearize: bool
    st: str  # the primvar the lookup reads its coordinates from


class _Look(NamedTuple):
    """What a material shows, as far as a proxy cares: a colour, maybe from a texture,
    and maybe an alpha cut-out."""

    colour: tuple
    colour_texture: _Texture | None
    cutout: _Texture | None
    threshold: float


class _Textured(NamedTuple):
    """The triangles of one source that read a texture, and the coordinates they read."""

    tris: np.ndarray  # (k,) triangle indices into the source
    corner_st: np.ndarray  # (k, 3, 2)
    colour: _Texture | None
    cutout: _Texture | None
    threshold: float


class _Source(NamedTuple):
    """One mesh's triangles in its own space, how they are coloured, and every placement
    of it in the world -- one for a plain mesh, one per instance under a PointInstancer."""

    path: str
    points: np.ndarray  # (n, 3) float64, local
    tris: np.ndarray  # (m, 3) int64
    corner_colours: np.ndarray  # (m, 3, 3) float32
    textured: list  # [_Textured]
    placements: np.ndarray  # (i, 4, 4) float64 local-to-world, row-vector convention


def _connected_shader(shader_input):
    """`(shader, output name)` feeding an input, or (None, None)."""
    if not shader_input or not shader_input.HasConnectedSource():
        return None, None
    sources, _invalid = shader_input.GetConnectedSources()
    if not sources:
        return None, None
    return UsdShade.Shader(sources[0].source.GetPrim()), str(sources[0].sourceName)


def _texture_of(shader, output_name, scalar) -> _Texture | None:
    """The file a texture node reads, if `shader` is one (UsdUVTexture, MaterialX image)."""
    file_input = shader.GetInput("file") if shader else None
    asset = file_input.Get() if file_input else None
    path = (asset.resolvedPath or asset.path) if asset else ""
    if not path or "<UDIM>" in path:
        return None

    space = shader.GetInput("sourceColorSpace")
    space = str(space.Get()) if space and space.Get() else "auto"

    st = "st"
    reader, _ = _connected_shader(shader.GetInput("st") or shader.GetInput("texcoord"))
    if reader and (varname := reader.GetInput("varname") or reader.GetInput("geomprop")):
        st = str(varname.Get() or "st")

    if scalar:
        channels = output_name if output_name in ("r", "g", "b", "a") else "r"
    else:
        channels = "rgb"
    return _Texture(path, channels, linearize=space != "raw" and channels == "rgb", st=st)


def _look(material) -> _Look | None:
    """What a bound material shows, or None if its surface isn't one this chunk reads."""
    if not material:
        return None
    shader = None
    for context in ("", "mtlx"):
        if shader := material.ComputeSurfaceSource(context)[0]:
            break
    if not shader:
        return None

    colour, colour_texture = None, None
    for name in _COLOUR_INPUTS:
        if not (shader_input := shader.GetInput(name)):
            continue
        source, output = _connected_shader(shader_input)
        if source:
            colour_texture = _texture_of(source, output, scalar=False)
            fallback = source.GetInput("fallback")
            value = fallback.Get() if fallback else None
            colour = tuple(value)[:3] if value is not None else None
        else:
            value = shader_input.Get()
            colour = tuple(value)[:3] if value is not None else None
        break

    cutout = None
    for name in _OPACITY_INPUTS:
        source, output = _connected_shader(shader.GetInput(name))
        if source:
            cutout = _texture_of(source, output, scalar=True)
            break
    threshold_input = shader.GetInput("opacityThreshold")
    threshold = float(threshold_input.Get() or 0.0) if threshold_input else 0.0

    return _Look(colour or _FALLBACK_COLOUR, colour_texture, cutout, threshold or 0.5)


def _per_corner(values, interpolation, tris, corners, face_of_tri):
    """A primvar's values laid out per triangle corner, or None if they don't fit."""
    values = np.asarray(values)
    n = len(values)
    if interpolation == UsdGeom.Tokens.constant and n >= 1:
        return np.broadcast_to(values[0], (len(tris), 3) + values.shape[1:])
    if interpolation == UsdGeom.Tokens.uniform and n > face_of_tri.max(initial=-1):
        return np.repeat(values[face_of_tri][:, None], 3, axis=1)
    if interpolation in (UsdGeom.Tokens.vertex, UsdGeom.Tokens.varying) and n > tris.max(initial=-1):
        return values[tris]
    if interpolation == UsdGeom.Tokens.faceVarying and n > corners.max(initial=-1):
        return values[corners]
    return None


def _primvar_corners(prim, name, tris, corners, face_of_tri):
    primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(name)
    if not primvar or not primvar.HasValue():
        return None
    try:
        values = primvar.ComputeFlattened()
    except Exception:  # pragma: no cover - a malformed primvar shouldn't take the cook down
        values = primvar.Get()
    if not values:
        return None
    return _per_corner(values, primvar.GetInterpolation(), tris, corners, face_of_tri)


def _face_looks(prim, n_faces, looks: dict):
    """`(look per face code, code per face)` from the prim's binding and its subsets'."""
    whole = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
    codes = np.zeros(n_faces, dtype=np.int64)
    materials = [whole]
    for subset in UsdShade.MaterialBindingAPI(prim).GetMaterialBindSubsets():
        material = UsdShade.MaterialBindingAPI(subset.GetPrim()).ComputeBoundMaterial()[0]
        indices = subset.GetIndicesAttr().Get()
        if not material or not indices:
            continue
        idx = np.asarray(indices, dtype=np.int64)
        codes[idx[(idx >= 0) & (idx < n_faces)]] = len(materials)
        materials.append(material)

    resolved = []
    for material in materials:
        key = str(material.GetPath()) if material else ""
        if key not in looks:
            looks[key] = _look(material)
        resolved.append(looks[key])
    return resolved, codes


def _read_mesh(prim, placements, looks: dict) -> _Source | None:
    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None
    tris, corners, face_of_tri = fan_triangulate(counts, indices)
    points = np.asarray(points, dtype=np.float64)
    if len(tris) == 0 or tris.max() >= len(points):
        return None

    display = _primvar_corners(prim, "displayColor", tris, corners, face_of_tri)
    if display is None:
        display = np.broadcast_to(np.asarray(_FALLBACK_COLOUR), (len(tris), 3, 3))
    corner_colours = np.array(display, dtype=np.float32)

    textured = []
    face_looks, codes = _face_looks(prim, len(counts), looks)
    tri_codes = codes[face_of_tri]
    for code, look in enumerate(face_looks):
        if look is None:
            continue  # no material, or one this chunk can't read: displayColor stands
        mask = tri_codes == code
        if not mask.any():
            continue
        corner_colours[mask] = look.colour
        lookups = [t for t in (look.colour_texture, look.cutout) if t]
        if not lookups:
            continue
        st = _primvar_corners(prim, lookups[0].st, tris, corners, face_of_tri)
        if st is None or np.asarray(st).shape[-1:] != (2,):
            continue
        which = np.flatnonzero(mask)
        textured.append(
            _Textured(which, np.asarray(st, dtype=np.float64)[which], look.colour_texture, look.cutout, look.threshold)
        )

    return _Source(str(prim.GetPath()), points, tris, corner_colours, textured, placements)


def _matrix(gf_matrix) -> np.ndarray:
    return np.asarray(gf_matrix, dtype=np.float64)


def _skipped(prim) -> bool:
    """Prims a proxy should not see: hidden, or not the render geometry itself."""
    imageable = UsdGeom.Imageable(prim)
    if not imageable:
        return False
    visibility = imageable.GetVisibilityAttr().Get()
    purpose = imageable.GetPurposeAttr().Get()
    return visibility == UsdGeom.Tokens.invisible or purpose in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy)


def _instancer_sources(prim, xcache, looks: dict, out: list) -> None:
    instancer = UsdGeom.PointInstancer(prim)
    time = Usd.TimeCode.Default()
    xforms = instancer.ComputeInstanceTransformsAtTime(time, time)
    proto_indices = instancer.GetProtoIndicesAttr().Get()
    if not xforms or not proto_indices:
        return
    instances = np.asarray([_matrix(m) for m in xforms]) @ _matrix(xcache.GetLocalToWorldTransform(prim))
    proto_indices = np.asarray(proto_indices, dtype=np.int64)[: len(instances)]

    stage = prim.GetStage()
    for index, target in enumerate(instancer.GetPrototypesRel().GetTargets()):
        mine = instances[proto_indices == index]
        prototype = stage.GetPrimAtPath(target)
        if not len(mine) or not prototype:
            continue
        for candidate in Usd.PrimRange(prototype, Usd.TraverseInstanceProxies()):
            if candidate.IsA(UsdGeom.PointInstancer):
                logger.warning("skipping nested PointInstancer %s", candidate.GetPath())
                continue
            if not candidate.IsA(UsdGeom.Mesh):
                continue
            local = _matrix(xcache.ComputeRelativeTransform(candidate, prototype)[0])
            if (source := _read_mesh(candidate, local @ mine, looks)) is not None:
                out.append(source)


def gather_sources(root) -> list:
    """Every mesh a proxy of `root` should cover, with its placements in the world."""
    if not root or not root.IsValid():
        return []
    xcache = UsdGeom.XformCache()
    looks: dict = {}
    sources: list = []
    prims = iter(Usd.PrimRange(root, Usd.TraverseInstanceProxies()))
    for prim in prims:
        if _skipped(prim):
            prims.PruneChildren()
        elif prim.IsA(UsdGeom.PointInstancer):
            _instancer_sources(prim, xcache, looks, sources)
            prims.PruneChildren()
        elif prim.IsA(UsdGeom.Mesh):
            world = _matrix(xcache.GetLocalToWorldTransform(prim))[None]
            if (source := _read_mesh(prim, world, looks)) is not None:
                sources.append(source)
    return sources


# ---------------------------------------------------------------------------
# sampling the sources
# ---------------------------------------------------------------------------


def _bounds(sources):
    """World `(lo, hi)` over every placement of every source."""
    corners = []
    for source in sources:
        lo, hi = source.points.min(axis=0), source.points.max(axis=0)
        box = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        placed = np.einsum("bj,ijk->ibk", box, source.placements[:, :3, :3]) + source.placements[:, None, 3, :3]
        corners.append(placed.reshape(-1, 3))
    corners = np.concatenate(corners)
    return corners.min(axis=0), corners.max(axis=0)


def _scale(placements) -> float:
    """A representative uniform scale for a set of placements -- the median, so one
    oddly-sized instance doesn't set the sampling density for the other ten thousand."""
    det = np.abs(np.linalg.det(placements[:, :3, :3]))
    return float(np.cbrt(np.median(det))) or 1.0


def _surface_area(sources) -> float:
    return sum(
        triangle_areas(s.points, s.tris).sum() * _scale(s.placements) ** 2 * len(s.placements) for s in sources
    )


class _Images:
    """Texture files decoded once per cook, kept as uint8 to keep a 16k sheet affordable."""

    def __init__(self):
        self._cache: dict = {}

    def get(self, path):
        if path not in self._cache:
            self._cache[path] = self._load(path)
        return self._cache[path]

    @staticmethod
    def _load(path):
        if Image is None or not os.path.isfile(path):
            return None
        try:
            with Image.open(path) as image:
                return np.asarray(image.convert("RGBA"))
        except Exception as exc:  # an unreadable texture costs its colour, not the cook
            logger.warning("can't read texture %s: %s", path, exc)
            return None

    def sample(self, texture: _Texture, st):
        """`texture` at each st, nearest texel, repeat-wrapped: (n, 3) or (n,), or None."""
        image = self.get(texture.path)
        if image is None:
            return None
        height, width = image.shape[:2]
        u = np.mod(st[:, 0], 1.0)
        v = np.mod(st[:, 1], 1.0)
        rows = np.clip(((1.0 - v) * height).astype(np.int64), 0, height - 1)
        cols = np.clip((u * width).astype(np.int64), 0, width - 1)
        texels = image[rows, cols].astype(np.float32) / 255.0
        if texture.channels == "rgb":
            rgb = texels[:, :3]
            return srgb_to_linear(rgb) if texture.linearize else rgb
        return texels[:, "rgba".index(texture.channels)]


def _sample_source(source: _Source, spacing, rng, images: _Images):
    """World-space samples of one source, and their colours."""
    local_spacing = spacing / _scale(source.placements)
    tri_index, bary = sample_triangles(source.points, source.tris, local_spacing, rng)
    corners = source.points[source.tris[tri_index]]  # (s, 3, 3)
    local = np.einsum("sk,skc->sc", bary, corners)
    colours = np.einsum("sk,skc->sc", bary, source.corner_colours[tri_index])
    keep = np.ones(len(tri_index), dtype=bool)

    for part in source.textured:
        # Which samples landed on this part's triangles, and where in its st they are.
        lookup = np.full(len(source.tris), -1, dtype=np.int64)
        lookup[part.tris] = np.arange(len(part.tris))
        row = lookup[tri_index]
        hits = np.flatnonzero(row >= 0)
        if not len(hits):
            continue
        st = np.einsum("sk,skc->sc", bary[hits], part.corner_st[row[hits]])
        if part.colour and (rgb := images.sample(part.colour, st)) is not None:
            colours[hits] = rgb
        if part.cutout and (alpha := images.sample(part.cutout, st)) is not None:
            keep[hits[alpha < part.threshold]] = False

    local, colours = local[keep], colours[keep]
    placements = source.placements
    world = np.einsum("sc,icd->isd", local, placements[:, :3, :3]) + placements[:, None, 3, :3]
    return world.reshape(-1, 3), np.broadcast_to(colours, (len(placements),) + colours.shape).reshape(-1, 3)


def sample_sources(sources, spacing):
    """Every source's world-space samples and colours, concatenated."""
    images = _Images()
    rng = np.random.default_rng(0)  # the same proxy every cook, not a shimmering one
    points, colours = [], []
    for source in sources:
        p, c = _sample_source(source, spacing, rng, images)
        points.append(p)
        colours.append(c)
    if not points:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.float32)
    return np.concatenate(points), np.concatenate(colours).astype(np.float32)


# ---------------------------------------------------------------------------
# the whole build, USD-free once the sources are read
# ---------------------------------------------------------------------------


class _Settings(NamedTuple):
    resolution: int
    thickness: float
    close: float
    fill: bool
    smooth: float
    bake: bool


class Proxy(NamedTuple):
    points: np.ndarray  # (n, 3) world space
    triangles: np.ndarray  # (m, 3)
    colours: np.ndarray | None  # (n, 3) or None when baking is off
    source_triangles: int
    voxel: float


def build_proxy(sources, settings: _Settings) -> Proxy | None:
    if not sources:
        return None
    lo, hi = _bounds(sources)
    voxel = float(np.max(hi - lo)) / max(1, settings.resolution)
    if voxel <= 0.0:
        return None

    spacing = voxel / _SAMPLES_PER_VOXEL
    expected = _surface_area(sources) / (spacing * spacing)
    if expected > _MAX_SAMPLES:
        spacing *= float(np.sqrt(expected / _MAX_SAMPLES))
    samples, sample_colours = sample_sources(sources, spacing)
    if not len(samples):
        return None

    field = build_field(samples, voxel, settings.thickness, settings.close, settings.fill, settings.smooth)
    surface = extract_surface(field)
    if surface is None:
        return None
    points, triangles = surface
    colours = bake_colours(points, samples, sample_colours) if settings.bake else None
    source_triangles = sum(len(s.tris) * len(s.placements) for s in sources)
    return Proxy(points, triangles, colours, source_triangles, voxel)


# ---------------------------------------------------------------------------
# skip-cook: same sources, same settings, same proxy
# ---------------------------------------------------------------------------


def _crc(array) -> tuple:
    array = np.ascontiguousarray(array)
    return array.shape, str(array.dtype), zlib.crc32(array)


def _texture_stamp(texture: _Texture | None):
    if texture is None:
        return None
    try:
        mtime = os.path.getmtime(texture.path)
    except OSError:
        mtime = None
    return texture + (mtime,)


def fingerprint(sources, settings: _Settings) -> tuple:
    """Everything the proxy depends on. Reading the sources is cheap next to building one
    (a few hundred ms against several seconds), so a recook that changed nothing upstream
    -- every upstream cook triggers one -- costs only the read."""
    parts = [tuple(settings)]
    for s in sources:
        textures = tuple(
            (_crc(t.tris), _crc(t.corner_st), _texture_stamp(t.colour), _texture_stamp(t.cutout), t.threshold)
            for t in s.textured
        )
        parts.append(
            (s.path, _crc(s.points), _crc(s.tris), _crc(s.corner_colours), _crc(s.placements), textures)
        )
    return tuple(parts)


# Keyed by the chunk's own layer identifier: (fingerprint, proxy).
_LAST_COOK: dict = {}


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------


def _compact(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 10_000:
        return f"{count / 1_000:.0f}k"
    return f"{count:,}"


def _report(node, proxy: Proxy | None, note="") -> None:
    if proxy is not None:
        text = f"{_compact(proxy.source_triangles)} → {_compact(len(proxy.triangles))} tris"
    else:
        text = note or "nothing to remesh — check the prim"
    if readout := node.get_param("readout"):
        readout.value = text
    if out_tris := node.get_param("out_triangles"):
        out_tris.value = len(proxy.triangles) if proxy is not None else 0
    logger.debug("%s", text)


def _settings(node) -> _Settings:
    is_f = chunk.is_param_t(float)
    is_b = chunk.is_param_t(bool)
    return _Settings(
        resolution=int(node.get_param("resolution", chunk.is_param_t(int)).value),
        thickness=float(node.get_param("thickness", is_f).value),
        close=float(node.get_param("close", is_f).value),
        fill=bool(node.get_param("fill_interior", is_b).value),
        smooth=float(node.get_param("smooth", is_f).value),
        bake=bool(node.get_param("bake_colour", is_b).value),
    )


def _output_path(node, source_path: str) -> str:
    """The proxy's prim: the param if set, else a sibling of the source named after it.

    A sibling and not a child, because purpose inherits -- under a `render` asset a
    `proxy` mesh would be overruled by its parent.
    """
    out = node.get_param("out_path").value
    if out:
        return out
    source = Sdf.Path(source_path)
    if source == Sdf.Path.absoluteRootPath:
        return "/Proxy"
    return str(source.GetParentPath().AppendChild(f"{source.name}_proxy"))


def _sources(node, paths):
    """Read through a composition without this chunk's layer, holding the scratch stage."""
    upstream = chunk_stage.get_upstream_stage(node.get_stage(), node.get_scene(), node)
    return upstream, [source for path in paths for source in gather_sources(upstream.GetPrimAtPath(path))]


def cache_key(node):
    """What the cook cache keys a proxy on beyond the params: the source geometry and looks."""
    paths = node.get_param("prim_path").resolve_paths()
    if not paths or not _HAS_DEPS:
        return ()
    _upstream, sources = _sources(node, paths)
    return fingerprint(sources, _settings(node))


def _author(stage, path: str, proxy: Proxy, purpose: str) -> None:
    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path(path))
    points = proxy.points
    parent_path = Sdf.Path(path).GetParentPath()
    # Not looked up at all under the root: a read of "/" is a read of the whole stage to
    # skip-cooking, and would stop it ever masking this chunk's cooks.
    parent = stage.GetPrimAtPath(parent_path) if parent_path != Sdf.Path.absoluteRootPath else None
    if parent and UsdGeom.Xformable(parent):
        # The proxy was built in world space; express it in its parent's.
        to_parent = _matrix(UsdGeom.XformCache().GetLocalToWorldTransform(parent).GetInverse())
        points = points @ to_parent[:3, :3] + to_parent[3, :3]
    points32 = np.ascontiguousarray(points, dtype=np.float32)

    mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(points32))
    mesh.CreateFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.full(len(proxy.triangles), 3, dtype=np.int32)))
    mesh.CreateFaceVertexIndicesAttr().Set(
        Vt.IntArray.FromNumpy(np.ascontiguousarray(proxy.triangles.reshape(-1), dtype=np.int32))
    )
    mesh.CreateExtentAttr().Set(
        Vt.Vec3fArray([Gf.Vec3f(*map(float, points32.min(axis=0))), Gf.Vec3f(*map(float, points32.max(axis=0)))])
    )
    # Polygons, not a subdivision cage: subdividing would shrink the proxy off its source.
    mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    mesh.CreatePurposeAttr().Set(purpose)
    if proxy.colours is not None:
        colour = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
            "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex
        )
        colour.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(proxy.colours, dtype=np.float32)))


def execute(node: chunk.Chunk) -> None:
    """Rebuild the proxy of the picked prim(s)."""
    stage = node.get_stage()
    layer = node.get_edit_context()
    if not layer:
        return

    if not _HAS_DEPS:
        _report(node, None, "needs scipy and scikit-image — pip install scipy scikit-image")
        return

    paths = node.get_param("prim_path").resolve_paths()
    if not paths:
        _report(node, None, "pick an asset to remesh")
        return

    settings = _settings(node)
    upstream, sources = _sources(node, paths)
    stamp = fingerprint(sources, settings)
    previous = _LAST_COOK.get(layer.identifier)
    if previous is not None and previous[0] == stamp:
        proxy = previous[1]
    else:
        proxy = build_proxy(sources, settings)
    del upstream  # the sources are numpy now; the scratch stage can go

    out_path = _output_path(node, str(paths[0]))
    if proxy is not None:
        with Usd.EditContext(stage, layer):
            _author(stage, out_path, proxy, node.get_param("purpose", chunk.is_param_t(str)).value)

    if len(_LAST_COOK) > 64:
        _LAST_COOK.clear()  # deleted chunks leave entries; a proxy can be large
    _LAST_COOK[layer.identifier] = (stamp, proxy)
    node.get_param("out_mesh").value = out_path if proxy is not None else ""
    _report(node, proxy, "" if sources else "no meshes under that prim")


_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def main():
    chunk_api = chunk.Chunk("Remesh Proxy")

    chunk_api.add_param(
        chunk.LineEdit(
            "prim_path",
            "Asset",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="The asset to build a proxy of. Every visible mesh underneath is included,\n"
            "instanced and PointInstancer copies too.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "out_path",
            "Proxy Path",
            flags=_PF.LABELED,
            tooltip="Where the proxy mesh is written. Empty: beside the asset, as <asset>_proxy.",
        )
    )
    chunk_api.add_param(
        chunk.Label(
            "readout",
            "not remeshed yet",
            default_value="not remeshed yet",
            tooltip="What the last cook produced.",
        )
    )

    chunk_api.add_param(
        chunk.Slider(
            "resolution",
            "Resolution",
            is_int=True,
            min_value=16,
            max_value=384,
            default_value=128,
            tooltip="Voxels along the asset's longest side. Sets the finest detail the proxy can\n"
            "hold, and the scale every size below is measured in. Memory and time grow with\n"
            "its cube: 384 is several GB.",
        )
    )

    shape = chunk.Group("Shape", flags=_GF)
    shape.add_params(
        [
            chunk.Slider(
                "thickness",
                "Thickness",
                min_value=0.5,
                max_value=8.0,
                default_value=1.0,
                tooltip="How far the proxy stands off the surface, in voxels. Open surfaces —\n"
                "leaf cards, sheet metal — become shells this thick. Below about 1 a thin\n"
                "card can break up.",
            ),
            chunk.Slider(
                "close",
                "Close Gaps",
                min_value=0.0,
                max_value=32.0,
                default_value=4.0,
                tooltip="Bridges gaps up to about twice this many voxels wide without growing\n"
                "the outline. Raise it until a canopy reads as one mass; 0 keeps every gap.",
            ),
            chunk.Checkbox(
                "fill_interior",
                "Fill Interior",
                default_value=True,
                tooltip="Make every enclosed cavity solid. Deletes the hidden internals of a CAD\n"
                "assembly and stops a closed shell from coming out double-walled.",
            ),
            chunk.Slider(
                "smooth",
                "Smooth",
                min_value=0.0,
                max_value=3.0,
                default_value=0.5,
                tooltip="Blur the volume before extracting the surface, in voxels. Takes out the\n"
                "voxel staircase; high values round off corners and thin parts.",
            ),
        ]
    )
    chunk_api.add_param(shape)

    look = chunk.Group("Output", flags=_GF)
    look.add_params(
        [
            chunk.Checkbox(
                "bake_colour",
                "Bake Colour",
                default_value=True,
                tooltip="Bake the asset's colour onto the proxy as vertex displayColor: from its\n"
                "materials' diffuse / base colour (and their textures), else its displayColor.",
            ),
            chunk.Menu(
                "purpose",
                "Purpose",
                menu_items=_PURPOSES,
                default_value=PURPOSE_DEFAULT,
                tooltip="The proxy mesh's purpose. 'proxy' shows it in viewports that draw proxies\n"
                "and keeps it out of final renders; pair it with purpose=render on the asset.",
            ),
        ]
    )
    chunk_api.add_param(look)

    chunk_api.add_param(
        chunk.LineEdit(
            "out_mesh",
            "Proxy",
            flags=_PF.OUTPUT | _PF.LABELED,
            tooltip="The proxy mesh. Feed it to Poly Reduce for the triangle budget.",
        )
    )
    out_triangles = chunk.Slider(
        "out_triangles",
        "Triangles Out",
        is_int=True,
        max_value=100_000_000,
        flags=_PF.OUTPUT,
        tooltip="Read-only. Triangles in the proxy, before any reduction.",
    )
    out_triangles.set_state(chunk.ChunkFlags.State.DISABLED)
    chunk_api.add_param(out_triangles)

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        requirements=[
            "numpy>=2,<3",
            "scipy>=1.11,<2",
            "scikit-image>=0.22,<1",
            "lazy_loader>=0.4",
            "pillow>=10",
        ],
        cache_cook=True,
        description=(
            "Rebuilds an asset as one closed, coloured proxy mesh by way of a volume: the\n"
            "surface is sampled, turned into a distance field, gaps are closed and enclosed\n"
            "cavities filled, and a new surface is extracted with marching cubes.\n\n"
            "For the assets decimation can't reduce: a tree's leaf cards, a CAD assembly's\n"
            "thousands of parts and hidden internals. The asset's colour — material diffuse,\n"
            "textures, displayColor — is baked onto the proxy's vertices. Follow it with\n"
            "Poly Reduce to hit a triangle budget."
        ),
    )
