"""UV Unwrap chunk -- automatic charts and packing, via xatlas.

A proxy out of Remesh Proxy or Poly Reduce has no UVs worth the name, and a baked normal or
colour map needs somewhere to go. xatlas (Jonathan Young's library, the one Godot and
Blender's add-ons use) segments the surface into low-distortion charts, flattens each one
and packs them into a square atlas with padding between charts so texture filtering
doesn't bleed across a seam.

`st` is written **faceVarying**, one value per face corner. That is what lets a seam exist
without touching the mesh's points: two faces meeting at a seam simply give the shared
vertex different coordinates. A triangle mesh keeps its topology exactly. A polygon mesh is
fan-triangulated first (xatlas works on triangles, and one quad's two halves may land in
different charts), and everything indexed by face or face-corner -- faceVarying and
uniform primvars, GeomSubsets -- is carried onto the triangles.

**One Atlas** packs every mesh under the picked prim into a single 0-1 square, at one
texel density measured in world space, so a whole asset bakes to one texture. Off, each
mesh gets its own square.

Reads through `get_upstream_stage()`: it writes `st` (and maybe topology) on the prim it
reads, so a composed read would unwrap its own output.
"""

from __future__ import annotations

import logging
import zlib
from typing import NamedTuple

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_utils
from usd_chunks import chunk_stage
from usd_chunks import surface_samples
from usd_chunks.mesh_math import fan_triangulate

try:
    import xatlas

    _HAS_XATLAS = True
except ImportError:  # pragma: no cover - exercised only on a machine without the wheel
    xatlas = None
    _HAS_XATLAS = False

logger = logging.getLogger(__name__)

# Attributes that index the polygon topology and can't be carried onto its triangles.
_STALE_ATTRS = (
    "cornerIndices",
    "cornerSharpnesses",
    "creaseIndices",
    "creaseLengths",
    "creaseSharpnesses",
    "holeIndices",
)


# ---------------------------------------------------------------------------
# array maths -- no USD
# ---------------------------------------------------------------------------


def unwrap(meshes, resolution, padding):
    """Per-corner UVs for a list of `(points, triangles)`, packed into one atlas.

    Returns one `(m, 3, 2)` float32 array per mesh, in input triangle order, or None for a
    mesh xatlas couldn't keep in order (it drops nothing in practice; this is the guard).
    """
    atlas = xatlas.Atlas()
    for points, triangles in meshes:
        atlas.add_mesh(
            np.ascontiguousarray(points, dtype=np.float32), np.ascontiguousarray(triangles, dtype=np.uint32)
        )
    pack = xatlas.PackOptions()
    pack.resolution = int(resolution)
    pack.padding = int(padding)
    pack.bilinear = True
    atlas.generate(pack_options=pack)

    out = []
    for i, (_points, triangles) in enumerate(meshes):
        _vmapping, indices, uvs = atlas.get_mesh(i)
        if len(indices) != len(triangles):
            logger.warning("xatlas changed the triangle count (%d -> %d)", len(triangles), len(indices))
            out.append(None)
            continue
        out.append(np.asarray(uvs, dtype=np.float32)[np.asarray(indices, dtype=np.int64)])
    return out


# ---------------------------------------------------------------------------
# USD
# ---------------------------------------------------------------------------


def _mesh_prims(prim, recurse):
    if not prim or not prim.IsValid() or surface_samples.is_passthrough(prim):
        return []  # a proxy kept as its source: its own UVs are the ones to keep
    found = []
    for candidate in Usd.PrimRange(prim) if recurse else [prim]:
        if not candidate.IsA(UsdGeom.Mesh):
            continue
        if candidate.IsInstanceProxy():
            logger.warning("skipping %s: cannot author an override on an instance proxy", candidate.GetPath())
            continue
        found.append(candidate)
    return found


class _Plan(NamedTuple):
    """One mesh: its triangles, how they map back to the polygons, and its new st."""

    path: Sdf.Path
    triangles: np.ndarray  # (m, 3)
    corners: np.ndarray  # (m, 3) indices into the original face-corner array
    face_of_tri: np.ndarray  # (m,)
    retopo: bool  # False when the mesh was all triangles already: topology untouched
    st: np.ndarray | None  # (m, 3, 2)


def _plan(prim) -> _Plan | None:
    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    if not points or not counts or not indices:
        return None
    triangles, corners, face_of_tri = fan_triangulate(counts, indices)
    if len(triangles) == 0:
        return None
    retopo = bool(np.any(np.asarray(counts) != 3))
    return _Plan(prim.GetPath(), triangles, corners, face_of_tri, retopo, None)


def _sizes_matter(prims, settings: _Settings) -> bool:
    """Whether the meshes' transforms change the unwrap.

    Only when several meshes share one atlas: their sizes against each other set how much of
    the square each one gets. One mesh alone fills it whatever its size.
    """
    return settings.one_atlas and len(prims) > 1


def _sizing(prim) -> np.ndarray:
    """The 3x3 part of a prim's local-to-world transform: scale, rotation and shear.

    Not the translation, which moves a mesh without changing its shape or size.
    """
    matrix = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(prim), dtype=np.float64)
    return matrix[:3, :3]


def _positions(prim, sizes_matter: bool) -> np.ndarray:
    points = np.asarray(UsdGeom.Mesh(prim).GetPointsAttr().Get(), dtype=np.float64)
    # Sized as in the world when the meshes share an atlas, so they get one texel density.
    # Local otherwise: xatlas is scale-invariant once resolution is fixed.
    return points @ _sizing(prim) if sizes_matter else points


def _author(stage, plan: _Plan, name: str) -> None:
    prim = stage.GetPrimAtPath(plan.path)
    if not prim or plan.st is None:
        return
    mesh = UsdGeom.Mesh(prim)
    api = UsdGeom.PrimvarsAPI(prim)
    writes, blocks = [], []

    if plan.retopo:
        n = len(plan.triangles)
        writes += [
            (mesh.GetFaceVertexCountsAttr(), Vt.IntArray.FromNumpy(np.full(n, 3, dtype=np.int32))),
            (
                mesh.GetFaceVertexIndicesAttr(),
                Vt.IntArray.FromNumpy(np.ascontiguousarray(plan.triangles.reshape(-1), dtype=np.int32)),
            ),
        ]
        corner_order = plan.corners.reshape(-1)
        for primvar in api.GetPrimvars():
            if primvar.GetPrimvarName() == name:
                continue
            interpolation = primvar.GetInterpolation()
            if interpolation not in (UsdGeom.Tokens.faceVarying, UsdGeom.Tokens.uniform):
                continue
            values = primvar.ComputeFlattened() if primvar.HasValue() else None
            if not values:
                continue
            picks = corner_order if interpolation == UsdGeom.Tokens.faceVarying else plan.face_of_tri
            if picks.max(initial=-1) >= len(values):
                blocks.append(primvar.GetAttr())
                continue
            writes.append((primvar.GetAttr(), _remap(values, picks)))
            blocks.append(primvar.CreateIndicesAttr())
        for subset in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(prim)):
            if subset.GetElementTypeAttr().Get() != UsdGeom.Tokens.face:
                continue
            members = np.zeros(int(plan.face_of_tri.max()) + 1, dtype=bool)
            idx = np.asarray(subset.GetIndicesAttr().Get() or [], dtype=np.int64)
            members[idx[(idx >= 0) & (idx < len(members))]] = True
            new = np.flatnonzero(members[plan.face_of_tri]).astype(np.int32)
            writes.append((subset.GetIndicesAttr(), Vt.IntArray.FromNumpy(new)))
        blocks += [attr for n_ in _STALE_ATTRS if (attr := prim.GetAttribute(n_)) and attr.HasAuthoredValue()]
        if mesh.GetSubdivisionSchemeAttr().Get() not in (None, UsdGeom.Tokens.none):
            # The triangles describe the same polygons only as a flat cage.
            writes.append((mesh.GetSubdivisionSchemeAttr(), UsdGeom.Tokens.none))

    st = api.CreatePrimvar(name, Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying)
    writes.append(
        (st.GetAttr(), Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(plan.st.reshape(-1, 2), dtype=np.float32)))
    )
    blocks.append(st.CreateIndicesAttr())  # an inherited index array would re-scramble it

    with Sdf.ChangeBlock():
        for attr, value in writes:
            attr.Set(value)
        for attr in blocks:
            attr.Block()


def _remap(values, picks):
    picked = np.asarray(values)[picks]
    try:
        return type(values).FromNumpy(np.ascontiguousarray(picked))
    except Exception:
        return type(values)(picked.tolist())


class _Settings(NamedTuple):
    resolution: int
    padding: int
    one_atlas: bool
    recurse: bool
    name: str


def _settings(node) -> _Settings:
    is_i = chunk.is_param_t(int)
    is_b = chunk.is_param_t(bool)
    return _Settings(
        resolution=int(node.get_param("resolution", is_i).value),
        padding=int(node.get_param("padding", is_i).value),
        one_atlas=bool(node.get_param("one_atlas", is_b).value),
        recurse=bool(node.get_param("recurse", is_b).value),
        name=node.get_param("uv_name", chunk.is_param_t(str)).value or "st",
    )


def _crc(values) -> tuple:
    array = np.ascontiguousarray(np.asarray(values))
    return array.shape, zlib.crc32(array)


def _fingerprint(prims, settings: _Settings) -> tuple:
    parts = [tuple(settings)]
    for prim in prims:
        mesh = UsdGeom.Mesh(prim)
        entry = [str(prim.GetPath())]
        entry += [
            _crc(attr.Get() or [])
            for attr in (mesh.GetPointsAttr(), mesh.GetFaceVertexCountsAttr(), mesh.GetFaceVertexIndicesAttr())
        ]
        if _sizes_matter(prims, settings):
            entry.append(tuple(map(tuple, _sizing(prim))))
        parts.append(tuple(entry))
    return tuple(parts)


# Keyed by the chunk's layer identifier (get_layer_identifier: the same whichever layer a
# cook writes into, its own or a diff cook's scratch layer): (fingerprint, plans).
_LAST_COOK: dict = {}


def _targets(node, settings: _Settings, paths):
    upstream = chunk_stage.get_upstream_stage(node.get_stage(), node.get_scene(), node)
    return upstream, [prim for path in paths for prim in _mesh_prims(upstream.GetPrimAtPath(path), settings.recurse)]


def _plan_all(prims, settings: _Settings) -> list:
    plans = [(prim, plan) for prim in prims if (plan := _plan(prim)) is not None]
    if not plans:
        return []
    sizes_matter = _sizes_matter(prims, settings)
    inputs = [(_positions(prim, sizes_matter), plan.triangles) for prim, plan in plans]
    if settings.one_atlas:
        uvs = unwrap(inputs, settings.resolution, settings.padding)
    else:
        uvs = [unwrap([one], settings.resolution, settings.padding)[0] for one in inputs]
    return [plan._replace(st=st) for (_prim, plan), st in zip(plans, uvs)]


def _report(node, plans, note="") -> None:
    done = [p for p in plans if p.st is not None]
    if done:
        tris = sum(len(p.triangles) for p in done)
        meshes = f" · {len(done)} meshes" if len(done) > 1 else ""
        text = f"unwrapped {tris:,} tris{meshes}"
    else:
        text = note or "nothing unwrapped — check the mesh path"
    if readout := node.get_param("readout"):
        readout.value = text


def cache_key(node):
    paths = node.get_param("prim_path").resolve_paths()
    if not paths or not _HAS_XATLAS:
        return ()
    settings = _settings(node)
    _upstream, prims = _targets(node, settings, paths)
    return _fingerprint(prims, settings)


def execute(node: chunk.Chunk) -> None:
    """Unwrap every mesh under the picked prim(s) into a fresh st."""
    stage = node.get_stage()
    layer = node.get_edit_context()
    if not layer:
        return
    if not _HAS_XATLAS:
        _report(node, [], "xatlas is not installed — pip install xatlas")
        return
    paths = node.get_param("prim_path").resolve_paths()
    if not paths:
        _report(node, [], "pick a mesh to unwrap")
        return

    settings = _settings(node)
    upstream, prims = _targets(node, settings, paths)
    stamp = _fingerprint(prims, settings)
    previous = _LAST_COOK.get(node.get_layer_identifier())
    plans = previous[1] if previous is not None and previous[0] == stamp else _plan_all(prims, settings)
    del upstream

    with Usd.EditContext(stage, chunk_utils.edit_target(stage, layer)):
        for plan in plans:
            _author(stage, plan, settings.name)

    if len(_LAST_COOK) > 256:
        _LAST_COOK.clear()
    _LAST_COOK[node.get_layer_identifier()] = (stamp, plans)
    _report(node, plans)


_PF = chunk.ChunkFlags.ParamFlags
_GF = (
    chunk.ChunkFlags.GroupFlags.COLLAPSABLE
    | chunk.ChunkFlags.GroupFlags.LABELED
    | chunk.ChunkFlags.GroupFlags.FRAMED
)


def main():
    chunk_api = chunk.Chunk("UV Unwrap")

    chunk_api.add_param(
        chunk.LineEdit(
            "prim_path",
            "Mesh",
            flags=_PF.LABELED | _PF.PRIM_SELECTION,
            tooltip="The mesh to unwrap — or any prim above one.",
        )
    )
    chunk_api.add_param(
        chunk.Label("readout", "not unwrapped yet", default_value="not unwrapped yet", tooltip="What the last cook produced.")
    )

    atlas = chunk.Group("Atlas", flags=_GF)
    atlas.add_params(
        [
            chunk.Slider(
                "resolution",
                "Resolution",
                is_int=True,
                min_value=64,
                max_value=8192,
                default_value=512,
                tooltip="The texture size the charts are packed for, in pixels. Bake at this size:\n"
                "padding is measured in its texels.",
            ),
            chunk.Slider(
                "padding",
                "Padding",
                is_int=True,
                min_value=0,
                max_value=16,
                default_value=2,
                tooltip="Texels between charts, so filtering and mip-maps don't bleed one chart's\n"
                "colour into its neighbour.",
            ),
            chunk.Checkbox(
                "one_atlas",
                "One Atlas",
                default_value=True,
                tooltip="Pack every mesh under the prim into one 0-1 square at one world-space\n"
                "texel density, so the asset bakes to one texture. Off: each mesh gets its\n"
                "own square.",
            ),
            chunk.Checkbox(
                "recurse",
                "Include Children",
                default_value=True,
                tooltip="Unwrap every mesh under the selected prim, not just the prim itself.",
            ),
            chunk.LineEdit(
                "uv_name",
                "UV Set",
                default_value="st",
                flags=_PF.LABELED,
                tooltip="The primvar the UVs are written to. 'st' is what UsdPreviewSurface and\n"
                "most renderers read by default.",
            ),
        ]
    )
    chunk_api.add_param(atlas)
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="image",
        requirements=["numpy>=2,<3", "xatlas>=0.0.9,<1"],
        cache_cook=True,
        description=(
            """
        Generates UVs for a mesh automatically (xatlas), e.g. for a proxy before baking.

        Generates UVs automatically with xatlas: splits the surface into low-distortion
        charts, flattens them and packs them into one atlas with padding. Writes a
        faceVarying st, so seams cost no extra points.

        Use it after Remesh Proxy / Poly Reduce to give a proxy somewhere to bake
        normal and colour maps to.
        """
        ),
    )
