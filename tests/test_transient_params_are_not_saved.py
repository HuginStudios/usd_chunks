"""A TRANSIENT param stays off the chunk prim, however the chunk was made.

A scripted chunk has its values before its prim exists, and the pass that writes them onto
the new prim skipped the TRANSIENT check every other writer makes. A Layer chunk's anon
layer id was saved; reopened in the same process, the Layer chunk found the closed scene's
layer by that id and adopted it -- with that scene's internal layers still sublayered in,
which a per-layer export then wrote out as dangling anon: references.
"""

from __future__ import annotations

from pxr import Sdf

from usd_chunks import Scene, chunk_utils
from usd_chunks.chunk_stage import get_chunk_prim_path


def test_a_scripted_layer_chunk_saves_no_layer_id(tmp_path) -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="model")
        with scene.create.edit_target(layer, edit_target="model"):
            scene.create.cube(prim_path="/world/box", name="box")
        prim = scene.get_stage().GetPrimAtPath(get_chunk_prim_path(layer))
        for name in ("_new_layer_id", "_layer_name_used", "_parent_layer_id"):
            assert not prim.GetAttribute(f"inputs:{name}"), name
        scene.save(str(tmp_path / "scene.usda"))
    assert "_new_layer_id" not in Sdf.Layer.FindOrOpen(str(tmp_path / "scene.usda")).ExportToString()


def test_a_reopen_in_the_same_process_makes_its_own_layer(tmp_path) -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="model")
        with scene.create.edit_target(layer, edit_target="model"):
            scene.create.cube(prim_path="/world/box", name="box")
        first = next(lyr for name, lyr in chunk_utils.user_layers(scene.get_stage()) if name == "model")
        first_id = first.identifier  # held: the layer stays alive past the scene
        scene.save(str(tmp_path / "scene.usda"))

    with Scene.open(str(tmp_path / "scene.usda")) as scene:
        stage = scene.get_stage()
        reopened = next(lyr for name, lyr in chunk_utils.user_layers(stage) if name == "model")
        assert reopened.identifier != first_id
        internal = chunk_utils.chunk_internal_layer_ids()
        named = {lyr.identifier for _n, lyr in chunk_utils.user_layers(stage)}
        for path in reopened.subLayerPaths:
            assert path in internal or path in named, f"{path} is left over from the closed scene"
    assert first is not None
