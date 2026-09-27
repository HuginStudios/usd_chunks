# Scripting

Networks can be built from Python as well as in the node graph. A script makes the same
chunks the graph does, so what it builds opens as a graph, and a graph can be extended by
a script.

``` py
from usd_chunks import Scene

with Scene() as scene:
    box = scene.create.cube(prim_path="/geo/box", width=2.0)
    scene.create.transform(box, primpath_location=box.get_param("prim_path"), ty=3.0)

    scene.save("box_network.usda")   # the network: reopens as a graph
    scene.export("box.usda")         # the result: plain USD, no Chunk prims
```

Run it with `USD_CHUNK_DIRS` pointing at the folders your chunks live in, the same as for
usdview. More examples are in `examples/scripts/`.

## The scene

`Scene()` sets up everything a chunk needs to run, on a new in-memory stage.
`Scene(stage)` works on a stage you already have, and `Scene.open(path)` opens a saved
network and loads its chunks.

Only one scene is live at a time, as in any host. Leaving the `with` block closes it;
without `with`, call `scene.close()` before making the next.

| Call | Does |
| --- | --- |
| `scene.save(path)` | Writes the network: Chunk prims and wires. It reopens as a graph and re-cooks. |
| `scene.export(path)` | Writes the result as plain USD, with the Chunk prims stripped out. |
| `scene.cook()` | Runs every chunk, upstream first. Rarely needed: chunks cook when they are created and when a value changes. |
| `scene.commit()` | Runs the chunks that write files, such as Export USD. |

## Creating chunks

`scene.create` has one function per chunk:

- **Every chunk module, by its file name.** `transform.py` is `scene.create.transform()`.
  Your own chunks are found the same way, from `USD_CHUNK_DIRS`, the next time the scene
  starts.
- **Every menu variant, by its label in snake_case.** Mesh's "Cube" entry is
  `scene.create.cube()`, and Light's "Rect Light" is `scene.create.rect_light()`. A
  variant is its module with some params preset.

A module name wins over a variant label, and a label two variants share is left out
rather than guessed at. `dir(scene.create)` lists what is there.

When the name is only known at runtime, `scene.add("transform", ...)` takes it as a
string. A file name that is not a Python identifier (`my-node.py`) is reachable that way
too.

Each function returns the chunk it made. Its arguments are:

``` py
scene.create.transform(
    box,                                          # upstream chunks: this runs after them
    name="Lift",                                  # the node's name; the chunk's own if left out
    primpath_location=box.get_param("prim_path"), # a param: wired to follow it
    ty=3.0,                                       # a value: set
)
```

Params are found by name, inside groups too. Plain values are set before the chunk joins
the scene, so its first cook already uses them.

## Connecting chunks

There are two kinds of connection, and they are the same two the graph draws.

**An exec wire** says one chunk runs after another. It decides the order chunks cook in,
and which layer a chunk authors into (see Edit Target below). Pass the upstream chunks
positionally when creating, or wire existing ones:

``` py
scene.create.transform(box)       # Transform runs after box
scene.chain(a, b, c)              # a, then b, then c
c.add_dependency(b)               # the same, on the chunk itself
```

**A param wire** says one param follows another's value. It is saved with the network
and draws in the graph. Pass the param as the value, when creating or later:

``` py
scene.create.transform(box, primpath_location=box.get_param("prim_path"))
scene.set(xform, primpath_location=box.get_param("prim_path"))
xform.get_param("primpath_location").connect_value(box.get_param("prim_path"))
```

`scene.chain` and a positional upstream call `add_dependency`; a param as a value calls
`connect_value`. Calling them directly is fine: the scene only saves the typing.

## `with` blocks

`with chunk:` says where the chunks created inside it go.

- **A chunkgroup takes them in as its children.**
- **Any other chunk runs before them**, so each chunk created inside, unless given its
  own upstream, runs after it.

The second is how a script authors into a layer. The layer a chunk writes to comes from
the nearest Edit Target upstream of it, so:

``` py
shot = scene.create.layer(layer_name="shot")

with scene.create.edit_target(shot, edit_target="shot"):
    ground = scene.create.ground(prim_path="/set/ground")
    scene.create.rect_light(ground, primpath_location="/set/key")
```

puts the ground and the light in the `shot` layer. Layer on its own only makes a layer;
the Edit Target is what points edits at it, in a script as in the graph.

## Inside usdview

The same calls work on the scene a host is already running. In usdview's interpreter:

``` py
from usd_chunks import current_scene

with current_scene() as scene:
    box = scene.create.cube(prim_path="/box")
```

The chunks land in the network the graph is showing, including a chunkgroup you have
dived into. Leaving the block leaves usdview's scene as it was; only a `Scene` of your own
is closed by it.

## Under the hood

`scene.create` is a layer over the lower-level calls the loader, paste and the graph
frontends use: `ChunkRegistry().get_chunk_module(name)`, then the module's `create()` or
`load()`, then `scene.add_chunk(chunk)`. Those stay, for code that needs what they give
and scripting does not -- building a chunk without adding it (`chunk.create_chunks(name,
anonymous=True)`), or restoring a saved chunk's values before it exists in the scene.
To build a network, use `scene.create`.
