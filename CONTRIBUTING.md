# Contributing to usd_chunks

This repository is the usd_chunks **core**: the chunk runtime, the notice and DAG system,
the USD layer handling, the host integrations, and the `usdchunks` CLI. Contributions to
those are very welcome.

**It is not a place to publish chunks.** Chunks you write live in your own
`USD_CHUNK_DIRS` and are distributed separately — the modules under [chunks/](chunks/)
exist to exercise and demonstrate the core, not as a chunk library. Fixes to the existing
examples are welcome; new ones are usually not what this repository is for. If a chunk you
wrote exposes a gap in the core, that gap is the contribution.

## Getting set up

```
git clone https://github.com/HuginStudios/usd_chunks.git
cd usd_chunks
pip install -e ".[dev,ui]"
```

That gives you the library, the `usdchunks` CLI, OpenUSD (`usd-core`, pulled in by `dev`)
and PySide6 for the widget tests. Python 3.10 or newer is required.

`usd-core` is not a hard dependency of the package — it lives in the `standalone` extra,
because a DCC that already ships USD must use its own rather than have a second one
installed beside it. Running the suite inside such a host needs neither extra.

Note that the `usd-core` wheels on PyPI do **not** include usdview. The library, the CLI
and the whole test suite work without it, but if you are changing the usdview integration
specifically you will need a full USD build providing `pxr.Usdviewq`.

## Running the tests

```
USD_CHUNK_DIRS=chunks pytest
```

`USD_CHUNK_DIRS` must point at `chunks/` or the registry finds no chunk modules and the
suite fails with a confusing `NoneType 'loaded'`. On Windows:

```
set USD_CHUNK_DIRS=chunks
pytest
```

Qt tests run offscreen automatically — `tests/conftest.py` sets `QT_QPA_PLATFORM`, so no
display is needed.

Lint before pushing, since CI enforces it:

```
ruff check .
```

CI runs one row — Windows, Python 3.11, the version the DCCs ship (Houdini 20.5, Maya
2025) — and it runs exactly two commands: `ruff check .` and `pytest`.

## Finding your way around

- `usd_chunks/` — the core. `chunk_core.py` (the object model), `chunk_notice.py` (notices,
  callbacks and the DAG), `chunk_registry.py` (discovery and loading), `chunk_stage.py` (the chunk graph on
  the USD stage: sync, wiring, view-driven cooking), `chunk.py` (the public API that chunk
  authors import).
- `usd_chunks/frontends/` — one subpackage per UI toolkit (`qt/`, `flet/`, `imgui/`,
  `blender/`), each with a widget `factory.py` and a `nodegraph.py` over `graph/engine.py`.
- `usd_chunks/hosts/` — per-application entry points: `usdview/`.
- `usd_chunks/cli.py` — the headless `usdchunks` command.
- `usd_chunks/graph/`, `usd_chunks/gizmo/`, `usd_chunks/store/` — the graph engine and
  layout, the gizmo declaration and runtime, and the store seam with `usdchunks pin`.
- Old module names (`usd_chunks.plugins.*`, `chunk_graph`, ...) still import through
  `usd_chunks/_moved.py`; new code uses the new names.
- `tests/` — run with `USD_CHUNK_DIRS=chunks`.
- `chunks/` — example chunks, used heavily as test fixtures.

Two contracts worth knowing before changing the core, because chunks in the wild depend on
them: a chunk module exposes `main()` (no arguments, returns a `Chunk`), optionally
`config()` and `execute(node)`; and a callback may declare either `(notice, sender)`,
matching USD's `Tf.Notice`, or `(owner, notice, sender)`, receiving whichever it declares.
Changing either is a breaking change for every chunk that exists.

## Why attrs

Chunk classes use [python-attrs](https://www.attrs.org/en/stable) instead of hand-written
`__init__` methods. Chunk classes have a lot of attributes, and each level of the class
hierarchy (base classes → `Chunk`/`Group` → the param types) adds more. attrs combines
them into one generated `__init__`, so a subclass doesn't repeat its parent's attributes,
and you get validation and conversion as well.

The downside is that there's no `__init__` to read for the signature, so use static type
checking (Pylance) to see it. Most of attrs is hidden away. Usually the only part you use
directly is `attrs.field()` on a class attribute, and only when adding a new param type or
extending `chunk_core`.

## Pull requests

- Branch off `main` and open a PR against it.
- Keep the change focused. A packaging fix and a new feature belong in separate PRs.
- Add a test when you fix a bug. The best regression test is one you have watched fail
  before your change and pass after it.
- Say how you verified it. "Tests pass" is less useful than what you actually ran.
- Update the README when you change behaviour a user would notice.

Commit messages follow the standard git convention: a subject of 50 characters or so
(72 hard), a blank line, then a body wrapped at 72 explaining *why*. This repository also
prefixes subjects with an emoji (📦 packaging, ✨ feature, 🐛 fix, 👷 CI, 📝 docs, ✅ tests).

## Reporting bugs

Open an issue using the bug template. The two things that make a report actionable are the
exact command or steps, and your environment — OS, Python version, and whether you are
running under usdview, another DCC, or the CLI.

## License

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same terms as the project.
