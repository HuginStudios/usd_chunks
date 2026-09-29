"""Painted density has to be visible on a real asset, not only on a bare mesh.

`displayColor` is the fallback a surface uses when nothing else has an opinion, and a bound
material always has one. So on any asset with a shader on it -- which is every asset anyone
actually scatters onto -- the brush authored the weights correctly and the surface never
changed. Reported on a photogrammetry rock: the strokes landed and there was nothing to see.
"""

from __future__ import annotations

import importlib

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from usd_chunks import chunk_registry
from usd_chunks.gizmo import runtime as chunk_gizmo
from usd_chunks.chunk_core import ChunkConfig
from usd_chunks.chunk_scene import SceneSender

brush_mod = importlib.import_module("chunks.modelling.scatter_brush")


class _DummyScene(SceneSender):
    def get_stage(self):
        return None

    def get_selected_prims(self):
        return []


def _textured_stage():
    """The asset in a weaker layer than the chunk's own, which is how a scene is composed.

    Layer strength is the whole mechanism here: a chunk layer overrides what is beneath it and
    nothing above it, so this has to be built the way a real scene is or the test would prove
    the opposite of what it claims.
    """
    asset = Sdf.Layer.CreateAnonymous("asset.usda")
    asset_stage = Usd.Stage.Open(asset)
    mesh = UsdGeom.Mesh.Define(asset_stage, "/World/Rock")
    mesh.GetPointsAttr().Set([Gf.Vec3f(0, 0, 0), Gf.Vec3f(4, 0, 0), Gf.Vec3f(4, 4, 0), Gf.Vec3f(0, 4, 0)])
    mesh.GetFaceVertexCountsAttr().Set([4])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])
    material = UsdShade.Material.Define(asset_stage, "/World/Looks/Rock")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

    root = Sdf.Layer.CreateAnonymous("root.usda")
    root.subLayerPaths = [asset.identifier]
    stage = Usd.Stage.Open(root)

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_Staged(), "pytest")
    return stage


def _brush(stage, **params):
    c = brush_mod.main()
    c._config = ChunkConfig(module="scatter_brush", gizmos=brush_mod.config().gizmos)
    c.get_param("surface_prim").value = "/World/Rock"
    for name, value in params.items():
        c.get_param(name).value = value
    return c


def _bound_material(stage):
    prim = stage.GetPrimAtPath("/World/Rock")
    material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
    return material.GetPath() if material else None


def _stroke(chunk_obj, *points):
    return chunk_gizmo.apply_stroke(
        chunk_obj, "paint", [(Gf.Vec3d(*p), 1.0) for p in points]
    )


def test_the_material_is_bound_before_anyone_paints() -> None:
    """The premise. Without this the rest of the file could pass for the wrong reason."""
    stage = _textured_stage()
    assert _bound_material(stage) == Sdf.Path("/World/Looks/Rock")


def test_painting_lets_the_tint_through_the_material() -> None:
    stage = _textured_stage()
    c = _brush(stage, radius=5.0, strength=1.0)

    _stroke(c, (2.0, 2.0, 0.0))

    assert _bound_material(stage) is None, "the material still wins, so the paint is invisible"
    tint = UsdGeom.Gprim(stage.GetPrimAtPath("/World/Rock")).GetDisplayColorAttr().Get()
    assert tint and max(c[2] for c in tint) > 0.5, "the blue paint should reach displayColor"


def test_unticking_show_paint_gives_the_asset_back() -> None:
    """Nothing is destroyed: the block is an empty relationship in the layer the chunk writes
    to, removed by name -- the same way the tint is."""
    stage = _textured_stage()
    c = _brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (2.0, 2.0, 0.0))

    c.get_param("show_paint").value = False

    assert _bound_material(stage) == Sdf.Path("/World/Looks/Rock")


def test_the_asset_layer_is_never_touched() -> None:
    """The override goes above the asset, never into it. If it ever landed on the asset's own
    layer the material would be gone for good and unticking would have nothing to undo."""
    stage = _textured_stage()
    c = _brush(stage, radius=5.0, strength=1.0)
    _stroke(c, (2.0, 2.0, 0.0))

    asset_layer = stage.GetRootLayer().subLayerPaths[-1]
    spec = Sdf.Layer.Find(asset_layer).GetPrimAtPath("/World/Rock")
    assert list(spec.properties["material:binding"].targetPathList.GetAddedOrExplicitItems()) == [
        Sdf.Path("/World/Looks/Rock")
    ]


def test_the_paint_still_lands_when_there_is_nothing_to_unbind() -> None:
    """An unmaterialled mesh gets the same treatment and is none the worse for it."""
    stage = Usd.Stage.CreateInMemory()
    mesh = UsdGeom.Mesh.Define(stage, "/World/Rock")
    mesh.GetPointsAttr().Set([Gf.Vec3f(0, 0, 0), Gf.Vec3f(4, 0, 0), Gf.Vec3f(4, 4, 0), Gf.Vec3f(0, 4, 0)])
    mesh.GetFaceVertexCountsAttr().Set([4])
    mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])

    class _Staged(_DummyScene):
        def get_stage(self):
            return stage

    chunk_registry.ChunkRegistry().initialize(_Staged(), "pytest")
    c = _brush(stage, radius=5.0, strength=1.0)

    # On a corner, so a vertex sits at the centre of the dab and takes the full strength.
    assert _stroke(c, (0.0, 0.0, 0.0)) is True
    weights = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/World/Rock")).GetPrimvar(
        brush_mod.DENSITY_PRIMVAR
    )
    assert max(weights.ComputeFlattened()) > 0.9
