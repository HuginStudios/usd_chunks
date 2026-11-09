"""Duplicate Prim can make its copies USD instances instead of deep copies.

Every copy was a full copy of the source's subtree: a row of 200 chairs was 200 chairs of
prims, and recolouring the source left the copies behind.
"""
from __future__ import annotations

import logging

from pxr import Gf, Usd, UsdGeom

from usd_chunks import Scene


def _chair(scene):
    """A chair with a part under it: what instancing shares."""
    chair = scene.create.prim(primpath_location="/World/chair", prim_type="Xform")
    return scene.create.cube(chair, prim_path="/World/chair/seat")


def test_copies_are_instances_of_one_prototype() -> None:
    with Scene() as scene:
        row = scene.create.duplicate_prim(_chair(scene), source_prim="/World/chair", dest_path="/World/row",
                                          copies=5, copy_translate=(2.0, 0.0, 0.0), as_instances=True)
        scene.set_output(row)
        stage = scene.get_stage()
        copies = stage.GetPrimAtPath("/World/row").GetChildren()
        assert [p.GetName() for p in copies] == [f"chair_{i:02d}" for i in range(5)]
        assert all(p.IsInstance() for p in copies)
        assert len(stage.GetPrototypes()) == 1, "the copies do not share one prototype"
        for i, p in enumerate(copies):
            assert p.GetChild("seat").IsInstanceProxy()
            at = UsdGeom.Xformable(p).ComputeLocalToWorldTransform(0).ExtractTranslation()
            assert Gf.IsClose(at, Gf.Vec3d(2.0 * i, 0, 0), 1e-6), (i, at)


def test_reopened_and_exported_they_are_still_instances(tmp_path) -> None:
    net, out = str(tmp_path / "net.usda"), str(tmp_path / "result.usda")
    with Scene() as scene:
        scene.set_output(scene.create.duplicate_prim(_chair(scene), source_prim="/World/chair", dest_path="/World/row",
                                                     copies=3, copy_translate=(2.0, 0.0, 0.0), as_instances=True))
        scene.save(net)
        scene.export(out)
    with Scene.open(net) as scene:
        reopened = scene.get_stage()
        assert all(p.IsInstance() for p in reopened.GetPrimAtPath("/World/row").GetChildren())
        assert len(reopened.GetPrototypes()) == 1
    exported = Usd.Stage.Open(out)
    assert all(p.IsInstance() for p in exported.GetPrimAtPath("/World/row").GetChildren())
    assert exported.GetPrimAtPath("/World/row/chair_02/seat").IsValid()


def test_one_instance_follows_the_source() -> None:
    with Scene() as scene:
        twin = scene.create.duplicate_prim(_chair(scene), source_prim="/World/chair", dest_path="/World/twin",
                                           as_instances=True)
        scene.set_output(twin)
        prim = scene.get_stage().GetPrimAtPath("/World/twin")
        assert prim.IsInstance() and prim.GetTypeName() == "Xform"
        assert prim.GetChild("seat").IsValid()


def test_instancing_a_leaf_warns(caplog) -> None:
    with Scene() as scene, caplog.at_level(logging.WARNING):
        leaf = scene.create.cube(prim_path="/World/step")
        scene.set_output(scene.create.duplicate_prim(leaf, source_prim="/World/step", dest_path="/World/twin",
                                                     as_instances=True))
    assert any("has no children" in r.getMessage() for r in caplog.records)
