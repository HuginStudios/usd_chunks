"""A copy's relationships and connections into its own subtree point at the copy, not the source.

Copying an urn with a PointInstancer of flowers under it left each copy's instancer pointing
at the source's prototypes; pruning the source then emptied every copy. A material copied with
the object it shades also lost the wiring between its shaders.
"""
from __future__ import annotations

import pytest
from pxr import Sdf, UsdGeom, UsdShade

from usd_chunks import Scene


def _urn(stage):
    """An urn with a flower instancer whose prototype lives under the urn, and a material with a
    shader connection, also under it; plus a relationship to something outside it."""
    urn = UsdGeom.Xform.Define(stage, "/World/urn").GetPrim()
    UsdGeom.Sphere.Define(stage, "/World/urn/flowers/Prototypes/bloom")
    UsdGeom.Sphere.Define(stage, "/World/elsewhere")
    instancer = UsdGeom.PointInstancer.Define(stage, "/World/urn/flowers")
    instancer.CreatePrototypesRel().SetTargets(["/World/urn/flowers/Prototypes/bloom"])
    instancer.CreateProtoIndicesAttr([0, 0])
    instancer.CreatePositionsAttr([(0, 0, 0), (1, 0, 0)])
    material = UsdShade.Material.Define(stage, "/World/urn/look")
    shader = UsdShade.Shader.Define(stage, "/World/urn/look/surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    urn.CreateRelationship("watched").SetTargets(["/World/elsewhere", "/World/urn/look"])
    return urn


@pytest.mark.parametrize("copies", [1, 3])
def test_paths_into_the_copied_subtree_follow_the_copy(copies) -> None:
    with Scene() as scene:
        stage = scene.get_stage()
        _urn(stage)
        dest = "/World/urns" if copies > 1 else "/World/urn_copy"
        dup = scene.create.duplicate_prim(source_prim="/World/urn", dest_path=dest, copies=copies,
                                          copy_translate=(2.0, 0.0, 0.0))
        scene.create.prune_prim(dup, prim_path="/World/urn")
        roots = [f"{dest}/urn_{i:02d}" for i in range(copies)] if copies > 1 else [dest]
        for root in roots:
            instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath(f"{root}/flowers"))
            assert instancer.GetPrototypesRel().GetTargets() == [Sdf.Path(f"{root}/flowers/Prototypes/bloom")]
            assert stage.GetPrimAtPath(f"{root}/flowers/Prototypes/bloom")
            output = UsdShade.Material(stage.GetPrimAtPath(f"{root}/look")).GetSurfaceOutput()
            sources = output.GetAttr().GetConnections()
            assert sources == [Sdf.Path(f"{root}/look/surface.outputs:surface")]
            watched = stage.GetPrimAtPath(root).GetRelationship("watched").GetTargets()
            assert watched == [Sdf.Path("/World/elsewhere"), Sdf.Path(f"{root}/look")]
