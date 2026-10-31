"""An Edit Target made in a change_block keeps the layer it was set to.

Inside the block nothing cooks, so the Layer chunk upstream has not made its layer yet, and
the Edit Target is not wired to it yet either. Its menu refresh took the picked layer for
one that was gone and reset it to Session Layer: the chain below cooked outside the layer it
was to author into, and that layer exported without it.
"""

from __future__ import annotations

from pxr import UsdGeom

from usd_chunks import Scene, chunk, chunk_registry


def _chain(scene):
    layer = scene.create.layer(layer_name="look")
    with scene.create.edit_target(layer, edit_target="look") as target:
        box = scene.create.cube(prim_path="/box", name="box_mesh")
        scene.create.set_display_color(box, prim_path="/box", color_r=1.0, color_g=0.0, color_b=0.0,
                                       name="red")
    return target


def _targets(scene):
    lm = chunk_registry.ChunkLayerManager()
    return {c.name: lm.get_chunk_target(c.get_hash()).GetDisplayName()
            for c in scene.get_chunks() if c.name in ("box_mesh", "red")}


def test_an_edit_target_made_in_a_change_block_keeps_its_layer() -> None:
    with Scene() as scene:
        with chunk.change_block():
            target = _chain(scene)
        assert target.get_param("edit_target").value == "look"
        assert _targets(scene) == {"box_mesh": "look", "red": "look"}
        color = UsdGeom.Gprim(scene.get_stage().GetPrimAtPath("/box")).GetDisplayColorAttr().Get()[0]
        assert tuple(color) == (1.0, 0.0, 0.0)


def test_a_layer_that_never_appears_still_falls_back_after_the_block() -> None:
    with Scene() as scene:
        with chunk.change_block():
            target = scene.create.edit_target(edit_target="nowhere")
        scene.create.cube(target, prim_path="/box")
        assert target.get_param("edit_target").value == "Session Layer"
