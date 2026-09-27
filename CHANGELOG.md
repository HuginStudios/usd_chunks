# Changelog

Notable changes to usd_chunks. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the pre-1.0 caveat that
the API may still change between minor versions.

This file starts with the first release; development before that is in the commit history.

## [Unreleased]

### Added

- `from usd_chunks import Scene` builds and runs chunk networks from a plain script:
  `scene.create.<chunk>()` for every chunk module and menu variant (`scene.create.cube()`,
  `scene.create.rect_light()`), upstream chunks passed positionally, and `with chunk:` to
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

### Changed

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

### Fixed

- Callbacks whose owner had been garbage collected were invoked with the wrong number of
  arguments, raising `missing 1 required positional argument` during teardown. Such
  connections no longer dispatch at all.
- The node graph's application-level key-event filter is now removed when its view goes
  away. It previously kept the entire view alive and stayed installed for the rest of the
  session.
- Bundled icons and `plugInfo.json` files are included in the wheel; icon lookup resolved
  outside the package and broke on an installed copy.
