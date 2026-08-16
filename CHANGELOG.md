# Changelog

Notable changes to usd_chunks. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the pre-1.0 caveat that
the API may still change between minor versions.

This file starts with the first release; development before that is in the commit history.

## [Unreleased]

### Added

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
