"""Render (Command mode) hands the renderer a file that still finds the network's assets.

A file the network names relative to its folder ("terrain.usdc", beside the network) is
found through the network's folder. The flattened stage for usdrecord was written next to
the root layer's file, and a network built with Scene.new(path=...) has none until it is
saved over: so it went to the temp folder, and usdrecord rendered without the asset.
"""
from __future__ import annotations

import os

from pxr import Usd, UsdGeom

from usd_chunks.chunk_registry import ChunkRegistry
from usd_chunks.chunk_script import Scene


def _asset(folder):
    stage = Usd.Stage.CreateNew(str(folder / "terrain.usda"))
    UsdGeom.Mesh.Define(stage, "/terrain/ground")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/terrain"))
    stage.Save()


def test_the_flattened_stage_resolves_a_file_beside_the_network(tmp_path):
    _asset(tmp_path)
    with Scene.new(path=str(tmp_path / "net.usda")) as scene:
        module = ChunkRegistry().get_chunk_module("render").module
        scene.create.reference(file_path="terrain.usda", primpath_location="/World/terrain")
        scene.save()
        assert scene.get_stage().GetPrimAtPath("/World/terrain/ground"), "the network itself finds it"

        path = module._export_for_render(scene.get_stage())
        try:
            assert os.path.dirname(path) == str(tmp_path)
            assert Usd.Stage.Open(path).GetPrimAtPath("/World/terrain/ground"), "the renderer's file finds it too"
        finally:
            os.remove(path)
