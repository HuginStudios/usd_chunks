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

Start from [common-chunks.md](common-chunks.md): the chunks most networks use, each with its
key params, so most steps need no lookup. For the rest, find the chunk with the CLI, before
reading any chunk source:

- `usdchunks list` lists every chunk you can make, one line each: its script name, its
  category and what it does. `usdchunks list scatter` lists only those mentioning a word.
  A module goes by its file name (`transform.py` → `scene.create.transform()`), a menu
  entry by its label in snake_case (Mesh's "Cube" → `scene.create.cube()`).
- `usdchunks show mesh` prints one chunk in full: its description, its menu entries,
  and every param a script can set, with its type, default and range. Inside a group,
  set a param by its own name; a list's rows as `prototypes.0.proto_path`.
- Both take `--json`. From Python, `dir(scene.create)` and `help(scene.create.mesh)` give
  the same names and descriptions.

No chunk named after the job doesn't mean no chunk can do it. Before you say a step can't
be done, try the general ones: `property_editor` sets attributes on prims that exist, and
`inline_usd` writes any USD you can type as `.usda`, time samples too (see *Animate a value*
below). Check a candidate with `usdchunks show`; never guess a param. If none of them does
it either, say so and offer the `usd-chunks-write-chunk` skill. Don't do the step with
Python (`script_runner`) unless the user agrees.

Before you model an object, look for one: a `.usd`/`.usda`/`.usdc` file beside the network,
in its `assets/` folder, or next to the files it already references. Bring it in with
`reference`. If nothing fits, build it with chunks: real size and the main parts first, a
material on it (`preview_surface`), then the details that show at the shot distance. Tell
the user what you modelled and why no file was used.

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
- **Wire existing chunks** with `scene.connect(upstream, downstream)`. Into a Merge the
  branch wired last wins (slot 0); `scene.get_inputs(merge)` lists the branches, strongest
  first.
- **Values** go in as keyword args by param name; names inside groups are found too.
  Change them later with `scene.set(c, ty=5.0)`. A list param takes a list, one item per
  row: `imports=["a.usda", "b.usda"]`, or dicts where a row holds several params
  (`prototypes=[{"proto_path": "/rock", "proto_weight": 200.0}]`). One param of one row
  alone is `**{"prototypes.1.proto_weight": 50.0}`.
- **File paths** are relative to the network file, as in USD: `inputs/set.usda` is found
  in its folder, `../inputs/set.usda` from it. Start with `Scene.new(path="<network>.usda")`
  so they resolve from there while building too, not from the working directory, and
  `scene.save()` writes there. Don't `os.chdir`.
- **Units:** a new Scene is in metres (`metersPerUnit` 1) with Y up. Add
  `scene.create.stage_settings(...)` only for other units, an up axis or a default prim.
- **End with `scene.set_output(last)`.** `usdchunks run` and a ChunkGroup deliver the
  Output. `scene.set_view(c)` only lets you look at something along the way.
- **Many chunks:** wrap the building in `chunk.change_block()` as above. To only write the
  file without cooking, use `Scene(stage, lazy=True)` or `Scene.open(path, lazy=True)`
  and `scene.cook(c)` before reading a result.
- **Layers:** chunks inside `with scene.create.edit_target(layer_chunk, edit_target="shot"):`
  write into that layer. A second Layer wired after a chunk inside that Edit Target nests
  *inside* the first layer. For department layers side by side (layout, lighting, anim), end
  each department with `scene.create.edit_target(last, edit_target="Session Layer")` and wire
  the next Layer after it. `examples/scripts/shot_departments.py` is that whole network,
  set to shot.usda, and runs as written: copy it. **Groups:** chunks made inside `with group:` (a
  `scene.create.chunkgroup(name=...)`) go into it. Set a group's `doc` param to say what it
  does; agents and people read it.
- **Leave the graph ready for the artist.** Put new nodes next to the ones they wire to,
  and don't move the rest. Frame a new branch with `scene.add_backdrop([...], title=...)`;
  add a note (`scene.add_note(...)`) only where a control needs explaining. Tell the user
  which node to change next.

### Common jobs

| To | Do |
| --- | --- |
| Name a node, to find or override it later | `scene.create.scatter_points(..., name="Rocks")`; then `usdchunks run net.usda Rocks.seed=3 -o out.usda` |
| A box or a ball | A USD `Cube` / `Sphere` prim ("a USD Cube", `UsdGeom.Cube`): `usd_cube(prim_path=..., cube_size=1.0)`, `usd_sphere(..., sphere_radius=0.5)`, centred on the prim. A polygon `Mesh` with UVs: `cube(...)`, `sphere(...)`, the cube's bottom on the prim. `examples/scripts/cubes_mesh_or_gprim.py` makes all four. A centred prim needs lifting to stand on y=0: a 4 m `usd_cylinder` needs `transform(..., ty=2.0)` |
| A ground or a grid | `ground(prim_path="/World/ground", width1=10, height2=10)`: a grid `width1` (X) by `height2` (Z); it binds a checker material unless `grid_checker=False` |
| Bring in a file | `sublayer(imports=["inputs/set.usda"])`, or `reference(file_path=..., primpath_location="/set")` |
| Move a prim that came from a file | `transform(last, primpath_location="/World/box", tx=3.0)` |
| Change other attributes on it | `property_editor(last, prim="/World/key", edits="inputs:intensity = 3")`, one `name = value` per line, by USD attribute name (a light's need `inputs:`). Values are JSON: `inputs:color = [1, 0.8, 0.6]`, not a tuple. `float name = 1` creates an attribute the prim lacks |
| A colour or a material | `preview_surface(last, mat_path="/World/mats/red", primpath_location="/World/box", diffuse_color=(0.8, 0.1, 0.1), roughness=0.4)` binds a UsdPreviewSurface (comma-separate several prims); `set_display_color(last, prim_path=..., color_r=0.8, color_g=0.1, color_b=0.1)` is a flat colour, no material |
| A light | `light(last, primpath_location="/World/key", light_type="rect_light")` (`distant_light`, `dome_light`, `sphere_light`, `disk_light` too). `intensity`, `width`, `height` and `color` are built when it cooks but can be passed at create |
| Aim a light or camera at a point | `transform(last, primpath_location="/World/key", tx=4.0, ty=5.0, aim_at_point=True, aim_point=(0, 1, 0))` turns the prim's -Z to the point. `camera(last, primpath_location="/World/cam", tz=10.0, aim_at_point=True, aim_point=(0, 1, 0))` has its own |
| A ring of N copies | Put the source at the radius on +X (`transform(..., tx=6.0)`), then `duplicate_prim(last, source_prim="/World/column", dest_path="/World/ring", copies=8, copy_rotate=(0, 45, 0))`: copy i is the source moved i times by the per-copy offset, turned about `copy_pivot`. Copies are named after the source. Then `prune_prim(last, prim_path="/World/column")` drops the source |
| Scatter an exact count, turned about Y only | `scatter_points(last, surface_prim=..., inst_path=..., prototypes=[{"proto_path": "/rock", "proto_weight": 70}], auto_spacing=False, spacing=0.5, max_count=40, orient_mode="Up", rand_axis="Y", rand_amount=360)`. For a margin from the edge, scatter onto a smaller grid and `prune_prim` it |
| Drop objects that fall and land (rigid bodies) | `rbd_solver(last, prim_path="/World/box", collision_path="/World/ground")`: `collision_path` holds the still colliders, one per line. Leave `gravity` at 0: it means Earth gravity along the up axis in the stage's units, and a positive value pushes bodies up. Headless (scripts, `usdchunks run`) it bakes when it cooks; in a UI press **Run Simulation** (`run_sim`) |
| Units, up axis, frame range, default prim | `stage_settings(meters_per_unit=1.0, up_axis="Y", start_frame=1, end_frame=48, default_prim="World")` |
| Instances (rocks ...) on a terrain | Reference the ready-made group: `reference(last, file_path="<repo>/examples/groups/scatter_on_terrain.usda", name="Rocks", Scatter_surface_prim="/World/terrain", Scatter_proto_path="/World/prototypes/rock", Scatter_inst_path="/World/rocks", Scatter_density=2.0, Scatter_max_slope=20.0, Scatter_seed=0)`, after the terrain and prototype are in. Density is per m² of the stage's units: put `stage_settings(meters_per_unit=1.0)` first. A seed per file: `usdchunks run net.usda Rocks.Scatter_seed=N -o rocks_N.usda`. `examples/scripts/use_groups.py` |
| Spiral stairs, or a row of copies | Reference `<repo>/examples/groups/spiral_stairs.usda` (`name="Stairs"`): `Stairs_copies`, `Stairs_copy_translate=(0, rise, 0)`, `Stairs_copy_rotate=(0, -degrees, 0)` (0 for a straight row), `Place_tx` (radius), `Place_ty`, `Place_sx`/`sy`/`sz` (step size), `Stairs_dest_path`. Steps are USD Cubes `step_00` ... |
| Import a URDF robot and pose it | `urdf_import(stage_settings(up_axis="Z"), primpath_location="/World/robot", file_path="pkg/urdf/arm.urdf", mesh_root="pkg")`: links, STL meshes, colliders, masses and joints as UsdPhysics; `mesh_root` is the folder `package://pkg/...` paths are found in. Then `pose_articulation(robot, prim_path="/World/robot", forearm_link=-45.0)`: one slider per moving joint, named after the link the joint moves (degrees, or metres when sliding) |
| A ready-made group's params | `usdchunks info <file>.usda` prints its description and every param with its default |
| A shot in department layers | Copy `examples/scripts/shot_departments.py`: a set sublayered in, layout / lighting / camera side by side, exported as `shot.usda` plus one file per department |
| Write one file per layer | `export_usd(last, output_dir=".", session_file_name="shot", bake_all=True)`: a relative Output Directory is from the network's folder; the session layer (the shot, in a department network) is written as `shot.usda`; Bake all writes plain USD, no network. It lists every real layer itself (rows of `export_layers`), the set file too: set that row's `export_enabled` to False to skip it |
| Animate a value (keyframes) | `inline_usd(last, code=...)` with an `over` on the prim and `.timeSamples`: `double radius.timeSamples = {1: 1, 48: 2}`. Directly authored samples need no bake |
| Move a camera over time (push-in, truck) | Read the camera's `xformOpOrder` first; the Camera chunk's op has a generated name. Then `inline_usd(cam, code=...)` with an `over` at the camera's path that adds its own op, `double3 xformOp:translate:dolly = (0, 0, 0)` and `double3 xformOp:translate:dolly.timeSamples = {1: (0, 0, 0), 48: (0, 0, -2)}`, and sets `xformOpOrder` to the old list with `"xformOp:translate:dolly"` appended. Forward is local -Z, so that moves it 2 units forward and keeps its pose and lens. Check the position at the first, middle and last frame |
| Make one branch win over another | `merge(assets, camera)`: the last input wins where both set the same value. Only where they overlap: it is not a global override |
| Aim a camera or light at something another branch makes | Wire that branch *above* it: `cam = camera(assets, primpath_location="/World/cam", aim_at="/World/hero")`, then `merge(lights, cam)`. A chunk only reads what is wired above it; being in the same Merge isn't enough |

`usdchunks show <name>` has every param; the table is only where to start.

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
Then check they do what was asked, as the user would look at it:

- **Sizes and places:** world bounds (`UsdGeom.BBoxCache`). Standing on the ground means the
  bottom is at the ground's top.
- **Anything that moves:** read the bounds at a few frames across the frame range. A dropped
  body goes down, stops moving, and ends resting on the ground. Up is +Y (or +Z).
- **Signs and units:** a value that sends things the wrong way or a hundred times too far is
  the usual mistake.

If a check fails, fix the param on the chunk that is off and check again.
A misspelled param name raises at `create`, or with `lazy=True` at that chunk's first cook.
Tell the user which files you wrote and what's in them.
