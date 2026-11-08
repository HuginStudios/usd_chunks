# usd_chunks — agent guide

Instructions for AI coding agents (Claude Code, Codex and others) working in this repo.
People are welcome to read it too; [CONTRIBUTING.md](CONTRIBUTING.md) is the human version.

## What usd_chunks is

usd_chunks is the procedural layer for OpenUSD. You build a node network of **chunks**,
each a short Python module, that generates and edits a USD scene. The network is saved in
the `.usda` itself: a chunk is a prim under `/chunks`, a wire is a USD connection, and
each chunk writes into its own layer. The same file opens as a node graph in usdview,
Houdini and Blender, and cooks headless with the `usdchunks` CLI. A chunk module lists its
params in `main()`, cooks in `execute(node)` and describes itself in `config()`; the
framework builds the node, its widgets and its command-line entry point from that.

## Start with the skills

For a common task, read the matching skill in [`skills/`](skills/) before anything else.
Each one is a step-by-step recipe based on how this repo works. The same folder is linked
as `.agents/skills`, `.claude/skills` and `.codex/skills`, so every agent finds it.

| Skill | Use it to |
| --- | --- |
| [`usd-chunks-write-chunk`](skills/usd-chunks-write-chunk/SKILL.md) | Write a new chunk module with `main()`, `execute()`, `config()` and a test |
| [`usd-chunks-build-network`](skills/usd-chunks-build-network/SKILL.md) | Build or edit a network with the `Scene` API from a plain-language description |
| [`usd-chunks-batch`](skills/usd-chunks-batch/SKILL.md) | Set up `usdchunks run` / `batch` / `sweep` commands with param overrides for pipeline jobs |
| [`usd-chunks-review-chunk`](skills/usd-chunks-review-chunk/SKILL.md) | Check a chunk module against this repo's conventions |
| [`usd-chunks-chunkgroup-asset`](skills/usd-chunks-chunkgroup-asset/SKILL.md) | Package a network as a ChunkGroup with a description and lofted params, save it to its own file, and reference it elsewhere |
| [`usd-chunks-add-gizmo`](skills/usd-chunks-add-gizmo/SKILL.md) | Add viewport handles to a chunk, with a test that drives them |

Edit skills in `skills/` only; the three link folders are not copies. If a link shows up
as a small text file containing `../skills` (a Windows checkout without symlink support),
run `python skills/link_skills.py` once to turn it into a working link.

## Repo layout

| Path | What's there |
| --- | --- |
| `usd_chunks/` | The framework. `chunk.py` is the public API chunk authors import; `chunk_core.py` the object model; `chunk_notice.py` notices, callbacks and the `DagNode` graph; `chunk_cook.py` when and how a chunk cooks; `chunk_registry.py` discovery and loading; `chunk_stage.py` the graph on the USD stage; `chunk_script.py` / `chunk_scene.py` the `Scene` scripting API; `cli.py` the `usdchunks` command; `sdf_authoring.py` fast spec writing; `sim_bake.py` long bakes |
| `usd_chunks/graph/` | The toolkit-free graph engine (`engine.py`, imported as `chunk_graph`) and layout |
| `usd_chunks/frontends/` | One subpackage per UI toolkit: `qt/`, `flet/`, `imgui/`, `blender/` |
| `usd_chunks/hosts/` | Per-application entry points: `usdview/`, `houdini/` (Blender's add-on is in `frontends/blender/addon/`) |
| `usd_chunks/gizmo/`, `usd_chunks/store/` | Viewport gizmo declarations and runtime; the store seam behind `usdchunks pin` |
| `chunks/` | Example chunks by category (`core/`, `usd/`, `modelling/`, `materials/`, `dynamics/`, ...). They exercise the framework and are test fixtures |
| `tests/` | The pytest suite, one `test_*.py` per behaviour. Shared helpers: `conftest.py`, `chunk_stubs.py`, `graph_fixtures.py` |
| `examples/scripts/` | Runnable `Scene` scripts (`box.py`, `biome.py`, `shot_setup.py`) |
| `docs/` | The user guide (built with mkdocs). Start with `writing-a-chunk.md`, `chunk-basics.md`, `scripting.md`, `fast-chunks.md`, `core-chunks.md`, `expressions.md`, `install.md` |
| `skills/` | Agent skills, one folder per skill with a `SKILL.md` |

Old module names (`usd_chunks.plugins.*`, `chunk_graph`, ...) still import through
`usd_chunks/_moved.py`; new code uses the new names.

## Set up, test, lint

Python 3.11 or newer. From the repo root:

```
pip install -e ".[dev,ui]"
```

`dev` brings `usd-core` (OpenUSD), pytest, ruff and the libraries the example chunks
import; `ui` brings PySide6. Inside a DCC that ships its own USD (Blender, Houdini), use
the host's `pxr` and never install `usd-core` beside it.

Run the tests with `USD_CHUNK_DIRS` pointing at `chunks/`. Without it the registry finds no
chunk modules and the suite fails with `NoneType 'loaded'`:

```
USD_CHUNK_DIRS=chunks pytest                              # bash
set USD_CHUNK_DIRS=chunks && pytest                       # cmd
$env:USD_CHUNK_DIRS="chunks"; pytest                      # PowerShell
USD_CHUNK_DIRS=chunks pytest -n0 tests/test_apply_schema.py   # one file, one process
```

`pytest.ini` runs on all cores (`-n auto --dist loadfile`). Run the files you touched while
working and the full suite once at the end. On a machine short of memory, pass `-n 4`
instead of all cores. Use `-n0` to see prints or use a debugger. Qt tests run offscreen by
themselves. Tests marked `slow` shell out to pip; skip them with `-m "not slow"`. Tests that
need optional libraries (OCP, MuJoCo, manifold3d, taichi) skip when they're missing.

Lint before you finish; CI runs exactly `ruff check .` and `pytest` (Windows, Python 3.11):

```
ruff check .
```

ruff is pinned in `pyproject.toml`. The house rules it enforces (`ruff.toml`): every module
starts with `from __future__ import annotations`, logging takes %-style args (never an
f-string), modern syntax for the target version, 120-character lines. Run `ruff check
--fix` only on files you changed, not on `.`.

## Rules for chunks

- **A chunk writes only to its own layer.** `execute()` already runs inside the chunk's
  edit context, and the framework clears that layer before each cook, so write the whole
  result every time and don't call `Clear()`. Anywhere else, author through
  `chunk.get_edit_context()`, never the stage's ambient edit target. `interactive=True` in
  `config()` is the exception: the chunk makes a kept edit into a file layer.
- **A chunk stays small and does one job.** Most chunks in `chunks/` are under 500 lines.
  Heavy work goes to compiled code (USD's C++, numpy); many prims go through
  `sdf_authoring` in one `Sdf.ChangeBlock` (see `docs/fast-chunks.md`).
- **Every chunk has a `config()` with a real `description`.** It's the help a person reads
  in the Tab menu and what an AI agent reads to choose a chunk. Also set `category` (a
  `chunk.StandardCategory`) and, where a dependency is needed, `requirements`. Keep
  `config()` self-contained: it runs on its own, before the rest of the module.
- **A new chunk needs a test** in `tests/` that builds it through the `Scene` API and checks
  what it wrote to the stage. See the write-chunk skill.
- **`main()` builds params and never touches the stage.** It runs once per node, before the
  chunk has a prim or a layer.
- **`execute(node)` is deterministic and fast.** Same params, same layer. Read the prims you
  need through prim-path params (`PRIM_SELECTION`), not hard-coded paths or `Traverse()`, or
  the chunk can never be skipped. Write files in `commit(node)`, not in `execute`.
- **Chunks talk only through wires**: an exec input or an `@Node.param@` expression.
  A callback listens to its own chunk; `add_callback` on another chunk's param, or
  `listen_globally` to another chunk's notices, raises. Use `connect_value` to follow
  another chunk's param.
- **Don't import Qt in a chunk.** To get back to the main thread from a worker, use
  `node.get_scene().call_later(fn)`. Long bakes go through `sim_bake`.
- **Imports at module top level.** `usdchunks deps` reads them statically. Guard a
  host-only import (`bpy`, `hou`) with `try`/`except ImportError` and set `hosts=` in
  `config()`.
- **Use `Slider` for numbers** (`is_int=True` for integers), not `Spinbox`.
- **Two public names:** a module's `execute(node)` (you write it, never call it) and
  `chunk.execute()` (call it to re-execute a chunk and what depends on it). Don't add a third.

This repo is the framework, not a chunk library (see CONTRIBUTING.md). A chunk a user asks
for normally goes in their own `USD_CHUNK_DIRS` folder; add to `chunks/` only when the
change is to the examples or fixtures themselves.

## Framework invariants

The non-obvious parts, for changes to `usd_chunks/` itself:

- **Cooking**: when a chunk cooks (batching, stale, held cooks, `dispatch`) is
  `chunk_cook.py`, which imports nothing above `chunk_notice`. How (guards, then the
  module into its layer) is `chunk_cook.cook`, reached through `ChunkBase._cook`.
  Skip-cooking is always on.
- **Main thread only for Qt/USD.** A background thread hops back with
  `scene.call_later(fn)` (+ `threading.Event` to block for a result). It's host-neutral
  (usdview: QTimer, Blender: `bpy.app.timers`), and a debounce cancels via its return value.
- **Graph = USD.** Chunk prims under `/chunks` are the nodes; wires are USD connections
  (`inputs:chunk`/`outputs:chunk` exec + typed `inputs:*`/`outputs:*`); position is
  `UsdUINodeGraphNodeAPI`. A node's body is the real `ChunkWidget` via a proxy widget.
  Graph nodes wrap the real chunk, so they use the same layer.
- **Engine / frontends.** `usd_chunks/graph/engine.py` decides what happens to the graph and
  imports no toolkit (a test enforces it). Frontends decide what appears on screen.
  `GraphFrontend` is a Protocol, not an ABC; `test_graph_frontend_contract.py` checks names
  and signatures. Qt's paste/materialize is deliberately unshared.
- **Output and view slots.** Each context (tab/chunkgroup) has one Output (what it
  delivers) and view slots. The active view drives cooking (`chunk_stage.recook_for_view`)
  and persists as `chunk:activeView`; with none active the context cooks to its Output.
  `usdchunks run` cooks to the Output. **Only what the active view/Output reaches cooks**:
  a new, dropped or pasted node cooks when it is wired in. A frontend holds what it
  creates in a driven graph (`chunk_graph.hold_until_wired`; set values after with
  `set_param_visibly`) and recooks after every wire, delete, create or paste
  (`chunk_graph.recook_active`). A graph with no view or Output (a script) cooks everything.
- **Shared clipboard.** `chunk_graph.node_clipboard`/`edge_clipboard` are module-level;
  assign into them (`copy_to_clipboard`), never rebind.
- **Two callback forms are permanent**: `(notice, sender)` and `(owner, notice, sender)`;
  the framework passes whichever the callback declares. Changing that, or the
  `main()`/`config()`/`execute(node)` module contract, breaks every chunk that exists.

## Changes and pull requests

- Branch off `main` and open a PR; keep each PR to one change.
- A bug fix comes with a test that failed before it and passes after.
- Say how you verified a change: the commands you ran, not just "tests pass".
- Update the README or `docs/` when you change something a user would notice.
- Commit subject about 50 characters (72 hard), prefixed with an emoji (📦 packaging,
  ✨ feature, 🐛 fix, 👷 CI, 📝 docs, ✅ tests), then a blank line and a body wrapped at 72
  explaining *why*.
