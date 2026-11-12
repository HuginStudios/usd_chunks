# Changelog

Notable changes to usd_chunks. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the pre-1.0 caveat that
the API may still change between minor versions.

This file starts with the first release; development before that is in the commit history.

## [Unreleased]

### Added

- Blender: **box handles** (Blockout Box, Blocking Volume) in the 3D viewport. Pick the
  prim, or select its chunk in the node editor, and a wire box shows; hover a face to light
  it and drag it to move that face. Esc puts it back.
- Blender: **File > Import > USD** of a network file opens its chunk network in the
  USDChunks Editor, in a node tree of its own.
- **One-file Blender add-on.** Each release (a `v*` tag) carries
  `usd_chunks_blender-<version>.zip`: install it with Install from Disk and it works, with
  usd_chunks and its chunks inside. No pip, no `--target`, no `USD_CHUNK_DIRS`. Built by
  `tools/build_blender_addon.py`; the steps from a clone still work.
- **Frames to Movie** chunk: makes an H.264 `.mp4` or ProRes `.mov` from a rendered image
  sequence (`render.####.png`, or wire in a Render chunk's Output) with ffmpeg from
  `imageio-ffmpeg`. It runs on Make Movie and on every `usdchunks run`; odd image sizes
  and gaps in the frames work.
- Render chunk: **Run in Background**. Render Now saves the scene to a file and renders it
  in its own process through Render Command (usdrecord with the chosen Renderer), so you
  keep working while it renders. **Stop Render** ends it and every process it started,
  and a status line shows Rendering, Done, Stopped or Failed. Render on Commit
  (`usdchunks run`) still waits for the render to finish.
- **Points to Instancer** chunk: turns an animated Points prim into a PointInstancer of a
  low-poly mesh sphere (or a prototype you pick), one instance per point on every frame.
  For renderers that draw no Points (Aurora) or draw each as one pixel (V-Ray). Instances
  can shrink with each point's opacity, so fading points still vanish.
- `examples/scripts/shot_departments.py`: a shot on a set file, in layout, lighting and
  camera layers side by side, exported as `shot.usda` plus one file per department. A test
  runs it as written, from another directory.
- Export USD's **Session File Name** names the session layer's file, which in a department
  network is the shot: `session_file_name="shot"` writes `shot.usda`. With Bake all on, the
  session layer is written like the rows, without the network's bookkeeping.
- `examples/scripts/cubes_mesh_or_gprim.py`: a cube and a sphere as polygon meshes (`cube`,
  `sphere`, the Mesh chunk) and as USD's own Cube and Sphere prims (`usd_cube`,
  `usd_sphere`, the Shape chunk), with how each sits on its origin. The build-network
  skill's Common jobs table says which to pick.
- Ready-made ChunkGroups in `examples/groups/`: `scatter_on_terrain.usda` (instances so
  many per m² over a terrain, up to a slope, resting on it) and `spiral_stairs.usda` (a box
  copied, risen and turned: stairs, or a row). A scene references one and sets its params;
  `build_groups.py` makes them and `examples/scripts/use_groups.py` uses both.
- One row's param of a list lofts onto a group (`Scatter_proto_path`, Scatter's first
  prototype). It warned "has no USD attribute bound" and lofted nothing: a list keeps each
  column in one array, so the group's attribute now records the row it drives.
- Gizmos per list row: a chunk module's `gizmos(node)` hook returns handles worked out
  from the chunk as it is, and a driver entry can name one row of a list, `doors.u[2]`.
  A row added gets its handle and a row removed loses it, so a list of placements can
  each have their own handle in the viewport. A gizmo's `anchor` can be a literal prim
  path (`/Building/Doors/door_2`), so a row's handle hangs off the prim made for that row
  and is offered on that prim only.
- A `box` gizmo type: a box whose faces are the handles. A host picks the face under the
  mouse and pulls it along its normal, and only that face moves. It drives six params, the
  distance to each face. `gizmo.runtime.pick_box_face` and `drag_box_face` do the picking
  and the drag for every host, against the box itself, never the geometry inside it.
- Scatter Points: a prototype row takes a collection, `%trees`, for one prototype per member of
  the collection named trees. Deactivated library entries count, and the meshes under a member
  are not prototypes of their own. Variant selections work on it too: `%trees{season=*}`.
- Transform: an **Aim** section. **Aim At** (a prim) or **Aim At Point** with **Point** turns the
  prim's -Z axis, the way lights and cameras face, toward the target on every cook:
  `scene.create.transform(light, primpath_location="/World/key", tx=3, ty=4, tz=3,
  aim_at_point=True, aim_point=(0, 0.75, 0))`.
- The build-network skill has a Common jobs table: naming a node and overriding it from
  the CLI, bringing in a file, moving or editing a prim from it, stage settings, one file
  per layer, and what to do for animation. `usdchunks list` ends by pointing at it, and
  `docs/scripting.md` opens with a pointer to it.

- The chunk menu (`Tab` or right-click in the node graph) lists Backdrop and Sticky Note,
  and search finds them.
- Camera: **Aim At Point** and **Point** keep the camera pointed at a world-space point, as
  Aim At does for a prim: `scene.create.camera(tx=0, ty=3, tz=15, aim_at_point=True,
  aim_point=(0, 1, 0))`.
- Scatter Brush with Surface Mesh left empty: Points and Spray paint on anything under the
  cursor, and Clear removes them. With a Surface Mesh set, the brush paints only that mesh.
  Density still needs a mesh.
- Scatter Brush: a Stack checkbox. In Points mode, spacing then only counts the current
  stroke, so with Surface Mesh empty each new stroke can land on top of the instances
  earlier strokes made, to build towers.
- `Scene.new(path=...)` names the file the network will be saved to. Its relative file
  paths resolve from that folder while it is built, and `scene.save()` writes there.
- Stage Settings' **Relative File Paths** (on by default): saving a network writes the files
  its chunks read relative to its folder where they sit under it. `scene.save(path,
  relative_paths=...)` overrides it for one save.
- A warning, once per chunk and path, for an input file given as an absolute path (the
  network is not portable) or one that is not found (naming the folders searched).
- `chunk.resolve_file(node, path)` and `chunk.anchor_file(node, path)`, for a chunk to
  find or author a param's file as USD would.
- A list param takes a Python list in `scene.create` and `scene.set`, one item per row:
  `scene.create.sublayer(imports=["a.usda", "b.usda"])`, or a dict per row where a row has
  several params (`prototypes=[{"proto_path": "/rock", "proto_weight": 200.0}]`). The list
  then has exactly those rows. Setting a list used to mean setting
  `get_param("imports").size` by hand and then each row's params one by one.
- A script can name one param of a row the list doesn't have yet
  (`**{"imports.2.sublayer_path": "c.usda"}`), and the list grows to hold it; it raised
  KeyError. A misspelt row param still raises.
- `usdchunks show` lists the row params of a list with no rows yet, such as a new
  Sublayer's `sublayer_path`, and gives the list form to set them.

- An Output node: what a graph delivers. A new graph gets one wired to the end of its chain
  instead of a View. With no active View a graph cooks to its Output, a ChunkGroup
  delivers its inner Output, and `usdchunks run` cooks to the Output whichever View the file
  was saved with. Views are for looking. Files saved with only Views keep working as before.
- `chunk.change_block()`: values set inside it are one change, so what they affect cooks
  once when the block ends rather than once per value. For scripts, and for modules that
  set many params of their own (the Wedge uses it).
- Cooks nothing reached are skipped. A chunk that
  only the chunks above it changed doesn't cook unless one of them changed a prim it reads.
  What it reads is not declared: each cook runs on a stage holding only its inputs and its
  last output, and a cook that looks up anything else is cooked on the whole stage from then
  on. On an interior set with a 5000-instance Scatter below a Transform and a Light, an
  edit of either takes 18 ms instead of 270 ms, with an identical result. Framework code
  that works on the scene during a cook uses the new `get_scene_stage()`.
- A cook cache for slow chunks. A module that sets `cache_cook=True` in its config has its
  cooked layer kept on disk and read back when its params, the files they name, its code
  and the stage's units all match, including on reopen; a `cache_key(node)` hook adds
  inputs that are not params. Each such node gets a Cache Result checkbox, and
  `USD_CHUNKS_COOK_CACHE=0` turns it off everywhere. URDF Import, Poly Reduce, Mesh, House
  and CAD Import use it: the duck robot opens in 0.5 s instead of 1.1 s, and with a 25%
  Poly Reduce on it in 0.7 s instead of 4.9 s.
- A chunk prim records which version of its module's code it cooked with, as
  `info:moduleHash` beside `info:chunkType`, and reopening a scene warns once per chunk type
  whose saved code differs from what is loaded now -- naming both hashes and the module file
  to diff. Written by the cook, not by a save, so output that was never re-made keeps the
  hash it was made with; a file from before this, or a chunk that never cooked, is not
  treated as changed.
- `from usd_chunks import Scene` builds and runs chunk networks from a plain script:
  `scene.create.<chunk>()` for every chunk module and menu variant (`scene.create.cube()`,
  `scene.create.rect_light()`), input chunks passed positionally, and `with chunk:` to
  nest into a chunkgroup or run after any other chunk -- so `with
  scene.create.edit_target(...)` authors what is inside into that layer. `save()` writes
  the network, `export()` the plain-USD result. Examples in `examples/scripts/`.
  The same calls work on any host's scene: `usd_chunks.current_scene()` inside usdview
  builds into the network the graph is showing.
- Mesh has one menu entry per shape: Cube, Grid, Tube and Ground. A Plane entry is a
  Grid with one face.
- A Shape chunk for USD's own shape prims: Cube, Sphere, Cylinder, Cone, Capsule and Plane,
  each in the menu as USD Cube, USD Sphere and so on. Renderers tessellate them; Mesh is
  still the one for polygon meshes with UVs.
- Duplicate Prim makes a row of copies: Copies sets how many, and Translate, Rotate, Scale
  and Pivot step each copy from the one before, so a row, a ring or a spiral staircase is
  one chunk. As Instances makes the copies share one prototype instead of being full
  copies.
- Scatter Points' Density (per m²) sets the count from the surface's area: the area of
  the faces within Max Slope, in square metres by the stage's metersPerUnit.
- Stage Settings sets the stage's Default Prim.
- `usdchunks list [query] [--json]` lists every chunk and menu entry with the first line
  of its description and the name a script creates it by; `usdchunks show <name>` prints
  one chunk's description and params. They run from the shell, for people and agents
  picking a chunk.
- Every chunk's description starts with a one-line summary of what it does, which
  `usdchunks list` shows. A test holds every chunk to it, and the write-chunk skill and
  `docs/writing-a-chunk.md` say how to write one.
- `docs/scripting.md` has a recipe for department layers side by side (layout, lighting,
  anim) instead of each nested in the one before.
- Viewport picking in Blender falls back to a CPU ray against prim bounds when Hydra's
  own pick is unavailable -- on some builds `TestIntersection` raises on pending GL errors
  left in the queue by whoever drew last, and a click did nothing but print a traceback.
  A click on a mesh now also finds the chunk driving the Xform above it.
- Viewport handles in Blender: a prim picked in the live Hydra view shows the chunk
  handles its driving chunk declares (`ui:gizmo:*`, see `docs/gizmo-host-contract.md`) as
  native Blender gizmos -- arrows, dials and box-arrows -- each writing through
  `chunk_gizmo.apply_bindings` per tick, so the prim moves because the chunk's params
  moved. A prim no chunk drives gets a Transform chunk rather than a direct write.
- The Blender frontend follows a hosted network: a Reference/Sublayer/Payload pointed at
  a saved chunk network wears the group colour and dives with Tab, a param overriding the
  file's value is drawn in alert red with a revert arrow beside it, and the sidebar gains
  Publish and Localise.

- A Reference, Sublayer or Payload chunk pointed at a saved chunk network becomes that
  network's node (a Payload hosts it as a payload arc, so it unloads with the rest). The file's chunkgroup composes in under the chunk's own prim, so the node is the
  group: dive in and the network is there, as if it had been expanded in place. It cooks
  whether or not you dive. Edits inside are overrides in the current file (see below);
  the network file is never written to. Several network files in one Sublayer stack
  under the node in list order, first strongest. Pointed at scene content, both chunks
  behave as before.
- A host's "Insert to location" is the scene root its network's prim paths resolve
  under, so two hosts of one room network go to /left/... and /right/... instead of
  fighting over the same prims. Empty by default: a sequence hosted in a shot keeps the
  paths the sequence wrote. The stored text and the network file are untouched -- the
  prefix applies when a prim-path param (a `primpath_location`, or one flagged
  PRIM_SELECTION) is read.
- A param overriding a composed-in value shows it (amber label, "Overrides seq.usda" on
  hover) and offers "Revert to seq.usda" on right-click, which drops the shot's opinion
  so the sequence's shows through again.
- "Publish network…" on a chunkgroup writes it to its own file and makes the group a
  host of that file; "Localise network" on a host (or a published group) takes the
  network back in as a plain chunkgroup of this file, overrides folded in.
- Installable from a wheel: `pip install usd-chunks` provides the library and the
  `usdchunks` CLI, with `usd-core` as a dependency and no USD build required.
- `usdchunks plugin-path`, which prints the installed plugin directory so
  `PXR_PLUGINPATH_NAME` can be set without knowing where pip put the package.
- Licensed under Apache 2.0, matching OpenUSD.
- Continuous integration on Linux (Python 3.10, 3.11, 3.13) and Windows (3.13), including
  a job that installs the built wheel into a clean virtualenv.
- Documentation covering params, groups, and how the runtime works, plus
  `build_docs.bat --share` for publishing the built site to a fileshare or web root.
- A reserved `commit(node)` chunk hook, for chunks whose real work is writing to disk
  rather than authoring into the stage. It runs once per chunk that defines it, in
  topological order, after the DAG solve has settled — where `execute(node)` is the cook
  and is re-run throughout a solve. Disabled chunks never commit, and a module without
  the hook is unaffected. Export USD now implements it, and its Export button calls the
  same function, so the interactive and headless paths cannot drift.

- Scatter Points: Grow Near. A layer weights where it lands by how near it is to what an
  earlier layer placed -- understory under the trees, ferns around the bushes, grass out of
  the shade -- read straight from that layer's instancer (or any points). Inside, Outside,
  Radius, Clear Radius (keep off the trunk) and Scale With Source; multiplied into the
  Density Field. Distances are exact and across the ground, so a one-quad ground works.
- Proximity Field: the same weight painted as a primvar on a mesh, for when the field should
  be seen, painted over or multiplied with a Noise Field (Multiply With).
- Scatter Brush: Spray. A third Output mode that sprinkles points over the surface under the
  brush, Spacing apart, at any mesh resolution; Strength is how much one dab fills, Fill
  sprays the whole surface. Wire it into Painted Points for an instance each, or into Grow
  Near to paint a region.
- Curves to Mesh: a Ribbon profile -- a flat strip the curve's width wide, lying on the
  ground (a road, a river, a path).
- `examples/scripts/biome.py`: a nested forest biome in four Scatter Points layers, each
  growing near (or away from) the ones before it, on stand-in shapes.
- `scene.create` takes a list row's param as `list.row.param`
  (`**{"prototypes.0.proto_path": "/assets/rock"}`), set before the chunk's first cook.

### Changed

- **Breaking:** URDF Import gives a welded link with no collision geometry (a camera mount,
  a tool tip) no rigid body and no fixed joint: it is nested under its parent, which is the
  weld. As a body it was simulated apart from its parent and fell away. A script that read
  that link's `joint_<name>` fixed-joint prim should read the nesting instead.
- Setting a param a chunk doesn't have from a script raises with the list of params it does
  have, including those a chunk makes as it cooks (Pose Articulation's joint sliders, which
  `usdchunks show` can't list). Pose Articulation's description says how its sliders are
  named and set, URDF Import's points to it, the build-network skill has a row for importing
  and posing a robot, and the write-chunk skill starts by checking that no chunk does the job.
- Dragging a gizmo stays on the cursor when the chunk cooks slowly. During a drag a chunk
  cooks at most 30 times a second, with the newest handle value, so mouse moves that arrive
  during a cook no longer queue up behind it. Once a cook takes longer than 50 ms, the drag
  stops cooking while the handle moves, instead of freezing the viewport on every cook, and
  cooks where the handle is held still for a tenth of a second and where it is let go.
  Hosts still call `apply_bindings` every tick. In Blender, the pivot dot now follows the
  mouse instead of the last cook.
- **Breaking:** a recook changes only what changed in the chunk's layer (diff cook). The cook
  writes into a scratch layer, on a stage of only the layers upstream of the chunk, and its
  result is reconciled into the chunk's layer: an unchanged prim sends no notice and a
  changed value is set in place, so the viewport updates it instead of rebuilding it (an
  instance's prototype too). Dragging the house example's box face: the frame after each
  tick 0.30 → 0.19 s; posing a SimReady fridge's door: 0 resyncs where it was ~30, the frame
  ~100 → 37 ms. The composed result is the same as before. A chunk that changes the layer
  stack or what is muted (Edit Target, Layer, a gate), sublayers its own layer, or writes
  onto chunk prims (Override Chunk Prim) is still cleared and rewritten.
  `USD_CHUNKS_DIFF_COOK=0` turns it off. What to change in a chunk module of your own:
  inside `execute()`, read and write through `node.get_stage()` and
  `node.get_edit_context()`, not the scene's stage (`node.get_scene_stage()`,
  `scene.get_stage()`), which no longer holds the layer the cook writes into; keep
  `get_scene_stage()` for the scene's bookkeeping (wiring chunk prims, layer menus). A cache
  kept between cooks is keyed on `node.get_layer_identifier()`, not the edit layer's
  identifier, which changes.
- Pose Articulation writes its op on every link a joint can move, the identity while the
  joint is at rest, so its layer holds the links whatever the pose.
- **Breaking:** a new script Scene (`Scene()`, `Scene.new()`) is in metres: its stage authors
  `metersPerUnit` 1, where it said nothing and USD read centimetres. What a script exports is
  stamped in metres, and chunks that read the units (RBD gravity, unit warnings) follow. To
  keep centimetres, add `scene.create.stage_settings(meters_per_unit=0.01)`. Saved networks,
  stages passed in and usdview, Blender and Houdini scenes keep their own units.
- Export USD writes the files each exported file uses relative to where it writes it (its
  new **Relative Paths**, on by default): a shot's sublayer of its set is
  `../inputs/set.usda`, not where the set sat on the machine that exported it.
- Saving a network writes its input file paths relative to its folder where they sit under
  it, and keeps relative paths naming the same file when saved to another folder.
  Reopened from anywhere, such a network finds them, as USD would.
- **Breaking:** a Merge built from a script lets the last input win, as one built in the
  graph does. `scene.create.merge(a, b)` puts `b` in slot 0, the strongest, so `b` wins
  where both branches author the same prim; it used to put `a` there. A script that relied
  on the first input winning needs its inputs swapped. Saved networks keep their slots.
- **Breaking:** a new Mesh Grid has no checker material. Grid Checker, on by default, binds
  it; a Grid made from the menu or a script sets it off. A script that wanted the checker
  passes `grid_checker=True`. Saved networks keep theirs.
- Export USD never writes over the network. A target that is a file holding a chunk
  network, such as the network's own file when Output Directory is its folder, is skipped
  with a message saying what to change.
- The Particle Solver imports Taichi when it first simulates, not when it loads. Blender
  loads every chunk to make its node types, so New Chunk Network is about a second faster.
- Chunks talk to each other only through wires: an exec input or an `@Node.param@`
  expression. A change no longer sends notices to the chunks wired below it; they are
  marked dirty and cooked. A callback can't listen to another chunk's param, and
  `listen_globally` is only for the stage and the host. Both raise a `ValueError`
  otherwise. A param `add_dependency` across two chunks used to warn and now raises; use
  `connect_value`. The RBD Solver now goes stale through its own cook instead of a global
  listener.
- Menus of what is on the stage fill as they open. A module's new `menu_items(node,
  param_name)` hook gives a Menu its items when the Qt, Flet, ImGui or Blender frontend
  shows it. Skel Pose, Set Variant and Set Camera use it, and no longer listen to the stage;
  Skel Pose walked the whole stage on every stage change to keep its Character menu current.
- Camera's Lock to Free Camera sets the camera's translate and rotate params instead of
  writing its transform straight onto the prim. The cook authors it, so the chunks wired
  below see each move, and the params always match the camera.
- Layer and Export USD wait for their saved values to load before refreshing from the stage,
  and Layer writes its After Layer list only when it changed.
- Opening a network no longer re-cooks what several exec roots share. Each root used to
  solve everything below it, so a Merge of six References and the Scatter below it cooked
  once per root; a root that an earlier root's solve already cooked is now skipped.
  interior_set_v3 opens in 1.1 s instead of 2.5 s, with the same result.
- `chunk.Checkbox` is defined in `chunk_core` and re-exported from `chunk`, unchanged, so
  the framework can add one itself.
- The framework clears a procedural chunk's layer before every `execute()`, so a chunk
  module no longer does. A cook always starts from nothing and authors its whole result:
  a module that forgot to clear can no longer pile up prims or walk its own unique path up
  by one on every cook. Only a layer the chunk owns (anonymous) is cleared; a chunk handed
  its context's file layer, and an `interactive=True` chunk, are left alone. Callbacks are
  not affected -- they still author on top of the cooked result. A chunk module that
  cleared its layer at the top of `execute()` can drop the line.
- View-driven recooks (`recook_for_view`, on every view switch, wire and delete) only
  execute chunks that are stale -- ones a change reached while they could not cook, or
  whose layer is new. The rest are revealed with the result they already hold. A clean
  CAD, URDF or poly-reduce import upstream no longer redoes its work each time.
- What a param drives (`connect_usd_attr`) is re-authored by the framework after each
  cook, as the param's state rather than the cook's output.
- A Light keeps its attribute params across a cook when its prim has the same attributes,
  instead of deleting and rebuilding every one -- and its widget -- on each slider tick.
- Emptying the path of a Reference, Payload, UV Texture or Curves to Mesh now removes its
  output, as emptying Bind Material's already did, rather than leaving the last one.
- Scatter Brush holds its paint on the chunk and authors it on every cook; the Camera
  holds the placement the freecam gave it while locked. Neither keeps state in its layer
  any more.
- `chunk_utils.get_unique_path` is a pure lookup. It no longer clears the asking chunk's
  layer, and takes `own_layer` so a prim only that layer defines is not a collision.

- **Breaking:** the Create Layer and Add Mesh chunks are now Layer (`chunks/usd/layer.py`)
  and Mesh (`chunks/modelling/mesh.py`). A saved network records a chunk's module name, so
  one using either will not load them, and Edit Target's follow entry is now
  "(Follow Upstream Layer)".
- `usd_chunks/plugins/` is split by what each file is. Toolkit frontends moved to
  `usd_chunks.frontends.{qt,flet,imgui,blender}` (`chunk_qt` -> `frontends.qt.factory`,
  `chunk_qt_nodegraph` -> `frontends.qt.nodegraph`, and so on), host entry points to
  `usd_chunks.hosts.{usdview,kraken,houdini}`, and the CLI to `usd_chunks.cli`. The core's
  graph, gizmo and store modules are subpackages now: `chunk_graph` -> `graph.engine`,
  `chunk_graph_layout` -> `graph.layout`, `chunk_gizmo` -> `gizmo.runtime`,
  `chunk_gizmo_types` -> `gizmo.types`, `pin` -> `store.pin`, `stores` ->
  `store.providers` (the `usd_chunks.stores` entry-point group keeps its name). Every old
  module name still imports, as an alias of the same module (see `usd_chunks._moved`). **Host directories moved with no alias**: point
  `PXR_PLUGINPATH_NAME` at `usd_chunks/hosts/usdview` (or `kraken`) and `HOUDINI_PATH` at
  `usd_chunks/hosts/houdini`; `usdchunks plugin-path --host <host>` prints the new path.
  The standalone graphs run as `python -m usd_chunks.frontends.flet.nodegraph` and
  `python -m usd_chunks.frontends.imgui.nodegraph`.

- The example chunk library moved from `scripts/` to `chunks/`. Point `USD_CHUNK_DIRS` at
  `<repo>/chunks`; the test suite adds it itself.

- A network brought in by composition is tweaked, never rewritten. Sublayer a sequence's
  network into a shot, or reference it under a chunkgroup, and editing a param in the shot
  authors a sparse `over` in the stage's edit target (the shot file itself, unless the
  host targets another layer); the sequence file is read-only to the chunk system. Saving keeps the arc (the root layer's foreign sublayers are carried onto
  the file, references stay on their prim) and writes only the shot's overrides, so the
  sequence stays live underneath. Before, opening such a shot moved the sequence's chunk
  prims out of the sequence file, editing wrote back into it, and saving inlined every
  chunk as a full `def` with the arc dropped -- or failed outright for a referenced one.
- `usdchunks run` now writes the outputs a network declares on disk: a network containing
  an Export USD chunk exports its configured files, which it previously did only when the
  Export button was pressed in the UI. Running a network that declares an export exports.
- The host integrations moved from a top-level `plugins` package to `usd_chunks.plugins`.
  Anything importing `plugins.chunk_usd` must now import `usd_chunks.plugins.chunk_usd`.
  A top-level package named `plugins` collided with any other distribution shipping one.
- Callback arity is decided by the callback's own declared signature. Both
  `(notice, sender)` and `(owner, notice, sender)` work for plain functions and bound
  methods alike; previously a plain function declaring `(notice, sender)` never ran.
- Minimum Python is 3.10, which is what the code has actually required.

- Scatter Points cooks faster: Grow Near with Outside 0 samples only the triangles a source
  can reach; Grow Near and the Density Field look up points per grid cell instead of
  rescanning; upright, spun instances are placed as arrays (identical results). A 30-chunk
  forest network builds in 37 s, down from 196 s.

### Fixed

- Blender: B (brush) did nothing on a brush chunk just selected in the node editor. The
  viewport tools read the editor's selection as it is now.
- Blender: a Y-up network (usdview's default) lay on its side in the 3D viewport, below the
  ground. The live view now stands it up, as Blender's USD importer does, and scales it by
  `metersPerUnit`. Picking, the pivot, G/R/S, the brush and the camera mirror go through
  the same turn. X, Y and Z after G, R or S name Blender's axes, so R Z turns a prim about
  the up axis on any stage.
- Blender: a chunk param named like one of a node's own properties (`color`, `name`,
  `location`, `width`, ...) was overwritten by the node. Exterior Walls' `color` took the
  node's header grey, so its walls cooked grey in Blender only.
- Blender: a network whose chunks are not on `USD_CHUNK_DIRS` opened as an empty graph
  with only its Output node; the missing chunks were dropped without a word. Each one now
  shows as a red node that names its chunk type, with its wires kept. Open Chunk Network
  warns, and the USDChunks sidebar lists the missing types until they are found.
- Blender: an opened network is framed in the node editor. Its saved positions could be far
  from where the editor starts, so the graph looked empty until Home was pressed.
- Export USD's relative Output Directory or Output File is from the network's folder, like
  every other file path in a param. It was from the working directory, so `output_dir="."`
  wrote a shot wherever a script or `usdchunks run` was started.
- Export USD writes over the files it wrote itself. An unbaked session layer carries the
  network's bookkeeping, so its next export took its own earlier file for a network and
  skipped it. Each file it writes is now stamped as its output; a saved network is not.
- Scatter Points built from a script no longer says "surface prim not found" or "prototype
  path(s) not found" for prims that are above it once it is wired. Its cook before the wire
  now waits instead; a prim still missing once wired is reported as before.
- Closing a script's Scene cooks nothing. Unwiring the chunks recooked what was below them,
  and a referenced group then found the Sublayer above it gone and said it "is blocked" as
  the script ended.
- URDF Import finds a relative Mesh Root from the network's folder, as it does the .urdf.
  It was read from the working directory, so the robot came in without its meshes when the
  network ran from anywhere else.
- URDF Import puts `MeshCollisionAPI` and the Collider approximation only on mesh
  colliders. A box, sphere or cylinder collider collides as itself and carried an
  approximation that meant nothing.
- Dragging a slider on a chunk with Cache Result on no longer writes a cache entry to disk on
  every tick. The entry is written once the chunk stops cooking for half a second, so only
  where the drag lets go is kept. Headless runs still write at once.
- Transform's Aim At no longer fills itself from the selection. In usdview a Transform made with
  a prim selected turned that prim toward itself; Prim Path still takes the selection.
- A network built in a `chunk.change_block()` saves every chunk it made. With department layers
  side by side, the second Layer and its Edit Target were missing from the saved file, and
  the network opened again without what they led to. Edit Target now resolves its layer on
  the scene's stage, not on a cook's throwaway one.
- `usdchunks list` leaves out private modules (a name starting with `_`), which
  `scene.create` refuses to make, so every name it lists can be made from a script.
- An export leaves out the prims a Prune Prim pruned: Export USD (layers and flattened),
  `scene.export` and `usdchunks run -o` kept them as inactive `def`s. A pruned prim inside a
  referenced asset keeps its inactive override, so it stays hidden.
- On Python 3.13 (Blender 5.2 among others) every cook ran on the whole stage. Nothing was
  skipped, and a chunk saw what the chunks below it had done: a Duplicate Prim with a Prune
  Prim below it made inactive copies. Python 3.11 was not affected.
- Export USD never writes over a network file on disk, open on the stage or not. A script
  built with `Scene.new(path=...)` names its root layer after the network file, so
  `scene.commit()` right after `scene.save()` wrote that layer's stripped result over it.
- Scatter Points keeps the rotation of a prototype's source. A cone laid on its side by a
  Transform now scatters on its side; before, only the scale carried over.
- A script building a correctly wired chain no longer gets "X is blocked: ... isn't wired
  above X" or "X reads prims that Y edits, but isn't wired above it" warnings. `scene.create`
  cooks a chunk once before wiring it, and that cook found nothing above it; the warnings
  now wait until the chunk is wired, and one that is not wired still gets them.
- Export USD keeps a relative asset path working when it writes into another folder than the
  network's. A Reference to `inputs/set.usda` exported into `shot/` was written unchanged, and
  USD looked for `shot/inputs/set.usda`. It now names the file it named in the network:
  `../inputs/set.usda` with Relative Paths on, an absolute path with it off.
- Scatter Points follows every Scatter Brush stroke in usdview. With Painted Points wired, only
  the first Points or Spray stroke placed instances; later strokes were skipped. The same
  fix applies to Keep Away From.
- A relative file path in a param resolves from the network file, as in USD, not from the
  working directory: a search path (`inputs/set.usda`) is found in the network's folder, and
  `./` or `../` is relative to the network file. A chunk authors into an anonymous layer, so
  a network only worked when run from the folder its paths were written for. Cooks, the
  stages a chunk reads through, saving, and a file param's existence check all use the
  network's resolver context now; CAD Import, URDF Import and Script Runner find their file
  the same way.
- An exported layer keeps a default prim that one of its sublayers defines. A shot that
  only sublayers its set lost the set's `/World` as default prim, so a Reference to the
  exported shot brought in nothing.
- Duplicate Prim keeps a destination given together with its source. Built in a
  `change_block`, the source's suggested destination (`name_copy`) replaced it.
- A Mesh Cube with 1 for dimx, dimy or dimz wrote no prim at all, and said nothing. They
  count points along an edge, so they now start at 2, a plain box.
- Inline USD warns when its text does not parse; it imported nothing and said nothing, and
  whatever relied on its prims quietly fell back (a Scatter Points to its default cube).
- A Camera made in a `change_block` keeps the focal length and apertures a script gives
  it. The preset menus put the default body's 50 mm back over them when the block ended;
  now only choosing another body or format sets the lens.
- A chunk made by `scene.create` with float params cooked once more per float: USD stores
  them as 32-bit, the read-back (0.16 -> 0.1599999964) counted as an edit. Compared within
  float precision now; a 45k-instance Scatter Points cooks once, not ten times.
- Scatter Points' Keep Away From with Spacing 0 and distance 0 sampled a curve every half
  micrometre -- out of memory on a road. The curve's width sets the step.
- A chunk that sets its own params while it cooks (Light, Wedge) cooked everything below it
  twice -- once in the middle of its own cook, against a half-written layer -- and was left
  dirty, so the next edit of any chunk below it cooked it again. Now what is below it cooks
  once, after it has finished, and a chunk that has just cooked is clean. On an interior set
  with a 5000-instance Scatter, the first edit of a Transform after opening takes 280 ms
  instead of 586 ms, and a Transform edit no longer cooks the Light above it.
- Apply Schema and MDL to MaterialX never cleared their layers, so repeated cooks left
  stale output behind.
- A chunk with no layer of its own, or one in a context's file layer, bumped its claimed
  prim path on every cook (`cam1`, `cam2`, ...) -- its own prim from the last cook read as
  a collision.

- Callbacks whose owner had been garbage collected were invoked with the wrong number of
  arguments, raising `missing 1 required positional argument` during teardown. Such
  connections no longer dispatch at all.
- The node graph's application-level key-event filter is now removed when its view goes
  away. It previously kept the entire view alive and stayed installed for the rest of the
  session.
- Bundled icons and `plugInfo.json` files are included in the wheel; icon lookup resolved
  outside the package and broke on an installed copy.
