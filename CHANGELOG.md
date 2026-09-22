# Changelog

Notable changes to usd_chunks. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the pre-1.0 caveat that
the API may still change between minor versions.

This file starts with the first release; development before that is in the commit history.

## [Unreleased]

### Added

- A Reference, Sublayer or Payload chunk pointed at a saved chunk network becomes that
  network's node (a Payload hosts it as a payload arc, so it unloads with the rest). The file's chunkgroup composes in under the chunk's own prim, so the node is the
  group: dive in and the network is there, as if it had been expanded in place. It cooks
  whether or not you dive. Edits inside are overrides in the current file (see below);
  the network file is never written to. Several network files in one Sublayer stack
  under the node in list order, first strongest. Pointed at scene content, both chunks
  behave as before.
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
