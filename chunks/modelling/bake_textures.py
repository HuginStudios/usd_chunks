"""Bake Textures chunk -- transfer an asset's look onto a proxy's UVs as texture maps.

The last step of the proxy chain: Remesh Proxy → UV Unwrap → **Bake Textures** (Remesh
Proxy reduces its own mesh). Source is the full asset (any number of meshes, materials,
textures, instances); Target is the unwrapped low-poly mesh. The bake writes

* a **base colour** map -- every material and texture of the source re-sampled into one
  texture on the target's atlas, so two hundred CAD part colours or a tree's bark and
  needle sheets become one material;
* a **normal** map -- the source's surface direction in the target's tangent frame, which
  brings back the detail and the hard edges the proxy smoothed away;
* **roughness** and **metallic** maps -- each material's value, or its texture, so a proxy
  of a chrome-and-leather chair still reads as chrome and leather;
* (a map that comes out the same everywhere -- metallic usually, a flat normal map -- is
  not written: its value goes into the material instead;)
* an **opacity** map (off by default) -- where the source is cut away, for proxies that are
  themselves cards: a leaf cluster, a fence panel. The material then cuts at 0.5;

and binds a UsdPreviewSurface material reading them to the target.

The transfer itself is `usd_chunks.texture_bake`; Backend picks who does it. Built-in takes
each texel from the nearest samples of the source surface (numpy + scipy, no renderer
needed); ray-traced backends can register themselves there.

Files go to Output Folder (default: a `baked` folder beside the stage's root layer, or the
user cache for an unsaved stage), named after the target plus a content hash: a renderer
caches textures by path, so a rebake with new content gets a new name instead of showing
the stale image. The previous bake's files are removed when a new one replaces them.

Reads through `get_upstream_stage()`; a target inside the source is left out of the
source's samples.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import NamedTuple

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import chunk
from usd_chunks import chunk_stage
from usd_chunks import surface_samples
from usd_chunks import texture_bake
from usd_chunks.chunk_cache import user_cache_dir
from usd_chunks.mesh_math import fan_triangulate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# reading the target
# ---------------------------------------------------------------------------


def _target_mesh(prim, uv_name):
    """One target mesh in world space, or None (and why) if it can't be baked onto."""
    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None, "empty mesh"
    tris, corners, face_of_tri = fan_triangulate(counts, indices)
    if len(tris) == 0:
        return None, "no triangles"

    primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(uv_name)
    values = primvar.ComputeFlattened() if primvar and primvar.HasValue() else None
    corner_st = (
        surface_samples.per_corner(values, primvar.GetInterpolation(), tris, corners, face_of_tri)
        if values
        else None
    )
    if corner_st is None:
        return None, f"no '{uv_name}' UVs — run UV Unwrap first"

    matrix = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(prim), dtype=np.float64)
    local = np.asarray(points, dtype=np.float64)
    world = local @ matrix[:3, :3] + matrix[3, :3]
    # The normal map has to be baked against the normals the target is *shaded* with, or it
    # decodes in the wrong frame and the proxy looks crumpled: its authored normals, else
    # flat ones -- Storm shades a polygon mesh without normals faceted, and so must the bake.
    # Measured on a sphere: baked against smooth normals, the 80-triangle proxy rendered
    # creased; against flat ones, as smooth as the 18k-triangle source.
    to_normal = np.linalg.inv(matrix[:3, :3]).T
    normals = surface_samples.read_corner_normals(prim, local, tris, corners, face_of_tri).astype(np.float64)
    normals = normals @ to_normal
    normals /= np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-12)
    return (world, tris, np.asarray(corner_st, dtype=np.float64), normals), ""


def read_target(root, uv_name):
    """Every mesh under `root`, joined into one `texture_bake.Target` (they share an atlas)."""
    parts, problems = [], []
    for prim in Usd.PrimRange(root):
        if not prim.IsA(UsdGeom.Mesh):
            continue
        part, why = _target_mesh(prim, uv_name)
        if part is None:
            problems.append(f"{prim.GetName()}: {why}")
        else:
            parts.append(part)
    if not parts:
        return None, problems
    offset, points, tris = 0, [], []
    for world, triangles, _st, _n in parts:
        points.append(world)
        tris.append(triangles + offset)
        offset += len(world)
    target = texture_bake.Target(
        np.concatenate(points),
        np.concatenate(tris),
        np.concatenate([p[2] for p in parts]),
        np.concatenate([p[3] for p in parts]),
    )
    return target, problems


# ---------------------------------------------------------------------------
# where the maps go, and the material that reads them
# ---------------------------------------------------------------------------


def _output_dir(node, stage) -> str:
    folder = node.get_param("output_dir").value
    if folder:
        return folder
    root = stage.GetRootLayer()
    if root and not root.anonymous and root.realPath:
        return os.path.join(os.path.dirname(root.realPath), "baked")
    return user_cache_dir("baked")


def _material_path(node, target_path: str) -> str:
    """The param if set, else `<target>_baked` beside the target."""
    out = node.get_param("material_path").value
    if out:
        return out
    target = Sdf.Path(target_path)
    return str(target.GetParentPath().AppendChild(f"{target.name}_baked"))


def _author_material(stage, path, files: dict, uv_name: str, constants: dict | None = None):
    """A UsdPreviewSurface reading the baked maps through `uv_name`; a map that baked to one
    value everywhere (`constants`) is set as that value instead of a texture."""
    constants = constants or {}
    material = UsdShade.Material.Define(stage, path)
    surface = UsdShade.Shader.Define(stage, f"{path}/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(surface.ConnectableAPI(), "surface")

    reader = UsdShade.Shader.Define(stage, f"{path}/UV")
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(uv_name)
    st = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    def texture(name, file, colour_space):
        shader = UsdShade.Shader.Define(stage, f"{path}/{name}")
        shader.CreateIdAttr("UsdUVTexture")
        shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(file.replace("\\", "/")))
        shader.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set(colour_space)
        shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st)
        for wrap in ("wrapS", "wrapT"):
            shader.CreateInput(wrap, Sdf.ValueTypeNames.Token).Set("clamp")
        return shader

    if file := files.get(texture_bake.MAP_BASE_COLOUR):
        colour = texture("BaseColor", file, "sRGB")
        surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            colour.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        )
    for map_name, input_name in (
        (texture_bake.MAP_ROUGHNESS, "roughness"),
        (texture_bake.MAP_METALLIC, "metallic"),
        (texture_bake.MAP_OPACITY, "opacity"),
    ):
        if file := files.get(map_name):
            scalar = texture(map_name.capitalize(), file, "raw")
            surface.CreateInput(input_name, Sdf.ValueTypeNames.Float).ConnectToSource(
                scalar.CreateOutput("r", Sdf.ValueTypeNames.Float)
            )
    if files.get(texture_bake.MAP_OPACITY):
        surface.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(0.5)
    for map_name, value in constants.items():
        if map_name == texture_bake.MAP_BASE_COLOUR:
            surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*value[:3]))
        elif map_name == texture_bake.MAP_NORMAL:
            if np.dot(np.asarray(value), (0.0, 0.0, 1.0)) < 0.999:  # flat is the default
                surface.CreateInput("normal", Sdf.ValueTypeNames.Normal3f).Set(Gf.Vec3f(*value))
        else:
            input_name = {texture_bake.MAP_ROUGHNESS: "roughness", texture_bake.MAP_METALLIC: "metallic"}.get(
                map_name, map_name
            )
            surface.CreateInput(input_name, Sdf.ValueTypeNames.Float).Set(float(value))
    if file := files.get(texture_bake.MAP_NORMAL):
        normal = texture("Normal", file, "raw")
        # Stored 0-1, read back as -1..1: the UsdPreviewSurface convention for normal maps.
        normal.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(2, 2, 2, 1))
        normal.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(-1, -1, -1, 0))
        surface.CreateInput("normal", Sdf.ValueTypeNames.Normal3f).ConnectToSource(
            normal.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        )
    return material


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------


class _Settings(NamedTuple):
    resolution: int  # 0 = automatic, from the target's triangle count
    maps: tuple
    backend: str
    uv_name: str


def _settings(node) -> _Settings:
    is_b = chunk.is_param_t(bool)
    maps = tuple(
        name
        for name, param in (
            (texture_bake.MAP_BASE_COLOUR, "bake_colour"),
            (texture_bake.MAP_NORMAL, "bake_normal"),
            (texture_bake.MAP_ROUGHNESS, "bake_roughness"),
            (texture_bake.MAP_METALLIC, "bake_metallic"),
            (texture_bake.MAP_OPACITY, "bake_opacity"),
        )
        if node.get_param(param, is_b).value
    )
    auto = node.get_param("auto_resolution", is_b).value
    return _Settings(
        resolution=0 if auto else int(node.get_param("resolution", chunk.is_param_t(int)).value),
        maps=maps,
        backend=node.get_param("backend", chunk.is_param_t(str)).value,
        uv_name=node.get_param("uv_name", chunk.is_param_t(str)).value or "st",
    )


def _stamp(sources, target: texture_bake.Target, settings: _Settings) -> tuple:
    return (
        tuple(settings),
        surface_samples.fingerprint(sources),
        surface_samples.crc(target.points),
        surface_samples.crc(target.tris),
        surface_samples.crc(target.corner_st),
        surface_samples.crc(target.corner_normals),
    )


class _Bake(NamedTuple):
    stamp: tuple
    files: dict  # map -> path
    constants: dict  # map -> the value a uniform map was replaced by
    resolution: int


# Keyed by the chunk's own layer identifier.
_LAST_COOK: dict = {}


def _report(node, text) -> None:
    if readout := node.get_param("readout"):
        readout.value = text
    logger.debug("%s", text)


def _set_outputs(node, material="", files=None) -> None:
    files = files or {}
    node.get_param("out_material").value = material
    node.get_param("out_basecolor").value = files.get(texture_bake.MAP_BASE_COLOUR, "")
    node.get_param("out_normal").value = files.get(texture_bake.MAP_NORMAL, "")
    node.get_param("out_roughness").value = files.get(texture_bake.MAP_ROUGHNESS, "")
    node.get_param("out_metallic").value = files.get(texture_bake.MAP_METALLIC, "")
    node.get_param("out_opacity").value = files.get(texture_bake.MAP_OPACITY, "")


def _read(node, source_paths, target_path, uv_name):
    upstream = chunk_stage.get_upstream_stage(node.get_stage(), node.get_scene(), node)
    sources = [
        source
        for path in source_paths
        for source in surface_samples.gather_sources(upstream.GetPrimAtPath(path), exclude=[target_path])
    ]
    target, problems = read_target(upstream.GetPrimAtPath(target_path), uv_name)
    return upstream, sources, target, problems


def _write(node, stage, target_path, images, stamp) -> tuple[dict, dict]:
    """Write every map that varies; return `(files, constants)` -- the uniform ones become
    material values instead, which saves a file, a texture upload and a lookup per pixel."""
    folder = _output_dir(node, stage)
    os.makedirs(folder, exist_ok=True)
    digest = hashlib.sha1(repr(stamp).encode()).hexdigest()[:8]
    files, constants = {}, {}
    for name, image in images.items():
        if (value := texture_bake.uniform_value(name, image)) is not None:
            constants[name] = value
            continue
        path = os.path.join(folder, f"{Sdf.Path(target_path).name}_{name}_{digest}.png")
        texture_bake.write_map(path, image, srgb=name == texture_bake.MAP_BASE_COLOUR)
        files[name] = path
    return files, constants


def _forget(previous: _Bake | None, keep: dict) -> None:
    """Delete the files a replaced bake wrote, unless the new one is writing the same paths."""
    if previous is None:
        return
    for path in set(previous.files.values()) - set(keep.values()):
        try:
            os.remove(path)
        except OSError:
            pass


def execute(node: chunk.Chunk) -> None:
    """Bake the source's look onto the target and bind a material reading the maps."""
    stage = node.get_stage()
    layer = node.get_edit_context()
    if not layer:
        return
    settings = _settings(node)
    backend = texture_bake.get_backend(settings.backend)
    if backend is None or not backend.available():
        _report(node, f"backend '{settings.backend}' is not available — needs scipy")
        _set_outputs(node)
        return
    source_paths = node.get_param("source_path").resolve_paths()
    target_paths = node.get_param("target_path").resolve_paths()
    if not source_paths or not target_paths:
        _report(node, "pick a source asset and a target mesh")
        _set_outputs(node)
        return
    if not settings.maps:
        _report(node, "no maps selected")
        _set_outputs(node)
        return

    target_path = str(target_paths[0])
    if surface_samples.is_passthrough(node.get_stage().GetPrimAtPath(target_path)):
        _report(node, "target is its own source (kept as is) — nothing to bake")
        _set_outputs(node)
        return
    upstream, sources, target, problems = _read(node, source_paths, target_path, settings.uv_name)
    del upstream
    if target is None:
        _report(node, problems[0] if problems else "no mesh under the target")
        _set_outputs(node)
        return
    if not sources:
        _report(node, "nothing to bake from under the source")
        _set_outputs(node)
        return

    resolution = settings.resolution or texture_bake.auto_resolution(len(target.tris))
    stamp = _stamp(sources, target, settings._replace(resolution=resolution))
    previous = _LAST_COOK.get(layer.identifier)
    if previous is not None and previous.stamp == stamp and all(os.path.isfile(f) for f in previous.files.values()):
        files, constants = previous.files, previous.constants
    else:
        request = texture_bake.BakeRequest(sources, target, resolution, frozenset(settings.maps))
        images = texture_bake.bake(request, settings.backend)
        files, constants = _write(node, stage, target_path, images, stamp)
        _forget(previous, files)

    material_path = _material_path(node, target_path)
    with Usd.EditContext(stage, layer):
        material = _author_material(stage, material_path, files, settings.uv_name, constants)
        if node.get_param("bind", chunk.is_param_t(bool)).value:
            target_prim = stage.GetPrimAtPath(target_path)
            UsdShade.MaterialBindingAPI.Apply(target_prim).Bind(material)

    if len(_LAST_COOK) > 64:
        _LAST_COOK.clear()
    _LAST_COOK[layer.identifier] = _Bake(stamp, files, constants, resolution)
    _set_outputs(node, material_path, files)
    note = f" · skipped {len(problems)} mesh(es)" if problems else ""
    flat = f" · constant: {', '.join(constants)}" if constants else ""
    _report(node, f"{resolution}px · {', '.join(files) or 'no maps'}{flat}{note}")


def _sync_resolution_param(node) -> None:
    """Show the resolution slider only while it is the one deciding."""
    param = node.get_param("resolution")
    if param is None:
        return
    if node.get_param("auto_resolution", chunk.is_param_t(bool)).value:
        param.set_state(chunk.ChunkFlags.State.HIDDEN)
    else:
        param.unset_state(chunk.ChunkFlags.State.HIDDEN)


def resolution_mode_changed(owner, notice, sender) -> None:
    _sync_resolution_param(owner.get_chunk() or owner)


_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def main():
    chunk_api = chunk.Chunk("Bake Textures")

    chunk_api.add_param(
        chunk.LineEdit(
            "source_path",
            "Source",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="The full-detail asset whose look is baked: every visible mesh under it,\n"
            "with its materials, textures and instances.",
        )
    )
    chunk_api.add_param(
        chunk.LineEdit(
            "target_path",
            "Target",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="The mesh baked onto — needs UVs (UV Unwrap). Several meshes under it\n"
            "bake into one texture set, so they must share one atlas.",
        )
    )
    chunk_api.add_param(
        chunk.Label("readout", "not baked yet", default_value="not baked yet", tooltip="What the last cook produced.")
    )

    maps = chunk.Group("Maps", flags=_GF)
    maps.add_params(
        [
            chunk.Checkbox(
                "auto_resolution",
                "Auto Resolution",
                default_value=False,
                tooltip="Size the maps from the target's triangle count -- about 16x16 texels a\n"
                "triangle, a power of two from 256 to 4096: 1k triangles bakes at 512, 3k at\n"
                "1024, 20k at 2048. A far LOD gets small maps without anyone setting them.",
            ),
            chunk.Slider(
                "resolution",
                "Resolution",
                is_int=True,
                min_value=64,
                max_value=8192,
                default_value=512,
                tooltip="Width and height of each map in pixels, when Auto Resolution is off. Match\n"
                "UV Unwrap's resolution: its padding between charts is measured in these texels.",
            ),
            chunk.Checkbox(
                "bake_colour",
                "Base Colour",
                default_value=True,
                tooltip="Every material and texture of the source, re-sampled into one map.",
            ),
            chunk.Checkbox(
                "bake_normal",
                "Normal",
                default_value=True,
                tooltip="The source's surface direction in the target's tangent space (OpenGL\n"
                "convention) — restores detail and hard edges the proxy smoothed away.",
            ),
            chunk.Checkbox(
                "bake_roughness",
                "Roughness",
                default_value=True,
                tooltip="Each material's roughness (UsdPreviewSurface roughness, OpenPBR /\n"
                "standard_surface specular_roughness), from its texture where it has one.",
            ),
            chunk.Checkbox(
                "bake_metallic",
                "Metallic",
                default_value=True,
                tooltip="Each material's metalness (metallic, base_metalness, metalness).",
            ),
            chunk.Checkbox(
                "bake_opacity",
                "Opacity",
                default_value=False,
                tooltip="Where the source is cut away by an alpha-tested material, so a card-like\n"
                "proxy can cut the same holes. Off for a closed proxy, which has nothing\n"
                "to cut.",
            ),
        ]
    )
    chunk_api.add_param(maps)
    chunk_api.add_callback(resolution_mode_changed, sender=chunk_api.get_param("auto_resolution"))
    _sync_resolution_param(chunk_api)

    output = chunk.Group("Output", flags=_GF)
    output.add_params(
        [
            chunk.Menu(
                "backend",
                "Backend",
                menu_items=texture_bake.backend_names(),
                default_value=texture_bake.NearestSampleBackend.name,
                tooltip="Who does the transfer. Built-in: each texel from the nearest samples of\n"
                "the source surface — no renderer needed.",
            ),
            chunk.LineEdit(
                "uv_name",
                "UV Set",
                default_value="st",
                flags=_PF.LABELED,
                tooltip="The target's UV primvar, and the one the baked material reads.",
            ),
            chunk.LineEdit(
                "output_dir",
                "Output Folder",
                flags=_PF.LABELED,
                tooltip="Where the maps are written. Empty: a 'baked' folder beside the stage's\n"
                "root layer, or the user cache for an unsaved stage.",
            ),
            chunk.LineEdit(
                "material_path",
                "Material Path",
                flags=_PF.LABELED,
                tooltip="Where the material is authored. Empty: <target>_baked beside the target.",
            ),
            chunk.Checkbox(
                "bind",
                "Bind to Target",
                default_value=True,
                tooltip="Bind the baked material to the target.",
            ),
        ]
    )
    chunk_api.add_param(output)

    for name, label, tip in (
        ("out_material", "Material", "The baked material."),
        ("out_basecolor", "Base Colour Map", "The base colour PNG."),
        ("out_normal", "Normal Map", "The normal map PNG."),
        ("out_roughness", "Roughness Map", "The roughness PNG (greyscale)."),
        ("out_metallic", "Metallic Map", "The metallic PNG (greyscale)."),
        ("out_opacity", "Opacity Map", "The opacity PNG (greyscale)."),
    ):
        chunk_api.add_param(chunk.LineEdit(name, label, flags=_PF.OUTPUT | _PF.LABELED, tooltip=tip))

    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        requirements=["numpy>=2,<3", "scipy>=1.11,<2", "pillow>=10"],
        description=(
            "Bakes a full-detail asset's look onto a low-poly mesh's UVs: base colour,\n"
            "roughness and metallic re-sampled from all of its materials and textures, a\n"
            "tangent-space normal map that restores the detail the low-poly surface lost,\n"
            "and optionally opacity. Binds a UsdPreviewSurface reading them.\n\n"
            "The end of the proxy chain: Remesh Proxy → UV Unwrap → Bake."
        ),
    )
