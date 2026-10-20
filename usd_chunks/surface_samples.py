"""Surface samples of an asset: points over its render geometry, with the colour it shows.

Shared by the chunks that need to know what an asset looks like at a point on its surface
-- Remesh Proxy (vertex colours on a stand-in) and Bake Textures (texture maps on one).
What is read:

* **Geometry**: every visible, non-guide, non-proxy mesh under a prim, instance proxies
  included, and every PointInstancer copy -- a prototype is read once and kept with all of
  its placements, so a scattered canopy costs one read.
* **Colour**: the bound material's diffuse / base colour, per GeomSubset; else
  `displayColor`. A MaterialX network (standard_surface, open_pbr_surface) is evaluated
  node by node (`mtlx_eval`) -- the hue/saturation, tint and mix nodes an exporter puts
  after the texture are the look -- and the UsdPreviewSurface beside it, a simplified
  approximation, is only the fallback for a network using nodes `mtlx_eval` can't run.
  Roughness, metalness and opacity are read the same way.
* **Cut-outs**: an alpha-tested material drops the samples that land on transparent
  texels, or a leaf card's cut-away part would count as surface.
* **Normals**: the mesh's authored normals when it has them, else its face normals.

Needs numpy, which the framework itself does not, and Pillow for textures (without it a
textured material falls back to its constant colour): only chunks that declare them
import this module.
"""

from __future__ import annotations

import logging
import os
import zlib
from typing import NamedTuple

import numpy as np
from pxr import Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import mtlx_eval
from usd_chunks.mesh_math import fan_triangulate

try:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # 16k texture sheets are normal, not a decompression bomb
except ImportError:  # pragma: no cover - textures then fall back to the material's colour
    Image = None

logger = logging.getLogger(__name__)

# The colour a surface gets when nothing on it says otherwise -- USD's own fallback grey.
_FALLBACK_COLOUR = (0.18, 0.18, 0.18)

# Inputs a surface shader's colour can come from: UsdPreviewSurface, then the MaterialX
# surfaces (standard_surface, open_pbr_surface).
_COLOUR_INPUTS = ("diffuseColor", "base_color")
_OPACITY_INPUTS = ("opacity", "geometry_opacity")
_TRANSMISSION_INPUTS = ("transmission_weight", "transmission")
# Transmission rougher than this is frosted glass: seen as a surface, not through.
_FROSTED_ROUGHNESS = 0.2
# Roughness and metalness, UsdPreviewSurface name first, then OpenPBR / standard_surface.
_ROUGHNESS_INPUTS = ("roughness", "specular_roughness")
_METALLIC_INPUTS = ("metallic", "base_metalness", "metalness")
# UsdPreviewSurface's own defaults, for a material that authors neither.
_DEFAULT_ROUGHNESS = 0.5
_DEFAULT_METALLIC = 0.0


#: customData on a proxy prim that *is* its source -- a reference to it, kept as is because
#: the source already had fewer triangles than the proxy was meant to. Poly Reduce, UV
#: Unwrap and Bake Textures leave such a prim alone: re-UVing a painting that already has
#: good UVs and bakes would only make it worse.
PASSTHROUGH_KEY = "usdChunks:proxyIsSource"


def is_passthrough(prim) -> bool:
    """Whether `prim`, or a prim above it, is a proxy kept as its own source."""
    while prim and prim.IsValid() and not prim.IsPseudoRoot():
        if prim.GetCustomDataByKey(PASSTHROUGH_KEY):
            return True
        prim = prim.GetParent()
    return False


# ---------------------------------------------------------------------------
# array maths -- no USD
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


def srgb_to_linear(values):
    values = np.asarray(values, dtype=np.float32)
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


# ---------------------------------------------------------------------------
# reading the asset: geometry and the colour it shows
# ---------------------------------------------------------------------------


class Texture(NamedTuple):
    """One texture lookup: a file, the channel(s) to read, how to decode them."""

    path: str
    channels: str  # "rgb", or one of "r", "g", "b", "a" for a scalar read
    linearize: bool
    st: str  # the primvar the lookup reads its coordinates from
    # A UsdTransform2d between the primvar and the lookup: (scale u, scale v, rotation in
    # degrees, translate u, translate v). Fabrics tile this way -- 6x, 8x -- and without it
    # one stretched copy of the weave covers the whole chair.
    transform: tuple | None = None


class _Look(NamedTuple):
    """What a material shows, as far as a proxy cares: a colour, maybe from a texture,
    and maybe an alpha cut-out."""

    colour: tuple
    colour_texture: Texture | None
    cutout: Texture | None
    threshold: float
    # Glass and other surfaces mostly seen *through*: left out of the samples altogether,
    # or a turntable's dust cover becomes an opaque slab hiding the platter.
    see_through: bool = False
    roughness: float = _DEFAULT_ROUGHNESS
    roughness_texture: Texture | None = None
    metallic: float = _DEFAULT_METALLIC
    metallic_texture: Texture | None = None
    # Compiled MaterialX inputs, by channel ("colour", "roughness", "metallic", "opacity"):
    # where present they decide that channel, over any constant or texture above.
    programs: dict = {}


class Textured(NamedTuple):
    """The triangles of one source that read a texture, and the coordinates they read."""

    tris: np.ndarray  # (k,) triangle indices into the source
    corner_st: np.ndarray  # (k, 3, 2)
    colour: Texture | None
    cutout: Texture | None
    threshold: float
    roughness: Texture | None = None
    metallic: Texture | None = None
    programs: dict = {}  # channel -> mtlx_eval.Program
    corner_primvars: dict = {}  # primvar name -> (k, 3, 2) values the programs read


class Source(NamedTuple):
    """One mesh's triangles in its own space, how they are coloured, and every placement
    of it in the world -- one for a plain mesh, one per instance under a PointInstancer."""

    path: str
    points: np.ndarray  # (n, 3) float64, local
    tris: np.ndarray  # (m, 3) int64
    corner_colours: np.ndarray  # (m, 3, 3) float32
    corner_normals: np.ndarray  # (m, 3, 3) float32, local, unit length
    tri_roughness: np.ndarray  # (m,) float32, the material's constant; textures override
    tri_metallic: np.ndarray  # (m,) float32
    textured: list  # [Textured]
    placements: np.ndarray  # (i, 4, 4) float64 local-to-world, row-vector convention


def _connected_shader(shader_input):
    """`(shader, output name)` feeding an input, or (None, None)."""
    if not shader_input or not shader_input.HasConnectedSource():
        return None, None
    sources, _invalid = shader_input.GetConnectedSources()
    if not sources:
        return None, None
    return UsdShade.Shader(sources[0].source.GetPrim()), str(sources[0].sourceName)


def _texture_of(shader, output_name, scalar) -> Texture | None:
    """The file a texture node reads, if `shader` is one (UsdUVTexture, MaterialX image)."""
    file_input = shader.GetInput("file") if shader else None
    asset = file_input.Get() if file_input else None
    path = (asset.resolvedPath or asset.path) if asset else ""
    if not path or "<UDIM>" in path:
        return None

    space = shader.GetInput("sourceColorSpace")
    space = str(space.Get()) if space and space.Get() else "auto"

    st, transform = "st", None
    reader, _ = _connected_shader(shader.GetInput("st") or shader.GetInput("texcoord"))
    if reader and str(reader.GetIdAttr().Get() or "") == "UsdTransform2d":
        transform = _uv_transform(reader)
        reader, _ = _connected_shader(reader.GetInput("in"))
    if reader and (varname := reader.GetInput("varname") or reader.GetInput("geomprop")):
        st = str(varname.Get() or "st")

    if scalar:
        channels = output_name if output_name in ("r", "g", "b", "a") else "r"
    else:
        channels = "rgb"
    return Texture(path, channels, linearize=space != "raw" and channels == "rgb", st=st, transform=transform)


def _uv_transform(shader) -> tuple | None:
    """A UsdTransform2d's (scale u, scale v, rotation, translate u, translate v), or None if
    it is the identity."""

    def value(name, default):
        shader_input = shader.GetInput(name)
        got = shader_input.Get() if shader_input else None
        return default if got is None else got

    scale = value("scale", (1.0, 1.0))
    rotation = float(value("rotation", 0.0))
    translation = value("translation", (0.0, 0.0))
    transform = (float(scale[0]), float(scale[1]), rotation, float(translation[0]), float(translation[1]))
    return None if transform == (1.0, 1.0, 0.0, 0.0, 0.0) else transform


def apply_uv_transform(st, transform):
    """UsdTransform2d's order: scale, then rotate (degrees, counter-clockwise), then translate."""
    if transform is None:
        return st
    su, sv, degrees, tu, tv = transform
    scaled = st * np.array([su, sv])
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    rotated = np.stack([scaled[:, 0] * cos - scaled[:, 1] * sin, scaled[:, 0] * sin + scaled[:, 1] * cos], axis=1)
    return rotated + np.array([tu, tv])


# The MaterialX inputs each channel is read from, in order of preference.
_PROGRAM_INPUTS = {
    "colour": ("base_color",),
    "roughness": ("specular_roughness",),
    "metallic": ("base_metalness", "metalness"),
    "opacity": ("geometry_opacity", "opacity"),
}


def _look(material) -> _Look | None:
    """What a bound material shows, or None if its surface isn't one this chunk reads."""
    if not material:
        return None
    preview = material.ComputeSurfaceSource("")[0]
    mtlx = material.ComputeSurfaceSource("mtlx")[0]
    if mtlx and not str(mtlx.GetIdAttr().Get() or "").startswith("ND_"):
        mtlx = None
    if not (preview or mtlx):
        return None
    look = _surface_look(preview or mtlx)
    if mtlx is None:
        return look

    programs, cache = {}, {}
    for channel, names in _PROGRAM_INPUTS.items():
        shader_input = next((i for n in names if (i := mtlx.GetInput(n))), None)
        if shader_input is None or not shader_input.HasConnectedSource():
            continue
        try:
            programs[channel] = mtlx_eval.compile_input(shader_input, cache)
        except mtlx_eval.Unsupported as exc:
            logger.debug("%s %s: %s -- using the preview surface", material.GetPath(), channel, exc)
    constants = {}
    if preview is not None:
        # Unconnected MaterialX values outrank the preview's, which only approximate them.
        for channel, field in (("colour", "colour"), ("roughness", "roughness"), ("metallic", "metallic")):
            shader_input = next((i for n in _PROGRAM_INPUTS[channel] if (i := mtlx.GetInput(n))), None)
            if shader_input is not None and not shader_input.HasConnectedSource() and shader_input.Get() is not None:
                value = shader_input.Get()
                constants[field] = tuple(value)[:3] if field == "colour" else float(value)
    see_through = look.see_through or _see_through(mtlx, constants.get("roughness", look.roughness))
    return look._replace(programs=programs, see_through=see_through, **constants)


def _surface_look(shader) -> _Look:
    """The look of one surface shader from its own inputs: constants and direct textures."""

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

    roughness, roughness_texture = _scalar(shader, _ROUGHNESS_INPUTS, _DEFAULT_ROUGHNESS)
    metallic, metallic_texture = _scalar(shader, _METALLIC_INPUTS, _DEFAULT_METALLIC)
    if colour is None and _transmission(shader) >= 0.5:
        # Frosted glass with no base colour of its own looks the colour it lets through.
        tint = shader.GetInput("transmission_color")
        value = tint.Get() if tint and not tint.HasConnectedSource() else None
        colour = tuple(value)[:3] if value is not None else (1.0, 1.0, 1.0)
    return _Look(
        colour or _FALLBACK_COLOUR,
        colour_texture,
        cutout,
        threshold or 0.5,
        _see_through(shader, roughness),
        roughness,
        roughness_texture,
        metallic,
        metallic_texture,
    )


def _scalar(shader, names, default):
    """`(constant, texture)` for the first of `names` the shader has. A texture's own
    `fallback` stands in as the constant, for whatever can't read the texture."""
    for name in names:
        if not (shader_input := shader.GetInput(name)):
            continue
        source, output = _connected_shader(shader_input)
        if source:
            fallback = source.GetInput("fallback")
            value = fallback.Get() if fallback else None
            constant = float(np.asarray(value).reshape(-1)[0]) if value is not None else default
            return constant, _texture_of(source, output, scalar=True)
        value = shader_input.Get()
        return (float(value) if value is not None else default), None
    return default, None


def _transmission(shader) -> float:
    for name in _TRANSMISSION_INPUTS:
        shader_input = shader.GetInput(name)
        if shader_input and not shader_input.HasConnectedSource():
            return float(shader_input.Get() or 0.0)
    return 0.0


def _see_through(shader, roughness: float) -> bool:
    """Whether a surface is mostly looked through: *clear* transmission (OpenPBR,
    standard_surface) or a constant opacity below one half (UsdPreviewSurface). A *textured*
    opacity is a cut-out, handled per sample instead.

    Rough transmission is frosted or milky glass -- a lamp shade, a bulb -- which reads as
    a solid, pale surface from any distance a proxy is seen at; dropping it left a pendant
    lamp as a bare cord. Clear glass (a dust cover, a window) is what disappears.
    """
    if _transmission(shader) >= 0.5 and roughness < _FROSTED_ROUGHNESS:
        return True
    for name in _OPACITY_INPUTS:
        shader_input = shader.GetInput(name)
        value = shader_input.Get() if shader_input and not shader_input.HasConnectedSource() else None
        if value is not None and float(np.mean(value)) < 0.5:
            return True
    return False


def per_corner(values, interpolation, tris, corners, face_of_tri):
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
    return per_corner(values, primvar.GetInterpolation(), tris, corners, face_of_tri)


def _bound_material(prim):
    """The material bound to `prim` (or a subset), looked up through the stage by path.

    ComputeBoundMaterial resolves the binding in C++, out of sight of the stage lookups
    skip-cooking watches to learn what a cook reads. On a cook stage masked to the asset, a
    material in a shared /Looks scope is simply absent and the binding comes back empty --
    the bake goes grey and nothing says why. Asking for the binding's target by path is a
    lookup it does see: the mask widens to the material and the cook runs again with it.
    """
    material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
    stage = prim.GetStage()
    if material:
        stage.GetPrimAtPath(material.GetPath())
        return material
    walk = prim
    while walk and not walk.IsPseudoRoot():
        target = UsdShade.MaterialBindingAPI(walk).GetDirectBinding().GetMaterialPath()
        if not target.isEmpty:
            stage.GetPrimAtPath(target)  # absent here: let the cook see it asked
            break
        walk = walk.GetParent()
    return material


def _face_looks(prim, n_faces, looks: dict):
    """`(look per face code, code per face)` from the prim's binding and its subsets'."""
    whole = _bound_material(prim)
    codes = np.zeros(n_faces, dtype=np.int64)
    materials = [whole]
    for subset in UsdShade.MaterialBindingAPI(prim).GetMaterialBindSubsets():
        material = _bound_material(subset.GetPrim())
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


def _read_mesh(prim, placements, looks: dict) -> Source | None:
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

    face_looks, codes = _face_looks(prim, len(counts), looks)
    see_through = [code for code, look in enumerate(face_looks) if look is not None and look.see_through]
    if see_through:
        solid = ~np.isin(codes[face_of_tri], see_through)
        tris, corners, face_of_tri = tris[solid], corners[solid], face_of_tri[solid]
        if len(tris) == 0:
            return None
    tri_codes = codes[face_of_tri]

    display = _primvar_corners(prim, "displayColor", tris, corners, face_of_tri)
    if display is None:
        display = np.broadcast_to(np.asarray(_FALLBACK_COLOUR), (len(tris), 3, 3))
    corner_colours = np.array(display, dtype=np.float32)
    tri_roughness = np.full(len(tris), _DEFAULT_ROUGHNESS, dtype=np.float32)
    tri_metallic = np.full(len(tris), _DEFAULT_METALLIC, dtype=np.float32)

    textured = []
    for code, look in enumerate(face_looks):
        if look is None:
            continue  # no material, or one this chunk can't read: displayColor stands
        mask = tri_codes == code
        if not mask.any():
            continue
        corner_colours[mask] = look.colour
        tri_roughness[mask] = look.roughness
        tri_metallic[mask] = look.metallic
        lookups = [t for t in (look.colour_texture, look.cutout, look.roughness_texture, look.metallic_texture) if t]
        if not lookups and not look.programs:
            continue
        which = np.flatnonzero(mask)
        corner_primvars = {}
        for name in set().union(*(p.primvars for p in look.programs.values())) if look.programs else ():
            values = _primvar_corners(prim, name, tris, corners, face_of_tri)
            if values is not None and np.asarray(values).shape[-1:] == (2,):
                corner_primvars[name] = np.asarray(values, dtype=np.float64)[which]
        st = _primvar_corners(prim, lookups[0].st, tris, corners, face_of_tri) if lookups else None
        if st is None or np.asarray(st).shape[-1:] != (2,):
            if not look.programs:
                continue
            st = np.zeros((len(tris), 3, 2))
        textured.append(
            Textured(
                which,
                np.asarray(st, dtype=np.float64)[which],
                look.colour_texture,
                look.cutout,
                look.threshold,
                look.roughness_texture,
                look.metallic_texture,
                look.programs,
                corner_primvars,
            )
        )

    normals = read_corner_normals(prim, points, tris, corners, face_of_tri)
    return Source(
        str(prim.GetPath()), points, tris, corner_colours, normals, tri_roughness, tri_metallic, textured, placements
    )


def face_normals(points, tris) -> np.ndarray:
    """Unit normals of a triangle soup, counter-clockwise; zero for a degenerate triangle."""
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    normals = np.cross(b - a, c - a)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return np.divide(normals, length, out=np.zeros_like(normals), where=length > 0.0)


def read_corner_normals(prim, points, tris, corners, face_of_tri) -> np.ndarray:
    """The normal at each triangle corner: authored if the mesh has them, else flat.

    Authored normals are what the asset was meant to shade with -- a CAD part's smooth
    fillet next to a hard edge, which face normals would facet and averaged ones would
    round off.
    """
    mesh = UsdGeom.Mesh(prim)
    normals = _primvar_corners(prim, "normals", tris, corners, face_of_tri)
    if normals is None and (values := mesh.GetNormalsAttr().Get()):
        normals = per_corner(values, mesh.GetNormalsInterpolation(), tris, corners, face_of_tri)
    if normals is not None and np.asarray(normals).shape[-1:] == (3,):
        normals = np.asarray(normals, dtype=np.float64)
        length = np.linalg.norm(normals, axis=-1, keepdims=True)
        normals = np.divide(normals, length, out=np.zeros_like(normals), where=length > 0.0)
        return normals.astype(np.float32)
    flat = face_normals(points, tris)
    if mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded:
        flat = -flat
    return np.repeat(flat[:, None], 3, axis=1).astype(np.float32)


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


def gather_sources(root, exclude=()) -> list:
    """Every mesh under `root` worth sampling, with its placements in the world.

    `exclude` is prim paths left out with everything under them -- a bake target that sits
    inside the asset it is baked from, which would otherwise be sampled as part of it.
    """
    if not root or not root.IsValid():
        return []
    excluded = {Sdf.Path(str(path)) for path in exclude}
    xcache = UsdGeom.XformCache()
    looks: dict = {}
    sources: list = []
    prims = iter(Usd.PrimRange(root, Usd.TraverseInstanceProxies()))
    for prim in prims:
        if _skipped(prim) or prim.GetPath() in excluded:
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


def bounds(sources):
    """World `(lo, hi)` over every placement of every source."""
    corners = []
    for source in sources:
        lo, hi = source.points.min(axis=0), source.points.max(axis=0)
        box = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        placed = np.einsum("bj,ijk->ibk", box, source.placements[:, :3, :3]) + source.placements[:, None, 3, :3]
        corners.append(placed.reshape(-1, 3))
    corners = np.concatenate(corners)
    return corners.min(axis=0), corners.max(axis=0)


def placement_scale(placements) -> float:
    """A representative uniform scale for a set of placements -- the median, so one
    oddly-sized instance doesn't set the sampling density for the other ten thousand."""
    det = np.abs(np.linalg.det(placements[:, :3, :3]))
    return float(np.cbrt(np.median(det))) or 1.0


def surface_area(sources) -> float:
    return sum(
        triangle_areas(s.points, s.tris).sum() * placement_scale(s.placements) ** 2 * len(s.placements) for s in sources
    )


class Images:
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

    def texels(self, path, st, wrap_u="periodic", wrap_v="periodic"):
        """Raw RGBA (n, 4) in 0-1 at each st, nearest texel, or None if unreadable."""
        image = self.get(path)
        if image is None:
            return None
        height, width = image.shape[:2]
        u = np.mod(st[:, 0], 1.0) if wrap_u == "periodic" else np.clip(st[:, 0], 0.0, 1.0)
        v = np.mod(st[:, 1], 1.0) if wrap_v == "periodic" else np.clip(st[:, 1], 0.0, 1.0)
        rows = np.clip(((1.0 - v) * height).astype(np.int64), 0, height - 1)
        cols = np.clip((u * width).astype(np.int64), 0, width - 1)
        return image[rows, cols].astype(np.float32) / 255.0

    def sample(self, texture: Texture, st):
        """`texture` at each st, nearest texel, repeat-wrapped: (n, 3) or (n,), or None."""
        image = self.get(texture.path)
        if image is None:
            return None
        height, width = image.shape[:2]
        st = apply_uv_transform(st, texture.transform)
        u = np.mod(st[:, 0], 1.0)
        v = np.mod(st[:, 1], 1.0)
        rows = np.clip(((1.0 - v) * height).astype(np.int64), 0, height - 1)
        cols = np.clip((u * width).astype(np.int64), 0, width - 1)
        texels = image[rows, cols].astype(np.float32) / 255.0
        if texture.channels == "rgb":
            rgb = texels[:, :3]
            return srgb_to_linear(rgb) if texture.linearize else rgb
        return texels[:, "rgba".index(texture.channels)]


class Samples(NamedTuple):
    """Points on an asset's surface, in world space, with what the asset shows there."""

    points: np.ndarray  # (s, 3) float64
    colours: np.ndarray  # (s, 3) float32, linear
    normals: np.ndarray  # (s, 3) float32, unit length
    roughness: np.ndarray  # (s,) float32
    metallic: np.ndarray  # (s,) float32
    # On a cut-out material's transparent part. Only present when asked for (`keep_cut`):
    # an opacity bake needs to know where the surface is *not*, everything else must not
    # see these at all -- a leaf texture's transparent texels hold junk colour.
    cut: np.ndarray  # (s,) bool


def _sample_source(source: Source, spacing, rng, images: Images, keep_cut: bool, check) -> Samples:
    """World-space samples of one source, with what its material shows at each."""
    local_spacing = spacing / placement_scale(source.placements)
    tri_index, bary = sample_triangles(source.points, source.tris, local_spacing, rng)
    corners = source.points[source.tris[tri_index]]  # (s, 3, 3)
    local = np.einsum("sk,skc->sc", bary, corners)
    colours = np.einsum("sk,skc->sc", bary, source.corner_colours[tri_index])
    normals = np.einsum("sk,skc->sc", bary, source.corner_normals[tri_index])
    roughness = source.tri_roughness[tri_index].copy()
    metallic = source.tri_metallic[tri_index].copy()
    cut = np.zeros(len(tri_index), dtype=bool)

    for part in source.textured:
        check()
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
        if part.roughness and (value := images.sample(part.roughness, st)) is not None:
            roughness[hits] = value
        if part.metallic and (value := images.sample(part.metallic, st)) is not None:
            metallic[hits] = value
        if part.cutout and (alpha := images.sample(part.cutout, st)) is not None:
            cut[hits[alpha < part.threshold]] = True
        if part.programs:
            check()
            primvars = {
                name: np.einsum("sk,skc->sc", bary[hits], values[row[hits]])
                for name, values in part.corner_primvars.items()
            }
            _run_programs(part.programs, primvars, images, hits, colours, roughness, metallic, cut)

    keep = slice(None) if keep_cut else ~cut
    local, colours, normals = local[keep], colours[keep], normals[keep]
    roughness, metallic, cut = roughness[keep], metallic[keep], cut[keep]
    placements = source.placements
    count = len(placements)
    world = np.einsum("sc,icd->isd", local, placements[:, :3, :3]) + placements[:, None, 3, :3]
    # Normals transform by the inverse transpose -- row vectors, so n @ inv(M).T -- which
    # is what keeps them perpendicular under a non-uniform scale.
    to_normal = np.transpose(np.linalg.inv(placements[:, :3, :3]), (0, 2, 1))
    world_normals = np.einsum("sc,icd->isd", normals, to_normal).reshape(-1, 3)
    length = np.linalg.norm(world_normals, axis=1, keepdims=True)
    world_normals = np.divide(world_normals, length, out=np.zeros_like(world_normals), where=length > 0.0)

    def tiled(values):
        return np.broadcast_to(values, (count,) + values.shape).reshape((-1,) + values.shape[1:])

    return Samples(
        world.reshape(-1, 3),
        tiled(colours),
        world_normals.astype(np.float32),
        tiled(roughness),
        tiled(metallic),
        tiled(cut),
    )


def _run_programs(programs, primvars, images, hits, colours, roughness, metallic, cut) -> None:
    """Evaluate a material's MaterialX channels at `hits`, writing over what textures gave."""
    n = len(hits)
    try:
        if program := programs.get("colour"):
            colours[hits] = np.clip(program.run(primvars, images, n)[:, :3], 0.0, None)
        if program := programs.get("roughness"):
            roughness[hits] = np.clip(program.run(primvars, images, n)[:, 0], 0.0, 1.0)
        if program := programs.get("metallic"):
            metallic[hits] = np.clip(program.run(primvars, images, n)[:, 0], 0.0, 1.0)
        if program := programs.get("opacity"):
            # MaterialX opacity is blended, not tested; half coverage is where it reads as gone.
            cut[hits[program.run(primvars, images, n)[:, 0] < 0.5]] = True
    except Exception as exc:  # a network that compiles but can't run costs its detail, not the cook
        logger.warning("MaterialX evaluation failed: %s", exc)


def sample_sources(sources, spacing, keep_cut: bool = False, check=None, progress=None) -> Samples:
    """Every source's world-space samples, concatenated; `spacing` is the world distance
    between neighbouring samples on a large triangle. Cut-out samples are dropped unless
    `keep_cut`, and then flagged in `Samples.cut`. `check` is called between the parts --
    each source, each textured part, before its MaterialX programs -- to raise and stop it
    (sim_bake.Context.check): a 7M-triangle tree samples for eleven seconds. `progress` is
    called after each source with the share of the triangles sampled so far, 0 to 1."""
    images = Images()
    rng = np.random.default_rng(0)  # the same samples every cook, not a shimmering result
    check = check or (lambda: None)
    sizes = [len(source.tris) * len(source.placements) for source in sources]
    total, done = max(1, sum(sizes)), 0
    parts = []
    for source, size in zip(sources, sizes):
        check()
        parts.append(_sample_source(source, spacing, rng, images, keep_cut, check))
        done += size
        if progress is not None:
            progress(done / total)
    if not parts:
        empty3 = np.zeros((0, 3), dtype=np.float32)
        empty = np.zeros(0, dtype=np.float32)
        return Samples(np.zeros((0, 3)), empty3, empty3, empty, empty, np.zeros(0, dtype=bool))
    return Samples(
        np.concatenate([p.points for p in parts]),
        np.concatenate([p.colours for p in parts]).astype(np.float32),
        np.concatenate([p.normals for p in parts]).astype(np.float32),
        np.concatenate([p.roughness for p in parts]).astype(np.float32),
        np.concatenate([p.metallic for p in parts]).astype(np.float32),
        np.concatenate([p.cut for p in parts]),
    )


# ---------------------------------------------------------------------------
# skip-cook: a cheap signature of everything a sampling depends on
# ---------------------------------------------------------------------------


def crc(array) -> tuple:
    """Shape, dtype and CRC of an array: equal for equal contents, cheap to compare."""
    array = np.ascontiguousarray(array)
    return array.shape, str(array.dtype), zlib.crc32(array)


def _texture_stamp(texture: Texture | None):
    if texture is None:
        return None
    try:
        mtime = os.path.getmtime(texture.path)
    except OSError:
        mtime = None
    return texture + (mtime,)


def fingerprint(sources) -> tuple:
    """Everything sampling `sources` depends on -- geometry, material values, placements,
    texture files (by modification time). Reading the sources is cheap next to what a chunk
    builds from them, so a recook that changed nothing upstream costs only the read."""
    parts = []
    for s in sources:
        textures = tuple(
            (
                crc(t.tris),
                crc(t.corner_st),
                _texture_stamp(t.colour),
                _texture_stamp(t.cutout),
                t.threshold,
                _texture_stamp(t.roughness),
                _texture_stamp(t.metallic),
                tuple(sorted((name, crc(values)) for name, values in t.corner_primvars.items())),
            )
            for t in s.textured
        )
        parts.append(
            (
                s.path,
                crc(s.points),
                crc(s.tris),
                crc(s.corner_colours),
                crc(s.corner_normals),
                crc(s.tri_roughness),
                crc(s.tri_metallic),
                crc(s.placements),
                textures,
            )
        )
    return tuple(parts)
