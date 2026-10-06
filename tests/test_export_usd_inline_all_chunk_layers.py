"""A baked layer export keeps every chunk layer's opinions, not only the last one's.

The export flattened each chunk-owned sublayer of the exported layer on its own and copied
its root prims in. A chunkgroup brings a second graph layer, so the layer had two: the
second copy of /ball replaced the first, and the render mesh's purpose was lost.
"""

from __future__ import annotations

from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene
from usd_chunks.chunk_registry import ChunkRegistry


def _layer(build):
    layer = Sdf.Layer.CreateAnonymous()
    stage = Usd.Stage.Open(layer)
    build(stage)
    return layer


def test_opinions_from_two_chunk_layers_both_survive() -> None:
    with Scene():
        export = ChunkRegistry().get_chunk_module("export_usd").module
        mesh = _layer(lambda st: UsdGeom.Mesh.Define(st, "/ball/geo/render"))
        purpose = _layer(lambda st: UsdGeom.Imageable(st.OverridePrim("/ball/geo/render")).CreatePurposeAttr("render"))
        dst = Sdf.Layer.CreateAnonymous()
        Sdf.CreatePrimInLayer(dst, "/ball").specifier = Sdf.SpecifierDef  # the layer's own content
        dst.subLayerPaths = [purpose.identifier, mesh.identifier]

        export._inline_internal_sublayers(dst, {purpose.identifier, mesh.identifier})

        stage = Usd.Stage.Open(dst)
        prim = stage.GetPrimAtPath("/ball/geo/render")
        assert prim.GetTypeName() == "Mesh"
        assert UsdGeom.Imageable(prim).GetPurposeAttr().Get() == "render"
        assert stage.GetPrimAtPath("/ball").GetSpecifier() == Sdf.SpecifierDef, "own content kept"
        assert list(dst.subLayerPaths) == []
