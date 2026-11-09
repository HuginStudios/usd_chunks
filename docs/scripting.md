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

Before you run a script, you need **a Python with USD** and usd_chunks installed in it.
Use usdview's own Python, or install usd_chunks with
`python -m pip install -e "./usd_chunks[standalone]"`, which adds `usd-core`. See
[Install](install.md#quick-start).

The chunks that come with usd_chunks are found by themselves. To use your own chunks too,
set `USD_CHUNK_DIRS` to their folders, the same as for usdview.

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
python box.py
```

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
| Every chunk you can make | `usdchunks list` (or `usdchunks list <word>`), or `dir(scene.create)` |
| What a chunk is for, and every param it takes | `usdchunks show transform`, or `help(scene.create.transform)` |
| A chunk's param names | `usdchunks show transform`, or `[p.name for p in chunk.get_params()]` (group names are in the list too) |
| Every chunk, in the order it was made | `scene.chunks_in_order()` (`scene.get_chunks()` lists the newest first) |

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

Each value you set cooks what it affects straight away, so setting several cooks several
times. Set them inside `chunk.change_block()` and they count as one change: what they
affect cooks once, when the block ends, with every value in place.

``` py
from usd_chunks import chunk

with chunk.change_block():
    lift.get_param("tx").value = 1.0
    lift.get_param("ty").value = 2.0
# lift, and everything after it, cooked once
```

Blocks can nest. An inner block joins the outermost one.

## Connect chunks

There are two kinds of wire, the same two the graph draws.

**The input port** sets the order chunks run in. Pass a chunk's inputs first when you make
it. You can pass more than one.

``` py
lift = scene.create.transform(box)      # box -> Transform
both = scene.create.merge(a, b)         # a and b -> Merge; b wins
c.add_dependency(b)                     # b -> c, for chunks that already exist
```

Into a Merge, the last input wins, the same as a chunk chained after the others. Where `a`
and `b` both author the same prim, `b`'s value is the one you get.

**A param wire** makes one param follow another's value. Pass the param as the value:

``` py
scene.create.transform(box, primpath_location=box.get_param("prim_path"))
scene.set(lift, primpath_location=box.get_param("prim_path"))
lift.get_param("primpath_location").connect_value(box.get_param("prim_path"))
```

All three lines do the same thing. Both kinds of wire are saved with the network.

## Build one chain

In usdview, only the chunks wired into the graph's Output cook, or into the active View
while one is active. The rest are switched off. So build your network as one chain: give
each chunk the one before it as its input.

``` py
ground = scene.create.ground(prim_path="/set/ground")
box = scene.create.cube(ground, prim_path="/set/box")                              # ground -> box
scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=0.5)  # box -> Transform
```

A chunk you make without an input starts a new, separate chain. It stays off until the
Output or a View points at it. This is on purpose, so you can build a side branch and look
at it later.

To end the graph at a chunk, use `scene.set_output()`. To look at a chunk, use
`scene.set_view()`:

``` py
lift = scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)
scene.set_output(lift)        # the graph now ends at lift, and it cooks
scene.set_view(box)           # look at box for now, in View 2
scene.set_view(lift, view=3)  # and at lift in View 3
```

| Call | Does |
| --- | --- |
| `scene.set_output(chunk)` | What **Set Output** does in the graph: the Output's wire moves to `chunk`, no View stays active, and the graph cooks to the Output. A graph with no Output gets one. |
| `scene.set_view(chunk)` | What **Set View** does: the View's old input is replaced, that View becomes active, and what it reaches cooks. It uses a View already showing `chunk`, else the first View the graph doesn't have yet, else the active one. |
| `scene.set_view(chunk, view=3)` | The same, in View 3. Views are numbered 2 and 3, as in the graph, since 1 is the Output. |

`usdchunks run` cooks to the Output, and a ChunkGroup delivers its Output, so end a script
with `scene.set_output()`. Use `scene.set_view()` to look at something along the way.

A script's own `Scene.new()` has no Output or View, so all its chunks cook, until you call
one of these. Build one chain there too: the saved network then works when you open it in
usdview.

## Backdrops and notes

A backdrop frames chunks in the graph, and a sticky note says what part of it is for. See
[Backdrops and sticky notes](node-graph-basics.md#backdrops-and-sticky-notes).

``` py
frame = scene.add_backdrop([box, lift], title="Geometry", color="Green")
note = scene.add_note("Builds the box and lifts it", at=box)

scene.set_backdrop(frame, title="Box", color=(0.6, 0.3, 0.2))
scene.set_backdrop(frame, around=[box, lift, cap])   # fit it around these
scene.chunks_in_backdrop(frame)                      # [box, lift, cap]
scene.get_backdrops()                                # [frame, note]
scene.remove_backdrop(note)                          # the chunks stay
```

| Call | Does |
| --- | --- |
| `scene.add_backdrop(chunks, title=, color=)` | A backdrop around the chunks, in their graph. Or give `rect=(x, y, w, h)` instead of chunks. |
| `scene.add_note(text, at=)` | A note just above a chunk, or at an `(x, y)`. |
| `scene.get_backdrops(context=None, notes=True)` | The backdrops and notes in a graph. `context` is a ChunkGroup; None is the graph new chunks go in. |
| `scene.set_backdrop(b, title=, color=, rect=, around=)` | Change any of them. What you leave out stays. |
| `scene.chunks_in_backdrop(b)` | The chunks in it: the ones that move with it in the graph. |
| `scene.remove_backdrop(b)` | Delete it. |

A colour is a name (Grey, Red, Orange, Yellow, Green, Teal, Blue, Purple) or `(r, g, b)`
from 0 to 1. Graph coordinates have y pointing down, as on screen. Each call returns or
takes the backdrop's USD prim (or its prim path). `backdrops.read(prim)` from
`usd_chunks.graph` gives its title, colour, rect and whether it is a note.

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

### Department layers side by side

A Layer chunk goes into whatever layer is being edited where it is wired. So a second Layer
wired after a chunk inside the first one's Edit Target lands *inside* the first layer:
lighting nested in layout, not beside it. For department layers that sit side by side, end
each department with an Edit Target back to `Session Layer`, and wire the next Layer after
that:

``` py
world = scene.create.prim(primpath_location="/World", prim_type="Xform")

layout = scene.create.layer(world, layer_name="layout")
into = scene.create.edit_target(layout, edit_target="layout")
box = scene.create.cube(into, prim_path="/World/box")
back = scene.create.edit_target(box, edit_target="Session Layer")   # out of layout

lighting = scene.create.layer(back, layer_name="lighting")
into = scene.create.edit_target(lighting, edit_target="lighting")
key = scene.create.distant_light(into, primpath_location="/World/key", intensity=3.0)
back = scene.create.edit_target(key, edit_target="Session Layer")   # out of lighting
```

`layout` and `lighting` are now both sublayers of the session layer, the box is in `layout`
and the light in `lighting`. An Export USD chunk after the last `back` writes each to its
own file, `layout.usda` and `lighting.usda`.

## Save and cook

| Call | Does |
| --- | --- |
| `scene.save(path)` | Writes the network: Chunk prims and wires. It opens as a graph. |
| `scene.export(path)` | Writes the result as plain USD, without the Chunk prims. |
| `scene.execute()` | Runs every chunk. You rarely need it: chunks cook when they are made and when a value changes. |
| `scene.commit()` | Runs the chunks that write files, such as Export USD. |

## Build a big network fast

Each chunk you make cooks what it affects straight away. In a long network that is often the
whole chain above it, again and again. Make the chunks inside `chunk.change_block()` instead:

``` py
with chunk.change_block():
    ...  # make and wire hundreds of chunks
# here everything has cooked, once
```

Inside the block each chunk cooks once as it is made, before it is wired, and nothing else
cooks. When the block ends, what changed since then cooks once, upstream first: about two cooks
per chunk. A readout you read inside the block is from that first, unwired cook. A 336-chunk
network took 250 s this way, against 17-27 minutes.

### Write the network without cooking it

To only write a network, open the scene lazily. Nothing cooks until you ask for a result:

``` py
with Scene(stage, lazy=True) as scene:      # or Scene.open(path, lazy=True)
    stand = scene.create.noise_field(...)   # marked, not cooked
    pines = scene.create.scatter_points(stand, ...)
    sun = scene.create.distant_light(pines, intensity=30.0)
    scene.cook(pines)                       # cooks Stand and Pines, nothing else
    print(pines.get_param("readout").value) # fresh
    scene.save("forest_network.usda")       # writes the network, as it stands
```

- `scene.cook(chunk)` cooks that chunk and what is still marked above it. Do this before
  you read a result: an OUTPUT param, or prims a chunk wrote. The chunk itself always
  cooks, even when nothing marked it, so it also picks up what a script changed behind the
  framework's back (a Scatter Brush's painted field, set as a Python attribute). It works
  the same in an eager Scene. `scene.cook()` with no chunk cooks everything.
- `export` and `commit` cook everything first, each chunk once.
- `save` writes the network as it stands. It does not cook it.
- Leaving the block cooks nothing. Closing the scene removes every result anyway.

Some chunks build params in their cook, such as a Light's schema inputs. They take the
values you give at `create` in that first cook, the same way a saved network's values come
back when you open it. A save cooks only those chunks, alone, without what is above them,
so their values are saved. The catch: a misspelled param name raises at the chunk's first
cook, not at `create`.

The woodland forest network (40 chunks, 8.5M instances) took about 12 minutes to write
the old way. Lazily it takes 6 s.

## Inside usdview

`Scene.current()` gives you the scene usdview is running. What you make goes into it:

- The chunks go into the graph you are looking at. If you have gone into a chunkgroup,
  they go into that group.
- The first node goes to the middle of the graph view. The next ones go in a row to its
  right. Each `with` block starts a new row.
- Leaving the `with` block does not close usdview's scene.
- If the graph has no Output yet, it adds one at the end of your chain.
- If the graph already has an Output, new chunks start switched off, the same as nodes you
  make in the graph: they are not in the Output's chain yet. To cook them, call
  `scene.set_output(last)` with the chain's last chunk, or `scene.set_view(last)` to look
  at it. In the graph, you can instead select that node and press **V**, which makes a
  View. Press **2** or **3** to switch between Views, and **1** to go back to the Output.

## Live sessions

A live session keeps a network open, and you change its params from another terminal or
a script. Each change cooks only what it reaches, then commits again, so a Render chunk
renders again. Opening the file and the first cook happen once, not on every change.

Start one:

``` sh
usdchunks run shot.usda --live
```

It cooks the network as `run` does, then waits. Ctrl+C ends it. From another terminal:

``` sh
usdchunks send shot.usda Fill.intensity=8 Sky.intensity=2 --wait
usdchunks send shot.usda --list     # every param you can set, with its type
usdchunks send shot.usda --reset    # clear every value sent so far
```

`--wait` returns once the session has cooked the change, and rendered it if the network
renders. Without it, `send` returns as soon as the values are written.

The same from Python, for a script that tries values one after another:

``` py
from usd_chunks import live

for strength in (2, 4, 8):
    live.send("shot.usda", {"Fill.intensity": strength}, wait=True)
    ...  # look at the render
```

Name a param as `run` takes it: `Chunk.param`. The last part is enough when only one of the
chunk's params ends with it, so `Fill.intensity` finds the Light's `attr_grp.inputs:intensity`.
When two chunks in different groups share a name, `--list` shows their params under the
prim path instead, such as `/chunks/Group/Fill.attr_grp.inputs:intensity`, and
`/chunks/Group/Fill.intensity` finds it. A name `send` cannot find gets the closest
matches in the error.

The session and `send` share two files beside the network:

| File | Written by | Holds |
| --- | --- | --- |
| `shot.live.usda` | `send` | The values, as USD overs on the Chunk prims. |
| `shot.live.json` | the session | Every param it can set, and which change it has finished. |

The session holds `shot.live.usda` as a sublayer of its session layer and reloads it when
the file changes. A value arrives as an edit on the stage, the same as one made in the
graph. The network file itself is never written.

The values stay in `shot.live.usda` after the session ends, and the next session opens with
them. `--reset` clears them. To keep them, set them in the network.

While a session runs, the Render chunk keeps its renderer between renders. The renderer
builds its scene once and then updates only what a change touched. The renderer's whole
scene stays in memory for as long as the session runs.

Two limits:

- `send` sets only the params the session lists. A chunk can build some params only when
  their group is opened (Render Settings does, for each renderer's settings). Such a param
  is listed once the network has a value for it.
- A value in `shot.live.usda` is stronger than a change to the same param in the graph.
  Clear it with `--reset` to edit that param in the graph again.

## Examples

### Five boxes in a row

``` py
from usd_chunks import Scene

with Scene.new() as scene:
    last = scene.create.cube(prim_path="/geo/box0")
    for i in range(1, 5):
        box = scene.create.cube(last, prim_path=f"/geo/box{i}")
        last = scene.create.transform(box, primpath_location=box.get_param("prim_path"), tx=i * 3.0)
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

Ground, three colored boxes, a sky light and a camera, in one chain:

``` py
from usd_chunks import Scene

colors = [(1.0, 0.2, 0.2), (0.2, 1.0, 0.2), (0.2, 0.4, 1.0)]

with Scene.new() as scene:
    last = scene.create.ground(prim_path="/set/ground")
    for i, (r, g, b) in enumerate(colors):
        box = scene.create.cube(last, prim_path=f"/set/box{i}")
        path = box.get_param("prim_path")
        moved = scene.create.transform(box, primpath_location=path, tx=i * 2.0 - 2.0, ty=0.5)
        last = scene.create.set_display_color(moved, prim_path=path, color_r=r, color_g=g, color_b=b)
    sky = scene.create.dome_light(last, primpath_location="/set/sky")
    scene.create.camera(sky, primpath_location="/set/cam", ty=2.0, tz=10.0)

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
