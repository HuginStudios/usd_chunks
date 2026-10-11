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
from typing import NamedTuple

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk
from usd_chunks import chunk_stage
from usd_chunks import surface_samples

try:
    from scipy import ndimage
    from scipy.spatial import cKDTree
    from skimage.measure import marching_cubes

    _HAS_DEPS = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheels
    ndimage = cKDTree = marching_cubes = None
    _HAS_DEPS = False

logger = logging.getLogger(__name__)

PURPOSE_DEFAULT = "default"
PURPOSE_RENDER = "render"
PURPOSE_PROXY = "proxy"
_PURPOSES = [PURPOSE_DEFAULT, PURPOSE_RENDER, PURPOSE_PROXY]

# Samples per voxel edge. Two keeps every voxel a surface passes through occupied, which
# is what stops a thin card from coming out as a dotted line of blobs.
_SAMPLES_PER_VOXEL = 2.0

# The most surface samples one cook will take. A 4M-triangle tree at resolution 384 asks
# for less than this; past it the spacing is widened rather than running out of memory.
_MAX_SAMPLES = 30_000_000

# Nearest samples averaged into one baked vertex colour.
_BAKE_NEIGHBOURS = 8



# ---------------------------------------------------------------------------
# array maths -- no USD, unit-testable on its own
# ---------------------------------------------------------------------------



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
    lo, hi = surface_samples.bounds(sources)
    voxel = float(np.max(hi - lo)) / max(1, settings.resolution)
    if voxel <= 0.0:
        return None

    spacing = voxel / _SAMPLES_PER_VOXEL
    expected = surface_samples.surface_area(sources) / (spacing * spacing)
    if expected > _MAX_SAMPLES:
        spacing *= float(np.sqrt(expected / _MAX_SAMPLES))
    samples = surface_samples.sample_sources(sources, spacing)
    if not len(samples.points):
        return None

    field = build_field(samples.points, voxel, settings.thickness, settings.close, settings.fill, settings.smooth)
    surface = extract_surface(field)
    if surface is None:
        return None
    points, triangles = surface
    colours = bake_colours(points, samples.points, samples.colours) if settings.bake else None
    source_triangles = sum(len(s.tris) * len(s.placements) for s in sources)
    return Proxy(points, triangles, colours, source_triangles, voxel)


# ---------------------------------------------------------------------------
# skip-cook: same sources, same settings, same proxy
# ---------------------------------------------------------------------------


def fingerprint(sources, settings: _Settings) -> tuple:
    """Everything the proxy depends on. Reading the sources is cheap next to building one
    (a few hundred ms against several seconds), so a recook that changed nothing upstream
    -- every upstream cook triggers one -- costs only the read."""
    return (tuple(settings),) + surface_samples.fingerprint(sources)


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
    return upstream, [
        source for path in paths for source in surface_samples.gather_sources(upstream.GetPrimAtPath(path))
    ]


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
        to_parent = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(parent).GetInverse(), dtype=np.float64)
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
    out_path = _output_path(node, str(paths[0]))
    source_triangles = sum(len(s.tris) * len(s.placements) for s in sources)
    keep_under = int(node.get_param("keep_under", chunk.is_param_t(int)).value)
    if sources and source_triangles <= keep_under and not Sdf.Path(out_path).HasPrefix(Sdf.Path(str(paths[0]))):
        del upstream
        _keep_source(node, stage, layer, str(paths[0]), out_path, source_triangles, keep_under)
        return
    stamp = fingerprint(sources, settings)
    previous = _LAST_COOK.get(layer.identifier)
    if previous is not None and previous[0] == stamp:
        proxy = previous[1]
    else:
        proxy = build_proxy(sources, settings)
    del upstream  # the sources are numpy now; the scratch stage can go

    if proxy is not None:
        with Usd.EditContext(stage, layer):
            _author(stage, out_path, proxy, node.get_param("purpose", chunk.is_param_t(str)).value)

    if len(_LAST_COOK) > 64:
        _LAST_COOK.clear()  # deleted chunks leave entries; a proxy can be large
    _LAST_COOK[layer.identifier] = (stamp, proxy)
    node.get_param("out_mesh").value = out_path if proxy is not None else ""
    _report(node, proxy, "" if sources else "no meshes under that prim")


def _keep_source(node, stage, layer, source_path, out_path, triangles, keep_under) -> None:
    """The proxy is the source itself: a reference to it, marked so the chain leaves it be.

    A 1,500-triangle painting remeshed to a 3,000-triangle proxy is bigger and worse than
    the painting. Below the budget the source already *is* the proxy, with its own UVs,
    materials and textures, which no bake would improve on.
    """
    with Usd.EditContext(stage, layer):
        prim = stage.DefinePrim(out_path)
        prim.GetReferences().AddInternalReference(source_path)
        prim.SetCustomDataByKey(surface_samples.PASSTHROUGH_KEY, True)
        purpose = node.get_param("purpose", chunk.is_param_t(str)).value
        UsdGeom.Imageable(prim).CreatePurposeAttr().Set(purpose)
    _LAST_COOK.pop(layer.identifier, None)
    node.get_param("out_mesh").value = out_path
    if readout := node.get_param("readout"):
        readout.value = f"kept as is: {_compact(triangles)} ≤ {_compact(keep_under)} tris"
    if out_tris := node.get_param("out_triangles"):
        out_tris.value = triangles


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
            default_value=256,
            tooltip="Voxels along the asset's longest side. Sets the finest detail the proxy can\n"
            "hold, and the scale every size below is measured in. A trunk or rail thinner\n"
            "than a few voxels comes out fat: 256 keeps a 30 m pine's 40 cm trunk reading\n"
            "as a trunk, 128 does not. Memory and time grow with its cube: a 256 tree\n"
            "takes ~1.5 s, a 384 cube-shaped asset several GB.",
        )
    )

    chunk_api.add_param(
        chunk.Slider(
            "keep_under",
            "Keep Source Under",
            is_int=True,
            min_value=0,
            max_value=1_000_000,
            default_value=3000,
            tooltip="An asset with no more triangles than this is not remeshed: the proxy is a\n"
            "reference to the asset itself, with its own UVs and materials, and Poly\n"
            "Reduce, UV Unwrap and Bake Textures leave it alone. Set it to the triangle\n"
            "budget the proxy is reduced to. 0 always remeshes.",
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
                default_value=0.75,
                tooltip="How far the proxy stands off the surface, in voxels. Open surfaces —\n"
                "leaf cards, sheet metal — become shells this thick. Much below 0.75 a thin\n"
                "card can break up.",
            ),
            chunk.Slider(
                "close",
                "Close Gaps",
                min_value=0.0,
                max_value=32.0,
                default_value=1.0,
                tooltip="Bridges gaps up to about twice this many voxels wide without growing\n"
                "the outline. 1 was best across 15 trees, plants and furniture: higher fuses\n"
                "chair legs and hanger rails, 0 breaks thin parts up. Raise it when a sparse\n"
                "canopy should read as one mass.",
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
                default_value=1.0,
                tooltip="Blur the volume before extracting the surface, in voxels. Takes out the\n"
                "voxel staircase; high values round off corners and thin parts. 1 matched\n"
                "the source's silhouette best across the same 15 assets.",
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
