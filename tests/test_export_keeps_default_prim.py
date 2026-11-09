"""An export keeps a default prim that one of the layer's sublayers defines.

Stripping the network from an exported layer cleared any defaultPrim the layer did not
define itself. A shot that only sublayers its set and department layers names the set's
/World, so every exported shot lost its default prim and a Reference to it brought in
nothing.
"""
from __future__ import annotations

from pxr import Sdf

from usd_chunks import chunk_stage


def _set(tmp_path) -> None:
    layer = Sdf.Layer.CreateNew(str(tmp_path / "set.usda"))
    Sdf.CreatePrimInLayer(layer, "/World").specifier = Sdf.SpecifierDef
    layer.Save()


def test_a_sublayered_default_prim_is_kept(tmp_path) -> None:
    _set(tmp_path)
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot.subLayerPaths = ["set.usda"]
    shot.defaultPrim = "World"
    chunk_stage.strip_chunks(shot)
    assert shot.defaultPrim == "World"


def test_one_that_names_nothing_still_goes(tmp_path) -> None:
    _set(tmp_path)
    shot = Sdf.Layer.CreateNew(str(tmp_path / "shot.usda"))
    shot.subLayerPaths = ["set.usda"]
    shot.defaultPrim = "chunks"  # the network's scope, stripped
    chunk_stage.strip_chunks(shot)
    assert not shot.defaultPrim
