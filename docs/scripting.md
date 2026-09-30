# Scripting

You can build a network from Python instead of the node graph. A script makes the same
chunks the graph does. What it builds opens as a graph, and a script can add to a graph
you already have.

## Quick start

### In usdview

Open the Python interpreter (**Window > Interpreter**) and paste this:

``` py
from usd_chunks import Scene

with Scene.current() as scene:
    box = scene.create.cube(prim_path="/geo/box", width=2.0)
    scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)
```

The box shows up in the viewport, and the nodes show up in the graph. There is nothing to
set up: if the Chunks panel works, this works too.

### In a terminal

Before you run a script, you need:

1. **A Python with USD.** Use usdview's own Python, or install usd_chunks with
   `python -m pip install -e "./usd_chunks[standalone]"`, which adds `usd-core`. See
   [Install](install.md#quick-start).
2. **`USD_CHUNK_DIRS` set** to your chunk folders, the same as for usdview. Without it,
   `scene.create.cube` fails with "No chunk module or variant called 'cube' -- is
   USD_CHUNK_DIRS set?".

Save this as `box.py`:

``` py
from usd_chunks import Scene

with Scene.new() as scene:
    box = scene.create.cube(prim_path="/geo/box", width=2.0)
    scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

    scene.save("box_network.usda")   # the network: opens as a graph
    scene.export("box.usda")         # the result: plain USD, no Chunk prims
```

Then run it:

```
set USD_CHUNK_DIRS=C:\path\to\usd_chunks\chunks
python box.py
```

In PowerShell, use `$env:USD_CHUNK_DIRS = "C:\path\to\usd_chunks\chunks"`. On Linux or
macOS, use `export USD_CHUNK_DIRS=/path/to/usd_chunks/chunks`.

Use `Scene.current()` in usdview and `Scene.new()` in a script. See
[Get a scene](#get-a-scene).

## Get a scene

| Call | Gives you |
| --- | --- |
| `Scene.new()` | A new, empty scene of your own. |
| `Scene.open(path)` | A saved network, loaded and cooked. |
| `Scene.current()` | The scene that is running now, such as usdview's. `None` if there is none. |

- `Scene()` is the same as `Scene.new()`.
- `Scene(stage)` works on a stage you already have.
- Only one scene can be live at a time. Leaving the `with` block closes a scene from
  `Scene.new()` or `Scene.open()`. Without `with`, call `scene.close()` when you are done.
- Inside usdview, `Scene.new()` and `Scene.open()` raise an error. Use `Scene.current()`.

## Make chunks

`scene.create` has one function per chunk:

- **Every chunk module, by its file name.** `transform.py` is `scene.create.transform()`.
  Your own chunks in `USD_CHUNK_DIRS` work the same way.
- **Every menu entry, by its label in snake_case.** Mesh's "Cube" entry is
  `scene.create.cube()`. Light's "Rect Light" is `scene.create.rect_light()`.

Each function returns the chunk it made.

``` py
scene.create.transform(
    box,                                          # input: box runs first
    name="Lift",                                  # node name (optional)
    primpath_location=box.get_param("prim_path"), # a param: wired to follow it
    ty=3.0,                                       # a value: set
)
```

To find what you can make and set:

| To see | Use |
| --- | --- |
| Every chunk you can make | `dir(scene.create)` |
| What a chunk is for | `help(scene.create.transform)` |
| A chunk's param names | `[p.name for p in chunk.get_params()]` (group names are in the list too) |

If the chunk name is in a string, use `scene.add("transform", box, ty=3.0)`. This also
works for a file name that is not a Python name, like `my-node.py`.

If a module and a menu entry have the same name, you get the module. If two menu entries
have the same label, neither is in `scene.create`. Make the module and set its params
instead.

## Set params

Pass params by name when you make the chunk. Params inside groups are found by name too.

``` py
lift = scene.create.transform(box, ty=3.0)
```

To change them later:

``` py
scene.set(lift, ty=5.0)
lift.get_param("ty").value = 5.0     # the same thing
```

Chunks cook again by themselves when a value changes.

## Connect chunks

There are two kinds of wire, the same two the graph draws.

**The input port** sets the order chunks run in. Pass a chunk's inputs first when you make
it. You can pass more than one.

``` py
lift = scene.create.transform(box)      # box -> Transform
both = scene.create.merge(a, b)         # a and b -> Merge
c.add_dependency(b)                     # b -> c, for chunks that already exist
```

**A param wire** makes one param follow another's value. Pass the param as the value:

``` py
scene.create.transform(box, primpath_location=box.get_param("prim_path"))
scene.set(lift, primpath_location=box.get_param("prim_path"))
lift.get_param("primpath_location").connect_value(box.get_param("prim_path"))
```

All three lines do the same thing. Both kinds of wire are saved with the network.

## `with` blocks

`with chunk:` says where the chunks made inside it go.

- **A chunkgroup**: they go inside the group.
- **Any other chunk**: it becomes their input. Each chunk made inside runs after it, unless
  you give it inputs of your own.

Use a group to keep chunks together:

``` py
props = scene.create.chunkgroup(name="Props")
with props:
    crate = scene.create.cube(prim_path="/props/crate")
```

Use an Edit Target to write into a layer. The chunks after an Edit Target write into its
layer:

``` py
shot = scene.create.layer(layer_name="shot")

with scene.create.edit_target(shot, edit_target="shot"):
    ground = scene.create.ground(prim_path="/set/ground")
    scene.create.rect_light(ground, primpath_location="/set/key")
```

The ground and the light go into the `shot` layer. A Layer chunk only makes the layer. The
Edit Target is what sends edits to it.

## Save and cook

| Call | Does |
| --- | --- |
| `scene.save(path)` | Writes the network: Chunk prims and wires. It opens as a graph. |
| `scene.export(path)` | Writes the result as plain USD, without the Chunk prims. |
| `scene.cook()` | Runs every chunk. You rarely need it: chunks cook when they are made and when a value changes. |
| `scene.commit()` | Runs the chunks that write files, such as Export USD. |

## Inside usdview

`Scene.current()` gives you the scene usdview is running. What you make goes into it:

- The chunks go into the graph you are looking at. If you have gone into a chunkgroup,
  they go into that group.
- The first node goes to the middle of the graph view. The next ones go in a row to its
  right. Each `with` block starts a new row.
- Leaving the `with` block does not close usdview's scene.

## Examples

### Five boxes in a row

``` py
from usd_chunks import Scene

with Scene.new() as scene:
    for i in range(5):
        box = scene.create.cube(prim_path=f"/geo/box{i}")
        scene.create.transform(box, primpath_location=box.get_param("prim_path"), tx=i * 3.0)
    scene.export("boxes.usda")
```

### Open a saved network and change it

``` py
from usd_chunks import Scene

with Scene.open("box_network.usda") as scene:
    lift = scene.get_chunk("Transform")
    scene.set(lift, ty=10.0)
    scene.export("box_high.usda")
```

`scene.get_chunk(name)` finds a chunk by its node name.

### A light with its values set

``` py
from usd_chunks import Scene

with Scene.new() as scene:
    scene.create.rect_light(primpath_location="/set/key", intensity=5.0, exposure=2.0)
    scene.export("light.usda")
```

### A small set

Ground, three colored boxes, a sky light and a camera:

``` py
from usd_chunks import Scene

colors = [(1.0, 0.2, 0.2), (0.2, 1.0, 0.2), (0.2, 0.4, 1.0)]

with Scene.new() as scene:
    scene.create.ground(prim_path="/set/ground")
    for i, (r, g, b) in enumerate(colors):
        box = scene.create.cube(prim_path=f"/set/box{i}")
        path = box.get_param("prim_path")
        moved = scene.create.transform(box, primpath_location=path, tx=i * 2.0 - 2.0, ty=0.5)
        scene.create.set_display_color(moved, prim_path=path, color_r=r, color_g=g, color_b=b)
    scene.create.dome_light(primpath_location="/set/sky")
    scene.create.camera(primpath_location="/set/cam", ty=2.0, tz=10.0)

    scene.save("set_network.usda")
    scene.export("set.usda")
```

More examples are in `examples/scripts/`.

## Under the hood

`scene.create` is a short way to make chunks. It calls
`ChunkRegistry().get_chunk_module(name)`, then the module's `create()` or `load()`, then
`scene.add_chunk(chunk)`. The loader, paste and the graph use these calls too. Use them
directly if you need to make a chunk without adding it to the scene
(`chunk.create_chunks(name, anonymous=True)`). To build a network, use `scene.create`.
