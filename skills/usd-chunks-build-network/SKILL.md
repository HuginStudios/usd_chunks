---
name: usd-chunks-build-network
description: Build or edit a usd_chunks node network with the Python Scene API (scene.create, set_output, save, export) from a plain-language description of a scene. Use when asked to make a network, graph, setup or scene out of chunks, to script one, or to change values or wiring in an existing network .usda.
---

# Build a network with the Scene API

A network is chunks wired into a chain and saved in a `.usda`. A script builds the same
chunks the graph does, and what it saves opens as a graph. The full reference is
`docs/scripting.md`, with runnable examples in `examples/scripts/`.

## 1. Turn the description into a chain

Write the network as an ordered list before any code: each step is one chunk, its input is
the step before it, and the last step is what the network delivers. For example, "a
ground with three coloured boxes and a camera" becomes ground → cube → transform →
set_display_color (×3) → dome_light → camera.

Find the chunk for each step:

- `dir(scene.create)` lists every chunk: each module by file name (`transform.py` →
  `scene.create.transform()`) and each menu entry by its label in snake_case (Mesh's
  "Cube" → `scene.create.cube()`).
- `help(scene.create.transform)` shows what a chunk does (its `config()` description).
- Outside Python: `grep -rn "description=" chunks/` and read the module.
- A chunk's param names: `[p.name for p in c.get_params()]`, or read its `main()`.

If no chunk does a step, say so and offer the `usd-chunks-write-chunk` skill. Don't fake
the step with a Python chunk unless the user agrees.

## 2. Write the script

```python
from __future__ import annotations

from usd_chunks import Scene, chunk

with Scene.new() as scene:
    with chunk.change_block():                      # cook once at the end, not per chunk
        ground = scene.create.ground(prim_path="/set/ground")
        box = scene.create.cube(ground, prim_path="/set/box")          # ground -> box
        lift = scene.create.transform(
            box,                                                       # box -> Transform
            primpath_location=box.get_param("prim_path"),              # param wire
            ty=0.5,                                                    # a value
        )
        cam = scene.create.camera(lift, primpath_location="/set/cam", tz=10.0)
    scene.set_output(cam)                 # the network ends here; usdchunks run cooks to it

    scene.save("set_network.usda")        # the network: opens as a graph
    scene.export("set.usda")              # the result: plain USD, no Chunk prims
```

- **One chain.** Pass the previous chunk as the first argument. A chunk made without an
  input starts a separate branch that doesn't cook in usdview until the Output or a View
  reaches it.
- **Wires:** pass a chunk as an input to set run order. Pass a *param* as a value
  (`box.get_param("prim_path")`) to make one param follow another, the same as
  `connect_value`. Both are saved.
- **Values** go in as keyword args by param name; names inside groups are found too.
  Change them later with `scene.set(c, ty=5.0)`.
- **End with `scene.set_output(last)`.** `usdchunks run` and a ChunkGroup deliver the
  Output. `scene.set_view(c)` only lets you look at something along the way.
- **Many chunks:** wrap the building in `chunk.change_block()` as above. To only write the
  file without cooking, use `Scene(stage, lazy=True)` or `Scene.open(path, lazy=True)`
  and `scene.cook(c)` before reading a result.
- **Layers:** chunks inside `with scene.create.edit_target(layer_chunk, edit_target="shot"):`
  write into that layer. **Groups:** chunks made inside `with group:` (a
  `scene.create.chunkgroup(name=...)`) go into it. Set a group's `doc` param to say what it
  does; agents and people read it.
- **Frame the parts** with `scene.add_backdrop([...], title=...)` and `scene.add_note(...)`
  so the saved graph reads well.

## 3. Edit an existing network

Change the network, don't rebuild it from scratch:

```python
from usd_chunks import Scene

with Scene.open("set_network.usda") as scene:
    lift = scene.get_chunk("Transform")        # by node name
    scene.set(lift, ty=2.0)
    extra = scene.create.sphere(lift, prim_path="/set/ball")
    scene.set_output(extra)
    scene.save("set_network.usda")
```

`scene.chunks_in_order()` lists what's there. To find node names without Python, look at
the prims under `/chunks` in the `.usda`. Ask before overwriting the user's network file;
otherwise save to a new path.

**In usdview's interpreter**, use `Scene.current()` instead of `new`/`open` (they raise
there). New chunks go into the graph on screen and start switched off if the graph already
has an Output, so finish with `scene.set_output(last)` or `scene.set_view(last)`.

## 4. Run and check it

The script needs a Python with `pxr` and usd_chunks installed (`pip install -e
".[standalone]"`, or the host's Python). Set `USD_CHUNK_DIRS` if it uses the user's own
chunks. Then:

```
python build_set.py
usdchunks validate set_network.usda -v      # every chunk type resolves
usdchunks run set_network.usda -o check.usda
```

Open `check.usda` (or read it) and confirm the prims the description asked for are there.
A misspelled param name raises at `create`, or with `lazy=True` at that chunk's first cook.
Tell the user which files you wrote and what's in them.
