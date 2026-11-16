"""A script building inside change_block() gets no "no prim at" warning for a prim that exists.

Inside the block each chunk cooks once as it is made, before it is wired, so a material
chunk made after the mesh it binds did not see the mesh yet and warned -- though the cook at
the block's end bound it fine. Only a path still missing then is worth a warning.
"""
from __future__ import annotations

import logging

from pxr import UsdShade

from usd_chunks import Scene, chunk


def _copies_with_a_material(scene, target):
    with chunk.change_block():
        last = scene.create.prim(primpath_location="/World/urn", prim_type="Xform")
        last = scene.create.sphere(last, prim_path="/World/urn/mound")
        last = scene.create.duplicate_prim(last, source_prim="/World/urn", dest_path="/World/urns", copies=3,
                                           copy_translate=(3, 0, 0))
        last = scene.create.prune_prim(last, prim_path="/World/urn")
        last = scene.create.preview_surface(last, mat_path="/World/looks/green", primpath_location=target)
    scene.set_output(last)


def test_a_binding_made_in_a_block_does_not_warn(caplog) -> None:
    with Scene() as scene, caplog.at_level(logging.WARNING):
        _copies_with_a_material(scene, ",".join(f"/World/urns/urn_{i:02d}/mound" for i in range(3)))
        mound = scene.get_stage().GetPrimAtPath("/World/urns/urn_02/mound")
        material, _ = UsdShade.MaterialBindingAPI(mound).ComputeBoundMaterial()
        assert material and material.GetPath() == "/World/looks/green"
    assert "no prim at" not in caplog.text


def test_a_path_still_missing_at_the_end_of_the_block_warns(caplog) -> None:
    with Scene() as scene, caplog.at_level(logging.WARNING):
        _copies_with_a_material(scene, "/World/urns/urn_9/mound")
    assert "no prim at /World/urns/urn_9/mound" in caplog.text
