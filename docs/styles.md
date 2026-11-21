# Styles

A style is a ready-made look you put on an object in one go: worn paint, dirt in the
corners, rubble piled round its base, bushes, ivy up the walls, a whole abandoned cottage.
Pick the object, pick the style, and the network that makes the look is added to your graph,
aimed at that object.

A style is the placing, not the assets. It works out where things go: round the object's
base, in its inner corners, along its edges, on everything facing up. Then it puts assets
there. Give it your own assets (a folder of USD files, or prims in your scene) and it places
those. Give it nothing and it uses its built-in kit: pieces it makes itself (broken concrete,
planks) and free [Poly Haven](https://polyhaven.com) models (CC0), downloaded the first time.

## Putting a style on an object

1. Select the object in the viewport: a mesh, or a prim with meshes under it.
2. For a style that piles things round the object (rubble, bushes, a cottage's plants),
   Ctrl-click the ground it stands on too: the second pick is its ground.
3. In the node graph, open the **Styles** menu and click a picture.

The style's node is wired onto the end of your chain and cooks at once. You can also add a
style from the Tab menu, under **Styles**, or from a script:

```python
scene.create.worn_paint(crate, primpath_location="/World/crate")
scene.create.abandoned_cottage(house, primpath_location="/House", Around_ground="/ground")
```

With nothing picked, the style goes to a place of its own, `/<style>` (an empty Xform), until
you set its Location.

The node is a Reference to the style's file. Its **Location** is the object, so put the same
style on two objects and you get two nodes that don't get in each other's way. Dive into the
node to see and change the network; your changes are kept in your scene, and the style's file
is never written to.

Styles that work from the object's edges and corners (Worn Paint, Dirty Stone, Rubble, Hanging
Vines, Overgrown Ruin) need a mesh with divisions: they work per point, and on an eight-point
cube every point is on an edge. The piles round an object work on any mesh.

The first time a style is used it downloads its assets from Poly Haven, a few MB each, into
your usd_chunks cache (set `USD_CHUNK_POLYHAVEN_DIR` to put it elsewhere). In usdview that
happens in the background and the style fills in when they arrive; after that it is instant.

### Your own assets

Each kind of thing a style places is a **slot**: Debris, Plants, Props, Leaves, and so on.
Each slot is four params on the style's node:

| Param | What it does |
| --- | --- |
| `Debris_files` | A folder of USD files (its subfolders too), files, or globs, separated by `;`. A folder whose assets come in detail levels (`rock_low.usdz`, `rock_mid.usdz`, `rock_high.usdz`) gives one of each |
| `Debris_prims` | Prims in your scene to use instead: `/Library/rocks/*`, or `%rocks` for a collection. The prims are hidden, so only the copies are seen |
| `Debris_fit` | The size each asset is scaled to (its largest side), so the style's piles suit them whatever size they were made at. 0 keeps their own size |
| `Debris_spacing` | How close the copies go, in sizes of the assets: 1 side by side, 0.3 piled up. Bigger assets spread out by themselves |

```python
scene.create.abandoned_cottage(
    house, primpath_location="/House", Around_ground="/ground",
    Debris_files="C:/assets/rubble", Plants_files="C:/assets/plants;C:/assets/ferns",
    Leaves_files="C:/assets/ivy/leaf_*.usd")
```

Every asset is stood on the ground at the origin, in your scene's units and up axis: a
centimetre asset in a metre scene comes in at the right size, and a Z-up asset stands up.
Leave a slot empty to keep the built-in kit for it.

### Handles in the viewport

With the object picked, each edge field in its styles shows a handle, named for what it marks:
**Wear**, **Dirt**, **Chips**, **Corners**, **Ledges**. Drag it up to widen the band: more
wear, dirt further from the corner, rubble spread wider. A pile round the object has a
**Reach** handle: drag it to spread the pile further out. The handles need
[usd_gizmos](install.md#gizmos-viewport-handles) to be drawn.

### The params on the node

A style's node shows the few params its author picked, such as `Dirt_width`, and its slots.
Everything else is inside it.

## The styles that come with usd_chunks

| Style | Put it on | What it does |
| --- | --- | --- |
| Concrete Debris | an object on a ground | Broken concrete and stones piled round its base |
| Bushy Nature | an object on a ground | Bushes, ferns, nettles and grass crowding its base |
| Forest Hideout | an object on a ground | Ferns, dry branches, mossy rocks and dandelions round it |
| Scrap Yard | an object on a ground | Boxes, bins, bags, barrels and crates dumped round it, with planks |
| Abandoned Cottage | a building on a ground | Ivy up the walls in patches, bushes, rubble and planks round the base |
| Overgrown Ruin | a ruin (with divisions) on a ground | Vines off the top edges, weeds in the corners, rubble round the base |
| Mossy Ledges | any mesh | Grass and small plants in patches on everything facing up: ledges, sills, wall tops |
| Ruins Ground | a ground (with divisions and UVs) | Rubble texture, broken concrete and weeds in patches |
| Forest Floor | a ground (with divisions and UVs) | Leaf-litter texture, ferns, branches and mossy rocks in patches |
| Asphalt Lot | a ground (with divisions and UVs) | Asphalt texture, litter and weeds in patches |
| Rubble | an object with divisions | Broken concrete and stones gathered in its inner corners |
| Hanging Vines | an object with divisions | Leafy vines along its outer edges, hanging down the sides |
| Worn Paint | an object with divisions | Blue paint rubbed back to a pale undercoat on the edges, in patches |
| Dirty Stone | an object with divisions | Grey stone, brown dirt in the inner corners, pale chips on the edges |

Everything a style makes goes under the object, in prims named `style_...`: the assets laid
out for its slots (`style_kit`), the copies it scatters, its vines.

They are in `chunks/styles`, built by `examples/styles/build_styles.py`, with pictures made by
`examples/styles/render_thumbnails.py`.

## The chunks styles are made of

You can use these on their own too:

- **Poly Haven Model**: a free Poly Haven model by name, from a picture menu of the catalog.
  Pick one part of a set or all of it; each part can sit at the origin, ready for a Scatter
  Points to copy (a row of `<Location>/<model>*`). Its looks are made UsdPreviewSurface, which
  Storm and Aurora draw.
- **Poly Haven Material**: a Poly Haven texture as a material on prims, with tiling.
- **Asset Kit**: your assets laid out for a scatter. A folder of USD files or prims in the
  scene, each stood on the ground at the origin, in the scene's units, and fitted to a size. A
  Scatter Points row of `<Location>/*` copies them; its Spacing can read the kit's
  `out_spacing`. A style's slot is one of these.
- **Around Field**: the ground round an object's base, as a hidden patch with a field that
  fades out from the object. Scatter Points on the patch piles things round the object.
- **Edge Field**: the sharp edges or inner corners of meshes, as a field.
- **Facing Field**: where meshes face up (ledges, roofs), sideways (walls) or down, or are under
  cover (under the eaves), as a field.

## Making your own

A style is a [ChunkGroup saved to its own file](core-chunks.md#chunkgroup), with two rules:

- **"/" is the object.** Write every prim path as if the object were the root: `/` for the
  object itself, `/rubble` for a prim under it. The Reference's Location is put in front of
  them when the style is used.
- **Give it slots.** Scatter an Asset Kit's `<Location>/*` and set its Fallback to the
  built-in assets the style makes, then loft its `files`, `prims`, `fit` and `spacing`.
- **Only prim path params are moved.** A param that picks prims (a Meshes or Prim Path field)
  is put under the object for you. Any other path, like a Scatter's Instancer Path or a
  Mesh's Prim Path, reads the object's path from the first chunk instead:
  `@Edges.prim_path@/rubble`.

Then save it into a `styles` folder:

```python
from usd_chunks import Scene, chunk_stage

with Scene(lazy=True) as scene:
    group = scene.create.chunkgroup(name="Mossy", doc="Moss on the tops.\nMore about it.")
    with group:
        edges = scene.create.edge_field(name="Edges", prim_path="/", primvar="moss")
        ...  # the rest of the look
        scene.set_output(last)
    chunk_stage.loft_param_to_context(edges.get_param("width"), group)  # a param on the node
    chunk_stage.export_network(scene, group, "my_chunks/styles/mossy.usda")
```

- **Where:** a `styles` folder inside any folder on `USD_CHUNK_DIRS`, or any folder on
  `USD_CHUNK_STYLE_DIRS` (separated like `PATH`). A folder earlier on the list wins when two
  have a style of the same name.
- **Its name** is the file name in words: `mossy.usda` is **Mossy**, and
  `scene.create.mossy()`.
- **Its picture** is a `.png` beside it with the same name, 256 pixels square.
  `render_thumbnails.py` shows how to make one.
- **Its tooltip** is the first line of the group's `doc`.

The list of styles is read once, the first time a menu is built: restart usdview after adding
one.
