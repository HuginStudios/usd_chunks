# Contributing to usd_chunks

This repository is the usd_chunks **core**: the chunk runtime, the notice and DAG system,
the USD layer handling, the host integrations, and the `usdchunks` CLI. Contributions to
those are very welcome.

**It is not a place to publish chunks.** Chunks you write live in your own
`USD_CHUNK_DIRS` and are distributed separately — the modules under [scripts/](scripts/)
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
USD_CHUNK_DIRS=scripts pytest
```

`USD_CHUNK_DIRS` must point at `scripts/` or the registry finds no chunk modules and the
suite fails with a confusing `NoneType 'loaded'`. On Windows:

```
set USD_CHUNK_DIRS=scripts
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

## Git hooks

Optional, per clone, and worth it — especially while CI is unavailable:

```
sh hooks/install.sh
```

Each clone runs that once; a repository cannot install hooks on your behalf, since
`core.hooksPath` is local config by design. Nothing is copied — the hooks stay reviewable
in git and update when you pull. Undo with `git config --unset core.hooksPath`.

These are plain git hooks, not the `pre-commit` framework: there is nothing extra to
install beyond `ruff` and `pytest`, which the `dev` extra already brings.

What you get is CI's two commands, split by what each one costs:

| | runs | takes | default |
|---|---|---|---|
| `pre-commit` | ruff on the staged Python, a core smoke set, and any test file the commit touches | ~3s | **on** |
| `pre-push` | `ruff` on tracked Python, then the full suite | ~7min | off |

The full-suite check is off unless you ask for it — `sh hooks/install.sh --with-prepush`,
or `git config usdchunks.prepush true` later. Seven minutes on every push is how a hook
gets uninstalled, taking the fast checks with it.

Skip any hook once with `--no-verify`; `USD_CHUNKS_SKIP_PREPUSH=1` also skips the slow one.

The suite is not green today, so `pre-push` blocks only on failures that are *not* in
`hooks/known-failures.txt`. That file is a debt list with a note on why each entry fails;
it should only ever shrink, and the hook tells you when an entry starts passing so it can
be deleted.

Hooks already in `.git/hooks` keep working — `core.hooksPath` replaces that directory
rather than adding to it, so each hook here runs its `.git/hooks` counterpart first.

## Finding your way around

- `usd_chunks/` — the core. `chunk_core.py` (the object model), `chunk_notice.py` (notices,
  callbacks and the DAG), `chunk_registry.py` (discovery and loading), `chunk_stage.py` (the chunk graph on
  the USD stage: sync, wiring, view-driven cooking), `chunk.py` (the public API that chunk
  authors import).
- `usd_chunks/frontends/` — one subpackage per UI toolkit (`qt/`, `flet/`, `imgui/`,
  `blender/`), each with a widget `factory.py` and a `nodegraph.py` over `graph/engine.py`.
- `usd_chunks/hosts/` — per-application entry points: `usdview/`, `kraken/`, `houdini/`.
- `usd_chunks/cli.py` — the headless `usdchunks` command.
- `usd_chunks/graph/`, `usd_chunks/gizmo/`, `usd_chunks/store/` — the graph engine and
  layout, the gizmo declaration and runtime, and the store seam with `usdchunks pin`.
- Old module names (`usd_chunks.plugins.*`, `chunk_graph`, ...) still import through
  `usd_chunks/_moved.py`; new code uses the new names.
- `tests/` — run with `USD_CHUNK_DIRS=scripts`.
- `scripts/` — example chunks, used heavily as test fixtures.

Two contracts worth knowing before changing the core, because chunks in the wild depend on
them: a chunk module exposes `main()` (no arguments, returns a `Chunk`), optionally
`config()` and `execute(node)`; and a callback may declare either `(notice, sender)`,
matching USD's `Tf.Notice`, or `(owner, notice, sender)`, receiving whichever it declares.
Changing either is a breaking change for every chunk that exists.

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
