"""Proximity Field chunk -- a 0..1 primvar on meshes from how near they are to other points.

Ecosystem affinity / repulsion, as a field. It reads the points of something
already placed -- usually another Scatter Points' instancer -- and writes a per-vertex float
primvar onto the meshes: **Inside** near a point, **Outside** beyond **Radius**, a smooth
fade between. Wired into a later Scatter Points' **Density Field**, it is how a biome nests:
shrubs crowd under the trees (Inside 1, Outside 0.2), grass thins in their shade (Inside
0.2, Outside 1), and **Clear Radius** keeps either off the trunk itself.

* **Scale With Source** -- each point's Radius and Clear Radius grow with that instance's
  scale, so a big tree throws a bigger understory than a sapling;
* **Along Ground** -- distance is measured across the ground (the stage's up axis is ignored),
  since an instancer's points are at the base of a trunk but the canopy is overhead;
* **Multiply With** -- an existing primvar on the same meshes (a Noise Field, say) the result
  is multiplied into, so clumping and nesting combine into the one field a Scatter reads.

Resolution is mesh resolution: the field is per vertex and Scatter samples the nearest one,
so a single-quad ground has one value per corner. Subdivide the ground to the size of the
smallest ring you want to see.

The weighting itself is usd_chunks.proximity, which Scatter Points' Grow Near also uses --
reach for that when the field doesn't need to be seen, painted or combined.

Requires: pxr (OpenUSD), usd_chunks, numpy.
"""

from __future__ import annotations

import logging

import numpy as np
from pxr import Sdf, Usd, UsdGeom, Vt

from usd_chunks import chunk, chunk_stage, proximity, chunk_utils

logger = logging.getLogger(__name__)


def _report(node, text: str) -> None:
    if (readout := node.get_param("readout")) is not None and readout.value != text:
        readout.value = text


def _base_values(prim, name, count):
    """The *name* primvar on *prim*, flattened to one value per vertex, or None."""
    if not name:
        return None
    pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar(name)
    if not pv or not pv.HasValue():
        logger.warning("%s has no primvars:%s to multiply with", prim.GetPath(), name)
        return None
    values = np.asarray(pv.ComputeFlattened(), dtype=np.float64)
    if len(values) == 1:
        return np.full(count, values[0])
    if len(values) < count:
        logger.warning("%s has %d %s values for %d points; ignoring it", prim.GetPath(), len(values), name, count)
        return None
    return values[:count]


def execute(node) -> None:
    stage = node.get_stage()
    layer = node.get_edit_context()
    if stage is None or layer is None:
        return
    is_f, is_s, is_b = chunk.is_param_t(float), chunk.is_param_t(str), chunk.is_param_t(bool)
    paths = [p.strip() for p in node.get_param("prim_path", is_s).value.split(",") if p.strip()]
    source_paths = [p.strip() for p in node.get_param("source_prims", is_s).value.split(",") if p.strip()]
    name = node.get_param("primvar", is_s).value.strip() or "proximity"
    base_name = node.get_param("base_primvar", is_s).value.strip()
    if not paths:
        _report(node, "set the meshes to paint")
        return
    scene = node.get_scene()
    upstream = chunk_stage.get_upstream_stage(stage, scene, node) if scene is not None else stage
    sources, scales = proximity.source_points(upstream, source_paths)
    if not node.get_param("scale_with_source", is_b).value:
        scales = np.ones(len(sources))
    up = None
    if node.get_param("along_ground", is_b).value:
        up = (0.0, 1.0, 0.0) if UsdGeom.GetStageUpAxis(stage).upper() == "Y" else (0.0, 0.0, 1.0)
    settings = dict(radius=max(0.0, node.get_param("radius", is_f).value),
                    clear=max(0.0, node.get_param("clear_radius", is_f).value),
                    softness=node.get_param("softness", is_f).value,
                    inside=node.get_param("inside", is_f).value,
                    outside=node.get_param("outside", is_f).value,
                    up=up)
    weight = proximity.Proximity(sources, scales, **settings)
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    painted, total, lit = 0, 0, 0.0
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
                values = weight.sample_many(world)
                base = _base_values(prim, base_name, len(values))
                if base is not None:
                    values = values * np.clip(base, 0.0, 1.0)
                out = UsdGeom.PrimvarsAPI(stage.OverridePrim(prim.GetPath()))
                pv = out.CreatePrimvar(name, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
                pv.Set(Vt.FloatArray.FromNumpy(values.astype(np.float32)))
                painted += 1
                total += len(values)
                lit += float(values.sum())
    if not painted:
        _report(node, "no meshes found to paint")
        return
    _report(node, f"primvars:{name} on {painted} mesh{'es' if painted != 1 else ''} · "
                  f"{len(sources)} source points · {100.0 * lit / max(total, 1):.0f}% covered")


def main() -> chunk.Chunk:
    chunk_api = chunk.Chunk("Proximity Field")
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api.add_param(chunk.LineEdit(
        "prim_path", "Meshes", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="Meshes (or parents of meshes) to paint -- the ground the next layer scatters on.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "source_prims", "Near", flags=labeled | chunk.ChunkFlags.ParamFlags.PRIM_SELECTION,
        tooltip="What the field is measured from: a Scatter Points' instancer (its instances), "
        "or any points. Comma-separate several.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "primvar", "Primvar", default_value="proximity", flags=labeled,
        tooltip="Name of the float primvar written. Wire it into a Scatter Points' Density Field.",
    ))
    chunk_api.add_param(chunk.LineEdit(
        "base_primvar", "Multiply With", flags=labeled,
        tooltip="A primvar already on the meshes (a Noise Field's) to multiply the result into. "
        "Empty: proximity alone.",
    ))
    chunk_api.add_param(chunk.Slider("radius", "Radius", min_value=0.0, max_value=20.0, default_value=3.0,
                                     tooltip="How far a source reaches, scene units -- a canopy's radius."))
    chunk_api.add_param(chunk.Slider("clear_radius", "Clear Radius", min_value=0.0, max_value=5.0, default_value=0.0,
                                     tooltip="Nothing within this of a source -- the trunk. 0 = no clearing."))
    chunk_api.add_param(chunk.Slider("softness", "Softness", min_value=0.0, max_value=1.0, default_value=0.5,
                                     tooltip="How much of the Radius fades out, from the edge in. 0 = a hard edge."))
    chunk_api.add_param(chunk.Slider("inside", "Inside", min_value=0.0, max_value=1.0, default_value=1.0,
                                     tooltip="The field near a source. 1 draws the next layer in (understory)."))
    chunk_api.add_param(chunk.Slider("outside", "Outside", min_value=0.0, max_value=1.0, default_value=0.0,
                                     tooltip="The field beyond Radius. Higher than Inside pushes it away (shade)."))
    chunk_api.add_param(chunk.Checkbox("scale_with_source", "Scale With Source", default_value=True,
                                       tooltip="Each instance's Radius and Clear Radius grow with its scale."))
    chunk_api.add_param(chunk.Checkbox("along_ground", "Along Ground", default_value=True,
                                       tooltip="Measure distance across the ground, ignoring height: a "
                                       "canopy shades what is under it however tall the tree is."))
    chunk_api.add_param(chunk.Label("readout", "nothing painted yet", default_value="nothing painted yet"))
    return chunk_api


def config() -> chunk.DefaultConfig:
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        description="Paints a 0..1 primvar on meshes from how near they are to other points -- "
        "nests one Scatter Points layer under or around another.",
        requirements=["numpy>=2,<3"],
    )
