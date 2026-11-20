"""Height Field chunk -- marks a band of height on meshes, to scatter or shade by height.

On a hillside, what grows depends on height: trees stop at a tree line, sand lines the shore,
the peaks are bare rock. This chunk paints that as a 0..1 primvar on the meshes: 1 between
Min Height and Max Height, 0 further than Softness outside them, a smooth fade between.

* Wire the primvar into a Scatter Points' Density Field to keep a layer inside the band.
* Multiply With: multiply in another field on the same meshes (a Noise Field's, say), so
  height and noise make the one field a Scatter reads.
* Face Subset: also put the faces inside the band in a GeomSubset, so Bind Material can give
  the band its own material.
* Show Field: tint the meshes by the field, to see the band in the viewport.

Height is world space along the stage's up axis, in scene units. The field is per vertex, so
its edge is as fine as the mesh.

Requires: pxr (OpenUSD), usd_chunks, numpy.
"""

from __future__ import annotations

import logging

import numpy as np
from pxr import Sdf, Usd, UsdGeom, UsdShade, Vt

from usd_chunks import chunk, chunk_stage, chunk_utils

logger = logging.getLogger(__name__)

#: Show Field's tint: blue, since usdview highlights the selection in yellow (as Noise Field).
_COLOUR = (0.15, 0.55, 1.0)


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def band(heights: np.ndarray, low: float, high: float, softness: float) -> np.ndarray:
    """1 inside [low, high], 0 further than *softness* outside it, smoothstepped between."""
    outside = np.maximum(low - heights, heights - high).clip(min=0.0)
    if softness <= 0.0:
        return (outside <= 0.0).astype(np.float64)
    t = 1.0 - np.clip(outside / softness, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


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


def _faces_in_band(mesh: UsdGeom.Mesh, heights: np.ndarray, low: float, high: float) -> np.ndarray:
    """Indices of the faces whose centre (the mean of its corners) is inside [low, high]."""
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get() or [], dtype=np.int64)
    indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get() or [], dtype=np.int64)
    if not len(counts) or counts.sum() != len(indices) or (len(indices) and indices.max() >= len(heights)):
        return np.zeros(0, dtype=np.int64)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    real = counts > 0  # a face with no corners has no centre, and would break reduceat
    centres = np.full(len(counts), np.nan)
    centres[real] = np.add.reduceat(heights[indices], starts[real]) / counts[real]
    return np.flatnonzero((centres >= low) & (centres <= high))


def _write_tint(out, mesh, values, colour):
    """Show the field: the mesh's own colour moved toward *colour* by the field, with the
    material set aside, since displayColor loses to any shader. The layer is cleared on every
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
    name = node.get_param("primvar", is_s).value.strip() or "height_band"
    base_name = node.get_param("base_primvar", is_s).value.strip()
    subset_name = node.get_param("subset", is_s).value.strip()
    low, high = node.get_param("min_height", is_f).value, node.get_param("max_height", is_f).value
    low, high = min(low, high), max(low, high)
    softness = max(0.0, node.get_param("softness", is_f).value)
    show = node.get_param("show_field", chunk.is_param_t(bool)).value
    colour = np.asarray(node.get_param("field_color").value or _COLOUR, dtype=np.float64) if show else None
    if not paths:
        _report(node, "set the meshes to paint")
        return
    if subset_name and not Sdf.Path.IsValidIdentifier(subset_name):
        logger.warning("%s: %r is not a valid prim name for a face subset", node.name, subset_name)
        subset_name = ""
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    up_index = 1 if UsdGeom.GetStageUpAxis(upstream).upper() == "Y" else 2
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    painted, total, lit, faces = 0, 0, 0.0, 0
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
                heights = (np.asarray(pts, dtype=np.float64) @ m[:3, :3] + m[3, :3])[:, up_index]
                values = band(heights, low, high, softness)
                base = _base_values(prim, base_name, len(values))
                if base is not None:
                    values = values * np.clip(base, 0.0, 1.0)
                out = UsdGeom.PrimvarsAPI(stage.OverridePrim(prim.GetPath()))
                pv = out.CreatePrimvar(name, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
                pv.Set(Vt.FloatArray.FromNumpy(values.astype(np.float32)))
                if colour is not None:
                    _write_tint(out, mesh, values, colour)
                if subset_name:
                    inside = _faces_in_band(mesh, heights, low, high)
                    subset = UsdGeom.Subset.Define(stage, prim.GetPath().AppendChild(subset_name))
                    subset.CreateElementTypeAttr(UsdGeom.Tokens.face)
                    subset.CreateFamilyNameAttr(UsdShade.Tokens.materialBind)
                    subset.CreateIndicesAttr(Vt.IntArray.FromNumpy(inside.astype(np.int32)))
                    faces += len(inside)
                painted += 1
                total += len(values)
                lit += float(values.sum())
    if not painted:
        _report(node, "no meshes found to paint")
        return
    text = (f"primvars:{name} on {painted} mesh{'es' if painted != 1 else ''} · "
            f"{100.0 * lit / max(total, 1):.0f}% in the band")
    if subset_name:
        text += f" · {faces} faces in {subset_name}"
    _report(node, text)


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Height Field")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Meshes", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="The ground to paint: meshes, or prims with meshes under them. Comma-separate several.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primvar", "Primvar", default_value="height_band", flags=labeled,
        tooltip="Name of the primvar written. Put it in a Scatter Points' Density Field.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "base_primvar", "Multiply With", flags=labeled,
        tooltip="Another primvar on the same meshes (a Noise Field's) to multiply in. Empty: height only.",
    ))
    # Sliders keep a typed value outside their range: a centimetre scene's heights run into thousands.
    chunk_api.add_param(chunk.Slider("min_height", "Min Height", min_value=-100.0, max_value=500.0,
                                     default_value=0.0, tooltip="The bottom of the band, in scene units."))
    chunk_api.add_param(chunk.Slider("max_height", "Max Height", min_value=-100.0, max_value=500.0,
                                     default_value=10.0, tooltip="The top of the band: a tree line, say."))
    chunk_api.add_param(chunk.Slider("softness", "Softness", min_value=0.0, max_value=50.0, default_value=2.0,
                                     tooltip="How far past the band the field fades out, in scene units. "
                                     "0 = a hard edge."))
    chunk_api.add_param(chunk.LineEdit(
        "subset", "Face Subset", flags=labeled,
        tooltip="Also put the faces inside the band in a face subset with this name, for Bind Material "
        "(sand on a shore, rock on the peaks). Empty: no subset.",
    ))
    display = chunk.Group("display_grp", "Display", icon="eye")
    display.add_param(chunk.ColorPicker("field_color", "Field Color", default_value=_COLOUR,
                                        tooltip="The colour Show Field tints the band."))
    display.add_param(chunk.Checkbox(
        "show_field", "Show Field", default_value=False, flags=labeled,
        tooltip="Tint the meshes by the field to see the band. It hides their material, so turn it off "
        "for a render.",
    ))
    chunk_api.add_param(display)
    chunk_api.add_param(chunk.Label("readout", "nothing painted yet", default_value="nothing painted yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="layers",
        description="""
        Marks a height band on meshes, to scatter below a tree line or give a shore sand.

        Paints a 0..1 primvar: 1 between Min Height and Max Height, fading to 0 over
        Softness. Put it in a Scatter Points' Density Field to keep a layer inside the band,
        and Multiply With another field to combine them. Face Subset puts the band's faces in
        a GeomSubset, so Bind Material can give the band its own material. Show Field tints
        the meshes so you can see it.
        """,
        requirements=["numpy>=2,<3"],
    )
