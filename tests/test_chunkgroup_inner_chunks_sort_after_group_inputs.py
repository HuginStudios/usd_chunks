"""A chunk inside a chunkgroup gets a layer stronger than what the group depends on.

Behind an Edit Target, a group's chunks now share the graph layer of the chunks feeding the
group, sorted by dependency depth. The first chunk in a group has no inputs of its own, so
it measured 0 and its layer sorted under them: whatever it changed on their prims lost. A
Poly Reduce in a group left the mesh it reduced at full resolution.
"""

from __future__ import annotations

from pxr import UsdGeom

from usd_chunks import Scene
from usd_chunks.chunk_scheduler import get_depth, layer_sort_key


def test_a_group_wins_over_what_feeds_it() -> None:
    with Scene() as scene:
        layer = scene.create.layer(layer_name="look")
        with scene.create.edit_target(layer, edit_target="look"):
            box = scene.create.cube(prim_path="/box", name="box_mesh")
            red = scene.create.set_display_color(box, prim_path="/box", color_r=1.0, color_g=0.0,
                                                 color_b=0.0, name="red")
            group = scene.create.chunkgroup(red, name="recolor")
            with group:
                blue = scene.create.set_display_color(prim_path="/box", color_r=0.0, color_g=0.0,
                                                      color_b=1.0, name="blue")

        assert layer_sort_key(blue.get_dag_node()) > layer_sort_key(red.get_dag_node())
        color = UsdGeom.Gprim(scene.get_stage().GetPrimAtPath("/box")).GetDisplayColorAttr().Get()
        assert tuple(color[0]) == (0.0, 0.0, 1.0), "the group's colour wins"


def test_outside_a_group_the_key_is_the_depth() -> None:
    with Scene() as scene:
        box = scene.create.cube(prim_path="/box", name="box_mesh")
        red = scene.create.set_display_color(box, prim_path="/box", color_r=1.0, name="red")
        for c in (box, red):
            assert layer_sort_key(c.get_dag_node()) == get_depth(c.get_dag_node())
