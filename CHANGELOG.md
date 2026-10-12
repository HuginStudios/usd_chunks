# Changelog

Notable changes to usd_chunks. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the pre-1.0 caveat that
the API may still change between minor versions.

This file starts with the first release; development before that is in the commit history.

## [Unreleased]

### Added

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
- Mesh has one menu entry per shape: Cube, Grid, Tube and Ground.
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

- Inline USD warns when its text does not parse; it imported nothing and said nothing, and
  whatever relied on its prims quietly fell back (a Scatter Points to its default cube).
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
