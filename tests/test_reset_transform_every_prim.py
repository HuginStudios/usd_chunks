"""Reset Transform clears every prim its Prim param names, not just the first.

Given "/a,/b" or a pattern like "/protos/*", it cleared /a and left the rest where they were.
"""
from __future__ import annotations

from pxr import Gf, UsdGeom

from usd_chunks import Scene


def test_every_named_prim_goes_back_to_its_origin():
    with Scene() as scene:
        stage = scene.get_stage()
        for i, path in enumerate(("/protos/a", "/protos/b", "/protos/c"), start=1):
            UsdGeom.Xform.Define(stage, path).AddTranslateOp().Set(Gf.Vec3d(i, 0, 0))

        scene.create.reset_transform(prim_path="/protos/a,/protos/b")
        scene.create.reset_transform(prim_path="/protos/c")

        for path in ("/protos/a", "/protos/b", "/protos/c"):
            assert UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation() == Gf.Matrix4d(1), path


def test_a_pattern_resets_every_match():
    with Scene() as scene:
        stage = scene.get_stage()
        for i, name in enumerate("xyz", start=1):
            UsdGeom.Xform.Define(stage, f"/set/{name}").AddTranslateOp().Set(Gf.Vec3d(0, i, 0))

        scene.create.reset_transform(prim_path="/set/*")

        for name in "xyz":
            assert not UsdGeom.Xformable(stage.GetPrimAtPath(f"/set/{name}")).GetOrderedXformOps(), name
