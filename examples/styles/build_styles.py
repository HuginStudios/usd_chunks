"""Build the styles in chunks/styles: looks you put on an object in one go.

    USD_CHUNK_DIRS=chunks python examples/styles/build_styles.py [out_dir]

Writes every style below to chunks/styles (by default; where the Styles menu finds them), or
only those named after the folder. A style is a ChunkGroup whose prim paths are
written against "/", the object it goes on: a Reference with that object as its Location
resolves them there (see usd_chunks/styles.py). Everything a style makes goes under the
object, so the same style on two objects doesn't collide.

The pictures beside them are made by render_thumbnails.py. The .usda files are made by this
script: change a style here and run it again.
"""
from __future__ import annotations

import os
import sys

from pxr import Sdf

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


def _export(scene, group, name: str) -> None:
    """Write the style to OUT/name, its handles anchored at nothing.

    A handle's anchor is a prim in the scene the style was built in ("/style_ground"). In a
    style that is outside the Reference that brings it in, so USD would warn about it on every
    read; the handle finds its prim from the chunk's own params instead (gizmo/runtime.py).
    """
    path = os.path.join(OUT, name)
    chunk_stage.export_network(scene, group, path)
    layer = Sdf.Layer.FindOrOpen(path)
    anchors = []
    layer.Traverse(layer.pseudoRoot.path, lambda p: anchors.append(p) if p.IsPropertyPath() and p.name.startswith(
        "ui:gizmo:") and p.name.endswith(":anchor") else None)
    for anchor in anchors:
        spec = layer.GetRelationshipAtPath(anchor)
        if spec is not None and spec.targetPathList.explicitItems:
            spec.targetPathList.explicitItems = []
    if anchors:
        layer.Save()


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
    _export(scene, group, "worn_paint.usda")


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
    _export(scene, group, "dirty_stone.usda")


#: Where a style lays out the assets it places, one Asset Kit per kind of thing (a slot):
#: <object>/style_kit/debris, .../plants. Each takes your own assets (Files, or Prims in the
#: scene); with none, the built-in kit under BUILTIN, which the style makes itself (a
#: broken concrete block, planks) or downloads from Poly Haven (CC0) the first time.
KIT = "/style_kit"
BUILTIN = "/style_kit/builtin"

#: Built-in plants that crowd a wall or a ruin: (kit name, Poly Haven model, scale).
PLANTS = (("shrub", "shrub_04", 1.5), ("fern", "fern_02", 1.0), ("nettle", "nettle_plant", 1.6),
          ("weed", "weed_plant_02", 1.0), ("grass", "grass_bermuda_01", 1.0))
FOREST = (("fern", "fern_02", 1.2), ("branches", "dry_branches_medium_01", 1.0),
          ("mossy", "rock_moss_set_01", 0.25), ("dandelion", "dandelion_01", 1.0),
          ("grass", "grass_bermuda_01", 0.8))
LEDGE = (("grass", "grass_bermuda_01", 1.4), ("sorrel", "shrub_sorrel_01", 2.5), ("weed", "weed_plant_02", 1.0))
SCRAP = (("box", "cardboard_box_01", 1.0), ("can", "metal_trash_can", 1.0), ("bag", "trashbag", 1.0),
         ("barrel", "barrel_03", 1.0), ("crate", "wooden_crate_01", 1.0))

#: The size each slot fits your assets to (their largest side, in metres), so the style's
#: spacing suits them whatever size they were made at; 0 keeps their own size. A slot's
#: Fit param changes it.
FIT = {"Moss": 0.3, "Debris": 0.3, "Plants": 0.8, "Props": 0.0, "Junk": 0.3, "Weeds": 0.6, "Leaves": 0.15, "Assets": 0.5}


def _kit(scene, last, items) -> tuple:
    """Built-in Poly Haven models, every part at the origin: (the last chunk, their paths)."""
    paths = []
    for name, model, scale in items:
        last = scene.create.polyhaven_model(last, name=f"Kit_{name}", model=model, scale=scale,
                                            primpath_location=f"{BUILTIN}/{name}")
        paths.append(f"{BUILTIN}/{name}/{model}*")
    return last, paths


def _rubble_kit(scene, last, first: str, planks: bool = False) -> tuple:
    """Built-in rubble: concrete chunks (a block broken by Fracture, with Poly Haven's
    concrete_debris), Poly Haven stones, and planks if asked. (The last chunk, their paths.)"""
    obj = OBJECT.format(first)
    concrete = scene.create.polyhaven_material(last, name="Concrete", texture="concrete_debris",
                                               material_path="/materials/style_concrete")
    block = scene.create.mesh(concrete, name="Block", mesh_type="cube", width=0.3, height=0.18, depth=0.26,
                              prim_path=f"{obj}{BUILTIN}/block")
    pieces = scene.create.fracture(block, name="Break", source_prims=f"{BUILTIN}/block",
                                   output_path=f"{obj}{BUILTIN}/chunks", generations=3, bevel=0.3, jaggedness=0.6,
                                   outside_material="/materials/style_concrete",
                                   inside_material="/materials/style_concrete")
    # The broken block itself stands where it was made: the copies are what is seen.
    last = scene.create.property_editor(pieces, name="HideBlock", prim=f"{obj}{BUILTIN}/chunks",
                                        edits='visibility = "invisible"')
    last = scene.create.polyhaven_model(last, name="Kit_stones", model="namaqualand_stones_01", scale=2.0,
                                        primpath_location=f"{BUILTIN}/stones")
    paths = [f"{BUILTIN}/chunks/Prototypes/*", f"{BUILTIN}/stones/namaqualand_stones_01*"]
    if planks:
        plank = scene.create.mesh(last, name="Plank", mesh_type="cube", width=1.2, height=0.025, depth=0.13,
                                  prim_path=f"{obj}{BUILTIN}/plank")
        last = scene.create.polyhaven_material(plank, name="PlankWood", texture="brown_planks_05",
                                               primpath_location=f"{BUILTIN}/plank",
                                               material_path="/materials/style_planks")
        paths.append(f"{BUILTIN}/plank")
    return last, paths


def _slot(scene, last, name: str, fallback, spacing: float = 1.0) -> tuple:
    """An Asset Kit for one kind of thing the style places: your assets, or the built-in
    ones at the fallback paths, with how close a Scatter puts them in sizes of the assets.
    (The kit, the Scatter rows that copy what it lays out.)"""
    where = f"{KIT}/{name.lower()}"
    kit = scene.create.asset_kit(last, name=name, fallback=" ".join(fallback), fit=FIT[name],
                                 spacing=spacing, primpath_location=where)
    return kit, [{"proto_path": f"{where}/*"}]


def _spacing(kit) -> str:
    """A Scatter's Spacing, following the slot's: bigger assets spread out by themselves."""
    return f"@{kit.name}.out_spacing@"


def _loft_slots(group, *kits) -> None:
    for kit in kits:
        loft(group, kit.get_param("files"), kit.get_param("prims"), kit.get_param("fit"), kit.get_param("spacing"))


def _slots_doc(*slots: tuple[str, str]) -> str:
    """The doc lines for a style's slots: (slot, what goes in it)."""
    lines = [f"{name}_files / {name}_prims: your own {what}, a folder of USD files or prims in the scene; "
             f"{name}_fit, the size they are fitted to (0 keeps theirs); {name}_spacing, how close they go, "
             "in sizes of the assets." for name, what in slots]
    return ("Bring your own assets: " + " ".join(lines) + " Empty, the style uses its built-in kit (Poly "
            "Haven's, CC0, downloaded the first time).")


def _around(scene, reach=2.0, breakup=0.7):
    """The first chunk of a style that piles things around its object: an Around Field."""
    return scene.create.around_field(name="Around", prim_path="/", ground="", reach=reach, breakup=breakup,
                                     breakup_size=1.0, softness=0.8,
                                     primpath_location=OBJECT.format("Around") + "/style_ground")


def _scatter(scene, last, rows, name, spacing, scale=(0.5, 1.4), seed=0, max_count=6000, on="Around",
             primvar="around", surface="/style_ground", out=None):
    """A Scatter Points of the rows, writing <object>/style_<out>."""
    return scene.create.scatter_points(
        last, name=name, surface_prim=surface, density_primvar=primvar,
        inst_path=OBJECT.format(on) + f"/style_{out or name.lower()}", prototypes=rows, auto_spacing=False,
        spacing=spacing, max_count=max_count, scale_min=scale[0], scale_max=scale[1], scale_bias=2.5, seed=seed,
        rand_axis="Y")


AROUND_PARAMS = ("Around_ground", "Around_reach", "Around_breakup", "Around_seed")


def _loft_around(group, around):
    loft(group, around.get_param("ground"), around.get_param("reach"), around.get_param("breakup"),
         around.get_param("seed"))


def _around_doc(summary: str, what: str, *slots: tuple[str, str]) -> str:
    return (f"{summary}\n{what} Put the object on a ground and set Around_ground to it (or select the "
            "object, then the ground, before picking the style); without one the pile sits at the "
            f"object's lowest point. {_slots_doc(*slots)}\n"
            "Params: Around_reach, how far out the pile goes; Around_breakup, how patchy it is; "
            "Around_seed, another pattern.")


def concrete_debris(scene) -> None:
    group = scene.create.chunkgroup(name="ConcreteDebris", doc=_around_doc(
        "Broken concrete and stones piled around the object's base.",
        "Thickest at the walls, thinning out with distance.", ("Debris", "debris (rocks, bricks, chunks)")))
    with group:
        around = _around(scene, reach=1.8)
        last, builtin = _rubble_kit(scene, around, "Around")
        debris, rows = _slot(scene, last, "Debris", builtin, spacing=0.32)
        last = _scatter(scene, debris, rows, "Scatter", _spacing(debris), scale=(0.4, 1.6), out="debris")
        scene.set_output(last)
    _loft_around(group, around)
    _loft_slots(group, debris)
    _export(scene, group, "concrete_debris.usda")


def bushy_nature(scene) -> None:
    group = scene.create.chunkgroup(name="BushyNature", doc=_around_doc(
        "Bushes, ferns, nettles and grass crowding the object's base.",
        "Thickest at the walls.", ("Plants", "plants")))
    with group:
        around = _around(scene, reach=1.6, breakup=0.6)
        last, builtin = _kit(scene, around, PLANTS)
        plants, rows = _slot(scene, last, "Plants", builtin, spacing=0.63)
        last = _scatter(scene, plants, rows, "Scatter", _spacing(plants), scale=(0.9, 2.0), seed=7, out="plants")
        scene.set_output(last)
    _loft_around(group, around)
    _loft_slots(group, plants)
    _export(scene, group, "bushy_nature.usda")


def forest_hideout(scene) -> None:
    group = scene.create.chunkgroup(name="ForestHideout", doc=_around_doc(
        "Ferns, dry branches and mossy rocks around the object, like a forest floor.",
        "Built in: ferns, branches, mossy rocks, dandelions and grass.", ("Plants", "forest plants and rocks")))
    with group:
        around = _around(scene, reach=2.2, breakup=0.75)
        last, builtin = _kit(scene, around, FOREST)
        plants, rows = _slot(scene, last, "Plants", builtin, spacing=0.83)
        last = _scatter(scene, plants, rows, "Scatter", _spacing(plants), scale=(0.8, 1.8), seed=3, out="forest")
        scene.set_output(last)
    _loft_around(group, around)
    _loft_slots(group, plants)
    _export(scene, group, "forest_hideout.usda")


def scrap_yard(scene) -> None:
    group = scene.create.chunkgroup(name="ScrapYard", doc=_around_doc(
        "Boxes, bins, bags, barrels and crates dumped around the object, with planks and stones.",
        "Props sparse, with smaller junk between them.", ("Props", "props (boxes, barrels, bins)"),
        ("Junk", "small junk (planks, stones, cans)")))
    with group:
        around = _around(scene, reach=2.4, breakup=0.8)
        last, builtin = _kit(scene, around, SCRAP)
        props, rows = _slot(scene, last, "Props", builtin, spacing=1.0)
        last, builtin = _rubble_kit(scene, props, "Around", planks=True)
        junk, junk_rows = _slot(scene, last, "Junk", builtin, spacing=0.5)
        last = _scatter(scene, junk, rows, "PropScatter", _spacing(props), scale=(0.8, 1.2), seed=5, max_count=400, out="props")
        last = _scatter(scene, last, junk_rows, "JunkScatter", _spacing(junk), scale=(0.4, 1.3), seed=6, out="junk")
        scene.set_output(last)
    _loft_around(group, around)
    _loft_slots(group, props, junk)
    _export(scene, group, "scrap_yard.usda")


def rubble(scene) -> None:
    group = scene.create.chunkgroup(name="Rubble", doc=(
        "Broken concrete and stones gathered in the inner corners: the foot of a wall, the back of a step.\n"
        f"Needs a mesh with divisions. {_slots_doc(('Debris', 'debris'))}\n"
        "Params: Corners_width, how far from a corner the stones spread; Scatter_seed, another pattern."))
    with group:
        corners = scene.create.edge_field(name="Corners", prim_path="/", primvar="rubble", find="Inner Corners",
                                          width=0.18, softness=0.0)
        last, builtin = _rubble_kit(scene, corners, "Corners")
        debris, rows = _slot(scene, last, "Debris", builtin, spacing=0.15)
        scatter = scene.create.scatter_points(
            debris, name="Scatter", surface_prim="/", density_primvar="rubble",
            inst_path=OBJECT.format("Corners") + "/style_rubble", prototypes=rows, auto_spacing=False,
            spacing=_spacing(debris), max_count=6000, scale_min=0.15, scale_max=0.6, scale_bias=2.5, rand_axis="Y")
        scene.set_output(scatter)
    loft(group, corners.get_param("width"), scatter.get_param("seed"))
    _loft_slots(group, debris)
    _export(scene, group, "rubble.usda")


def _vines(scene, last, first: str, density_primvar: str, roots=40, hang=40.0, name="Vines") -> tuple:
    """Vine over the painted part of the object, its stems made a mesh (Aurora draws no curves),
    and leaves on its leaf points: yours (the Leaves slot) or Poly Haven's sorrel. Returns
    (the last chunk, the Leaves kit)."""
    obj = OBJECT.format(first)
    vines = scene.create.vine(last, name=name, surface_prim="/", density_primvar=density_primvar,
                              vine_path=f"{obj}/style_vines", density=1.0, thickness=0.5, root_count=roots, climb=0.6,
                              hang=hang, hang_length=15.0, leaf_spacing=0.9, leaf_spread=1.0)
    stems = scene.create.curves_to_mesh(vines, name="Stems", curves_prim=f"@{name}.out_curves@",
                                        primpath_location="/style_vines/stems")
    bark = scene.create.set_display_color(stems, name="Bark", prim_path="/style_vines/stems",
                                          color_r=0.2, color_g=0.15, color_b=0.1)
    leaf = scene.create.polyhaven_model(bark, name="Kit_leaf", model="shrub_sorrel_01",
                                        primpath_location=f"{BUILTIN}/leaf")
    leaves, rows = _slot(scene, leaf, "Leaves", [f"{BUILTIN}/leaf/shrub_sorrel_01*"])
    return scene.create.scatter_points(
        leaves, name="LeafScatter", points_prim=f"@{name}.out_leaves@", inst_path=f"{obj}/style_vines/leaves",
        prototypes=rows, scale_min=0.25, scale_max=0.5, align_up="+Z", rand_axis="Y", rand_amount=60.0), leaves


def hanging_vines(scene) -> None:
    group = scene.create.chunkgroup(name="HangingVines", doc=(
        "Leafy vines that grow along the outer edges and hang down over the sides.\n"
        "Writes <object>/style_vines: the stems as a mesh and the leaves as a PointInstancer. Needs a "
        f"mesh with divisions. {_slots_doc(('Leaves', 'leaves (one leaf or a small sprig each)'))}\n"
        "Params: Ledges_width, how far from an edge the vines may grow; Vines_growth, how far they have "
        "grown (0-100); Vines_hang, how many tips hang down (%); Vines_seed, other vines."))
    with group:
        ledges = scene.create.edge_field(name="Ledges", prim_path="/", primvar="ledges", find="Outer Edges",
                                         width=0.3)
        last, leaves = _vines(scene, ledges, "Ledges", "ledges", roots=30, hang=60.0)
        scene.set_output(last)
    vines = next(c for c in scene.get_chunks() if c.name == "Vines")
    loft(group, ledges.get_param("width"), vines.get_param("growth"), vines.get_param("hang"),
         vines.get_param("seed"))
    _loft_slots(group, leaves)
    _export(scene, group, "hanging_vines.usda")


def abandoned_cottage(scene) -> None:
    group = scene.create.chunkgroup(name="AbandonedCottage", doc=(
        "A whole building gone to seed: ivy up the walls, bushes and rubble round its base.\n"
        "Ivy climbs the walls in patches; plants crowd the walls; debris lies between them. Put the "
        "building on a ground and set Around_ground to it (or select the building, then the ground, "
        "before picking the style). "
        + _slots_doc(("Debris", "debris"), ("Plants", "plants"), ("Leaves", "ivy leaves")) + "\n"
        "Params: Around_reach, how far out the plants and rubble go; Around_breakup, how patchy; "
        "IvyPatches_threshold, how much of the walls the ivy may cover; Vines_growth, how far it has grown."))
    with group:
        # The ivy first: a chunk reads only what is above it, so it grows on the building and not
        # on the rubble, plants and ground patch the style makes after it.
        patches = scene.create.noise_field(name="IvyPatches", prim_path="/", primvar="style_ivy", size=1.6,
                                           threshold=0.42, softness=0.35)
        last, leaves = _vines(scene, patches, "IvyPatches", "style_ivy", roots=60, hang=12.0)
        around = _around(scene, reach=2.2, breakup=0.75)
        scene.connect(last, around)
        last, builtin = _rubble_kit(scene, around, "Around", planks=True)
        debris, rows = _slot(scene, last, "Debris", builtin, spacing=0.34)
        last = _scatter(scene, debris, rows, "DebrisScatter", _spacing(debris), scale=(0.4, 1.6), out="debris")
        last, builtin = _kit(scene, last, PLANTS)
        plants, plant_rows = _slot(scene, last, "Plants", builtin, spacing=0.63)
        last = _scatter(scene, plants, plant_rows, "PlantScatter", _spacing(plants), scale=(0.9, 2.0), seed=7, out="plants")
        scene.set_output(last)
    _loft_around(group, around)
    vines = next(c for c in scene.get_chunks() if c.name == "Vines")
    loft(group, patches.get_param("threshold"), vines.get_param("growth"))
    _loft_slots(group, debris, plants, leaves)
    _export(scene, group, "abandoned_cottage.usda")


def overgrown_ruin(scene) -> None:
    group = scene.create.chunkgroup(name="OvergrownRuin", doc=(
        "A ruin taken back by nature: vines off its edges, weeds in its corners, rubble round its base.\n"
        "Vines hang from the top edges, weeds grow out of the inner corners, and debris piles round the "
        "base. Needs a mesh with divisions for the corners and edges. Put it on a ground and set "
        "Around_ground. "
        + _slots_doc(("Weeds", "weeds"), ("Debris", "debris"), ("Leaves", "vine leaves")) + "\n"
        "Params: Around_reach, how far out the rubble goes; Corners_width, how far weeds spread from a "
        "corner; Vines_hang, how many vine tips hang down (%)."))
    with group:
        # What reads the ruin first, before the style adds anything under it (see abandoned_cottage).
        corners = scene.create.edge_field(name="Corners", prim_path="/", primvar="style_corners",
                                          find="Inner Corners", width=0.3)
        ledges = scene.create.edge_field(corners, name="Ledges", prim_path="/", primvar="style_ledges",
                                         find="Outer Edges", width=0.35)
        last, builtin = _kit(scene, ledges, PLANTS[2:])
        weeds, weed_rows = _slot(scene, last, "Weeds", builtin, spacing=1.17)
        last = scene.create.scatter_points(
            weeds, name="WeedScatter", surface_prim="/", density_primvar="style_corners",
            inst_path=OBJECT.format("Corners") + "/style_weeds", prototypes=weed_rows, auto_spacing=False,
            spacing=_spacing(weeds), max_count=3000, scale_min=0.6, scale_max=1.4, rand_axis="Y", seed=11)
        last, leaves = _vines(scene, last, "Corners", "style_ledges", roots=30, hang=70.0)
        around = _around(scene, reach=2.0, breakup=0.7)
        scene.connect(last, around)
        last, builtin = _rubble_kit(scene, around, "Around")
        debris, rows = _slot(scene, last, "Debris", builtin, spacing=0.3)
        last = _scatter(scene, debris, rows, "DebrisScatter", _spacing(debris), scale=(0.3, 1.1), out="debris")
        scene.set_output(last)
    _loft_around(group, around)
    vines = next(c for c in scene.get_chunks() if c.name == "Vines")
    loft(group, corners.get_param("width"), vines.get_param("hang"))
    _loft_slots(group, weeds, debris, leaves)
    _export(scene, group, "overgrown_ruin.usda")


def mossy_ledges(scene) -> None:
    group = scene.create.chunkgroup(name="MossyLedges", doc=(
        "Grass and small plants taking hold on everything facing up: ledges, sills, wall tops.\n"
        "A Facing Field finds the faces that look up, a Noise Field breaks them into patches, and "
        "plants are scattered there. Works on any mesh: the finer it is, the closer the patches "
        f"follow it. {_slots_doc(('Moss', 'small plants or moss clumps'))}\n"
        "Params: Tops_angle, how far from level a face may tilt and still count; Patches_threshold, "
        "how much of it the plants cover; Scatter_seed, another pattern."))
    with group:
        patches = scene.create.noise_field(name="Patches", prim_path="/", primvar="style_patches", size=0.8,
                                           threshold=0.4, softness=0.35)
        tops = scene.create.facing_field(patches, name="Tops", prim_path="/", primvar="style_tops", find="Tops",
                                         angle=25.0, softness=15.0, base_primvar="style_patches")
        last, builtin = _kit(scene, tops, LEDGE)
        moss, rows = _slot(scene, last, "Moss", builtin, spacing=1.27)
        last = scene.create.scatter_points(
            moss, name="Scatter", surface_prim="/", density_primvar="style_tops",
            inst_path=OBJECT.format("Patches") + "/style_moss", prototypes=rows, auto_spacing=False,
            spacing=_spacing(moss), max_count=8000, scale_min=0.4, scale_max=1.0, scale_bias=2.0, rand_axis="Y", seed=4)
        scene.set_output(last)
    loft(group, tops.get_param("angle"), patches.get_param("threshold"), last.get_param("seed"))
    _loft_slots(group, moss)
    _export(scene, group, "mossy_ledges.usda")


def _ground_biome(scene, name: str, doc: str, texture: str, items, rubble: bool, spacing: float, out: str,
                  tiling: float = 6.0) -> None:
    """spacing: how close, in sizes of the assets."""
    """A biome put on a ground mesh: its texture, and assets scattered in noisy patches."""
    group = scene.create.chunkgroup(name=name, doc=doc + " " + _slots_doc(("Assets", "assets")) + (
        "\nParams: Patches_size and Patches_threshold, how big and how many the patches are; "
        "Scatter_seed, another pattern."))
    with group:
        patches = scene.create.noise_field(name="Patches", prim_path="/", primvar="style_patches", size=2.5,
                                           threshold=0.5, softness=0.4)
        last = scene.create.polyhaven_material(patches, name="Ground", texture=texture, primpath_location="/",
                                               material_path=f"/materials/style_{texture}", tiling=tiling)
        builtin = []
        if rubble:
            last, builtin = _rubble_kit(scene, last, "Patches")
        last, more = _kit(scene, last, items)
        kit, rows = _slot(scene, last, "Assets", builtin + more, spacing=spacing)
        last = _scatter(scene, kit, rows, "Scatter", _spacing(kit), scale=(0.5, 1.6), on="Patches",
                        primvar="style_patches", surface="/", max_count=12000)
        scene.set_output(last)
    loft(group, patches.get_param("size"), patches.get_param("threshold"), last.get_param("seed"))
    _loft_slots(group, kit)
    _export(scene, group, out)


def ruins_ground(scene) -> None:
    _ground_biome(scene, "RuinsGround", (
        "Ground of a ruined town: rubble underfoot, broken concrete and weeds in patches.\n"
        "Put it on a ground mesh (with UVs). The texture is Poly Haven's rubble; the built-in kit a broken "
        "concrete block, stones and weeds."),
        "rubble", (("weed", "weed_plant_02", 1.0), ("grass", "grass_bermuda_01", 1.0)), True, 0.78,
        "ruins_ground.usda")


def forest_floor(scene) -> None:
    _ground_biome(scene, "ForestFloor", (
        "A forest floor: leaf litter, ferns, dry branches and mossy rocks in patches.\n"
        "Put it on a ground mesh (with UVs). The texture is Poly Haven's forest leaves."),
        "forest_leaves_02", FOREST, False, 1.32, "forest_floor.usda")


def asphalt_lot(scene) -> None:
    _ground_biome(scene, "AsphaltLot", (
        "An abandoned lot: cracked asphalt with litter, gravel and weeds in patches.\n"
        "Put it on a ground mesh (with UVs). The texture is Poly Haven's asphalt."),
        "asphalt_02", (("weed", "weed_plant_02", 1.0), ("bag", "trashbag", 1.0), ("box", "cardboard_box_01", 1.0)),
        True, 0.79, "asphalt_lot.usda")


STYLES = (worn_paint, dirty_stone, rubble, hanging_vines, concrete_debris, bushy_nature, forest_hideout,
          scrap_yard, abandoned_cottage, overgrown_ruin, mossy_ledges, ruins_ground, forest_floor, asphalt_lot)

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    # Lazy: nothing cooks. A style's own file holds no object to put it on.
    only = set(sys.argv[2:])
    for build in STYLES:
        if only and build.__name__ not in only:
            continue
        with Scene(lazy=True) as scene:
            build(scene)
        print(f"Wrote {build.__name__}.usda")
