"""Build the styles in chunks/styles: looks you put on an object in one go.

    USD_CHUNK_DIRS=chunks python examples/styles/build_styles.py [out_dir]

Writes worn_paint.usda, dirty_stone.usda, rubble.usda and hanging_vines.usda (to chunks/styles
by default, where the Styles menu finds them). A style is a ChunkGroup whose prim paths are
written against "/", the object it goes on: a Reference with that object as its Location
resolves them there (see usd_chunks/styles.py). Everything a style makes goes under the
object, so the same style on two objects doesn't collide.

The pictures beside them are made by render_thumbnails.py. The .usda files are made by this
script: change a style here and run it again.
"""
from __future__ import annotations

import os
import sys

from usd_chunks import Scene, chunk_stage

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "chunks", "styles")

#: Where a style's own prims go: under the object, whatever it is. A param that names prims
#: (a PRIM_SELECTION or primpath_location one) is resolved under the object by the host, so
#: it is written "/rubble"; any other path param reads the object's path from this.
OBJECT = "@{}.prim_path@"


def _material(name: str, body: str) -> str:
    """A MaterialX material at /materials/<name>: shared by every object wearing the style."""
    return f"""#usda 1.0
def Scope "materials" {{
    def Material "{name}" {{
        token outputs:mtlx:surface.connect = </materials/{name}/surface.outputs:out>
{body}
    }}
}}
"""


def _reader(name: str, primvar: str) -> str:
    return f"""        def Shader "{name}" {{
            uniform token info:id = "ND_geompropvalue_float"
            string inputs:geomprop = "{primvar}"
            float outputs:out
        }}"""


WORN_PAINT = _material("worn_paint", _reader("wear", "wear") + """
        def Shader "colour" {
            uniform token info:id = "ND_mix_color3"
            color3f inputs:bg = (0.05, 0.22, 0.45)
            color3f inputs:fg = (0.74, 0.7, 0.62)
            float inputs:mix.connect = </materials/worn_paint/wear.outputs:out>
            color3f outputs:out
        }
        def Shader "rough" {
            uniform token info:id = "ND_mix_float"
            float inputs:bg = 0.55
            float inputs:fg = 0.3
            float inputs:mix.connect = </materials/worn_paint/wear.outputs:out>
            float outputs:out
        }
        def Shader "surface" {
            uniform token info:id = "ND_standard_surface_surfaceshader"
            color3f inputs:base_color.connect = </materials/worn_paint/colour.outputs:out>
            float inputs:specular_roughness.connect = </materials/worn_paint/rough.outputs:out>
            token outputs:out
        }""")

DIRTY_STONE = _material("dirty_stone", _reader("chips", "chips") + "\n" + _reader("dirt", "dirt") + """
        def Shader "chipped" {
            uniform token info:id = "ND_mix_color3"
            color3f inputs:bg = (0.42, 0.41, 0.38)
            color3f inputs:fg = (0.78, 0.76, 0.7)
            float inputs:mix.connect = </materials/dirty_stone/chips.outputs:out>
            color3f outputs:out
        }
        def Shader "colour" {
            uniform token info:id = "ND_mix_color3"
            color3f inputs:bg.connect = </materials/dirty_stone/chipped.outputs:out>
            color3f inputs:fg = (0.2, 0.12, 0.05)
            float inputs:mix.connect = </materials/dirty_stone/dirt.outputs:out>
            color3f outputs:out
        }
        def Shader "surface" {
            uniform token info:id = "ND_standard_surface_surfaceshader"
            color3f inputs:base_color.connect = </materials/dirty_stone/colour.outputs:out>
            float inputs:specular_roughness = 0.8
            token outputs:out
        }""")


def loft(group, *params) -> None:
    for param in params:
        chunk_stage.loft_param_to_context(param, group)


def worn_paint(scene) -> None:
    group = scene.create.chunkgroup(name="WornPaint", doc=(
        "Paint worn off the edges and corners, in patches.\n"
        "Paints the object blue, with the paint rubbed back to a pale undercoat where its edges "
        "bend sharply. Needs a mesh with divisions: the wear is per point.\n"
        "Params: Wear_width, how far the wear reaches from an edge; Wear_angle, how sharp an edge "
        "must be; Patches_size and Patches_threshold, the size and amount of the patches the wear "
        "breaks into."))
    with group:
        patches = scene.create.noise_field(name="Patches", prim_path="/", primvar="style_patches", size=0.3,
                                           threshold=0.45, softness=0.3)
        wear = scene.create.edge_field(patches, name="Wear", prim_path="/", primvar="wear", width=0.15,
                                       base_primvar="style_patches")
        look = scene.create.inline_usd(wear, name="Look", code=WORN_PAINT)
        bind = scene.create.bind_material(look, name="Paint", primpath_location="/",
                                          material_path="/materials/worn_paint")
        scene.set_output(bind)
    loft(group, wear.get_param("width"), wear.get_param("angle"), patches.get_param("size"),
         patches.get_param("threshold"))
    chunk_stage.export_network(scene, group, os.path.join(OUT, "worn_paint.usda"))


def dirty_stone(scene) -> None:
    group = scene.create.chunkgroup(name="DirtyStone", doc=(
        "Grey stone with dirt in the corners and pale chips on the edges.\n"
        "Dark dirt collects where faces meet inward (the foot of a step, the inside of a box), "
        "and the outer edges are chipped lighter. Needs a mesh with divisions.\n"
        "Params: Dirt_width, how far the dirt reaches from a corner; Chips_width, how wide the "
        "chipped edges are."))
    with group:
        chips = scene.create.edge_field(name="Chips", prim_path="/", primvar="chips", find="Outer Edges",
                                        width=0.06)
        dirt = scene.create.edge_field(chips, name="Dirt", prim_path="/", primvar="dirt", find="Inner Corners",
                                       width=0.2, softness=0.0)
        look = scene.create.inline_usd(dirt, name="Look", code=DIRTY_STONE)
        bind = scene.create.bind_material(look, name="Stone", primpath_location="/",
                                          material_path="/materials/dirty_stone")
        scene.set_output(bind)
    loft(group, dirt.get_param("width"), chips.get_param("width"))
    chunk_stage.export_network(scene, group, os.path.join(OUT, "dirty_stone.usda"))


def rubble(scene) -> None:
    group = scene.create.chunkgroup(name="Rubble", doc=(
        "Small stones gathered in the inner corners: the foot of a wall, the back of each step.\n"
        "Writes <object>/rubble: the stones as a PointInstancer, and the one stone they copy. "
        "Needs a mesh with divisions.\n"
        "Params: Corners_width, how far from a corner the stones spread; Scatter_spacing, how "
        "close together they are; Scatter_scale_max, the size of the biggest; Scatter_seed, "
        "another pattern."))
    with group:
        corners = scene.create.edge_field(name="Corners", prim_path="/", primvar="rubble", find="Inner Corners",
                                          width=0.15, softness=0.0)
        stone = scene.create.mesh(corners, name="Stone", mesh_type="sphere", radius1=0.03, dimx2=7, dimy2=5,
                                  prim_path=OBJECT.format("Corners") + "/rubble/stone")
        squash = scene.create.transform(stone, name="Squash", primpath_location="/rubble/stone",
                                        sx=1.2, sy=0.6, sz=0.9)
        colour = scene.create.set_display_color(squash, name="Colour", prim_path="/rubble/stone",
                                                color_r=0.2, color_g=0.19, color_b=0.17)
        scatter = scene.create.scatter_points(
            colour, name="Scatter", surface_prim="/", density_primvar="rubble",
            inst_path=OBJECT.format("Corners") + "/rubble/stones", prototypes=[{"proto_path": "/rubble/stone"}],
            auto_spacing=False, spacing=0.03, max_count=5000, scale_min=0.5, scale_max=1.6, scale_bias=2.0)
        scene.set_output(scatter)
    loft(group, corners.get_param("width"), scatter.get_param("spacing"), scatter.get_param("scale_max"),
         scatter.get_param("seed"))
    chunk_stage.export_network(scene, group, os.path.join(OUT, "rubble.usda"))


def hanging_vines(scene) -> None:
    group = scene.create.chunkgroup(name="HangingVines", doc=(
        "Leafy vines that grow along the outer edges and hang down over the sides.\n"
        "Writes <object>/vines (the stems as a mesh and as curves, and the leaves as a "
        "PointInstancer) and <object>/vine_leaf, the leaf they copy. Needs a mesh with divisions.\n"
        "Params: Ledges_width, how far from an edge the vines may grow; Vines_growth, how far "
        "they have grown (0-100); Vines_hang, how many tips hang down (%); Vines_seed, other vines."))
    with group:
        ledges = scene.create.edge_field(name="Ledges", prim_path="/", primvar="ledges", find="Outer Edges",
                                         width=0.3)
        vines = scene.create.vine(ledges, name="Vines", surface_prim="/", density_primvar="ledges",
                                  vine_path=OBJECT.format("Ledges") + "/vines", density=0.5, hang=40.0,
                                  hang_length=15.0, leaf_spacing=2.5, leaf_spread=0.4)
        stems = scene.create.curves_to_mesh(vines, name="Stems", curves_prim="@Vines.out_curves@",
                                            primpath_location="/vines/stems")
        bark = scene.create.set_display_color(stems, name="Bark", prim_path="/vines",
                                              color_r=0.22, color_g=0.16, color_b=0.1)
        leaf = scene.create.mesh(bark, name="Leaf", mesh_type="grid", dimx1=2, dimy1=2, width1=0.05,
                                 height2=0.07, grid_checker=False, prim_path=OBJECT.format("Ledges") + "/vine_leaf")
        green = scene.create.set_display_color(leaf, name="Green", prim_path="/vine_leaf",
                                               color_r=0.1, color_g=0.32, color_b=0.07)
        foliage = scene.create.scatter_points(
            green, name="Foliage", points_prim="@Vines.out_leaves@", inst_path=OBJECT.format("Ledges") + "/vines/leaves",
            prototypes=[{"proto_path": "/vine_leaf"}], scale_min=0.8, scale_max=1.3, align_up="+Z")
        scene.set_output(foliage)
    loft(group, ledges.get_param("width"), vines.get_param("growth"), vines.get_param("hang"),
         vines.get_param("seed"))
    chunk_stage.export_network(scene, group, os.path.join(OUT, "hanging_vines.usda"))


STYLES = (worn_paint, dirty_stone, rubble, hanging_vines)

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    # Lazy: nothing cooks. A style's own file holds no object to put it on.
    for build in STYLES:
        with Scene(lazy=True) as scene:
            build(scene)
    print(f"Wrote {', '.join(b.__name__ + '.usda' for b in STYLES)} in {os.path.normpath(OUT)}")
