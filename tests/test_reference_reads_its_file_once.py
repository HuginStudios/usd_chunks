"""A Reference or Sublayer reads the file it brings in once per cook, not twice.

network_target opens the file to ask whether it holds a network, and let it go; the arc
the chunk authored next opened it all over again. The chunk now holds the file open, so
the arc finds it: on the bathroom scene that was half a second of a five-second open.
"""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from usd_chunks import Scene, chunk_stage


@pytest.fixture
def still_open_after_check(monkeypatch):
    """For each network check a cook makes, whether its file was still open afterwards."""
    seen = []
    real = chunk_stage.network_target

    def checking(asset_path, import_path=""):
        result = real(asset_path, import_path)
        seen.append(Sdf.Layer.Find(asset_path) is not None)
        return result

    monkeypatch.setattr(chunk_stage, "network_target", checking)
    return seen


def _asset(path) -> str:
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.Cube.Define(stage, "/Asset").GetPrim().CreateAttribute("tag", Sdf.ValueTypeNames.String).Set("asset")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/Asset"))
    stage.GetRootLayer().Save()
    return str(path)


def test_a_reference_keeps_its_file_open_for_the_arc(tmp_path, still_open_after_check) -> None:
    asset = _asset(tmp_path / "asset.usda")
    with Scene() as scene:
        scene.create.reference(file_path=asset, primpath_location="/World/asset")
        assert scene.get_stage().GetPrimAtPath("/World/asset").GetAttribute("tag").Get() == "asset"
    assert still_open_after_check and all(still_open_after_check)


def test_a_sublayer_keeps_its_file_open_for_the_stack(tmp_path, still_open_after_check) -> None:
    asset = _asset(tmp_path / "asset.usda")
    with Scene() as scene:
        sub = scene.create.sublayer()
        imports = sub.get_param("imports")
        imports.size = 1
        imports.get_indexed_param("sublayer_path", 0).value = asset
        assert scene.get_stage().GetPrimAtPath("/Asset").GetTypeName() == "Cube"
    assert still_open_after_check and all(still_open_after_check)
