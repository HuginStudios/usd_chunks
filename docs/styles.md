# Styles

A style is a ready-made look you put on an object in one go: worn paint, dirt in the
corners, rubble, hanging vines. Pick the object, pick the style, and the network that makes
the look is added to your graph, aimed at that object.

## Putting a style on an object

1. Select the object in the viewport: a mesh, or a prim with meshes under it.
2. In the node graph, open the **Styles** menu and click a picture.

The style's node is wired onto the end of your chain and cooks at once. You can also add a
style from the Tab menu, under **Styles**, or from a script:

```python
scene.create.worn_paint(crate, primpath_location="/World/crate")
```

With nothing picked, the style goes to a place of its own, `/<style>` (an empty Xform), until
you set its Location.

The node is a Reference to the style's file. Its **Location** is the object, so put the same
style on two objects and you get two nodes that don't get in each other's way. Dive into the
node to see and change the network; your changes are kept in your scene, and the style's file
is never written to.

A style needs a mesh with divisions. Most of them work from the edges of the mesh, and they
work per point: on an eight-point cube every point is on an edge.

### Handles in the viewport

With the object picked, each edge field in its styles shows a handle, named for what it marks:
**Wear**, **Dirt**, **Chips**, **Corners**, **Ledges**. Drag it up to widen the band: more
wear, dirt further from the corner, rubble spread wider. The handles need
[usd_gizmos](install.md#gizmos-viewport-handles) to be drawn.

### The params on the node

A style's node shows the few params its author picked, such as `Dirt_width` or
`Scatter_spacing`. Everything else is inside it.

## The styles that come with usd_chunks

| Style | What it does | Writes |
| --- | --- | --- |
| Worn Paint | Paints the object blue, rubbed back to a pale undercoat on the edges, in patches | a material, `/materials/worn_paint` |
| Dirty Stone | Grey stone, brown dirt in the inner corners, pale chips on the edges | a material, `/materials/dirty_stone` |
| Rubble | Small stones gathered in the inner corners | `<object>/rubble` |
| Hanging Vines | Leafy vines along the outer edges, hanging down the sides | `<object>/vines`, `<object>/vine_leaf` |

They are in `chunks/styles`, built by `examples/styles/build_styles.py`, with pictures made by
`examples/styles/render_thumbnails.py`.

## Making your own

A style is a [ChunkGroup saved to its own file](core-chunks.md#chunkgroup), with two rules:

- **"/" is the object.** Write every prim path as if the object were the root: `/` for the
  object itself, `/rubble` for a prim under it. The Reference's Location is put in front of
  them when the style is used.
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
