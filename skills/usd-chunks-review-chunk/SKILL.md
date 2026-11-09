---
name: usd-chunks-review-chunk
description: Review a usd_chunks chunk module (and its test) against this repo's conventions -- own-layer authoring, small single-purpose modules, a self-contained config() with a description, wires-only communication, Slider params, top-level imports, and a test. Use when asked to review, check, audit or tidy a chunk, or after writing one.
---

# Review a chunk

Check one chunk module against the rules in `AGENTS.md` and `docs/writing-a-chunk.md`.
Report what you find; don't rewrite the module unless the user asks.

## 1. Read it and run the tools

Read the whole module and any test that covers it (`grep -rln "<module_name>" tests/`).
Then:

```
ruff check <module.py>
usdchunks deps                 # undeclared or unpinned third-party imports, all chunks
usdchunks show <module>        # the description and params as someone finding the chunk sees them
```

`usdchunks deps` needs `USD_CHUNK_DIRS` to include the module's folder.

## 2. Go through the checklist

**Module shape**

- [ ] `main()` takes no arguments and returns a `chunk.Chunk`.
- [ ] `main()` only builds params, groups and callbacks. Nothing in it touches the stage.
      Nothing slow runs per call (it runs once per node).
- [ ] `execute(node)` exists if the chunk authors anything that must come back when the
      file is reopened. Work done only in a callback is lost on load.
- [ ] No module-level function called `execute` that's meant to be a normal callback (the
      name is reserved).
- [ ] Files on disk are written in `commit(node)`, not in `execute`.

**Its own layer**

- [ ] `execute` authors through `node.get_stage()` in the edit context it's given, and
      doesn't change the stage's edit target or write to another layer (unless
      `interactive=True`).
- [ ] `execute` doesn't `Clear()` its layer (the framework already did). Authoring from a
      callback goes through `owner.get_edit_context()`.
- [ ] Same params give the same result: no unseeded randomness, wall-clock time or
      ordering that depends on dict or set order.

**Reads and wires**

- [ ] Prims it reads come from prim-path params (`PRIM_SELECTION`, `primpath_location`),
      not hard-coded paths or `stage.Traverse()`. Otherwise it can never skip a cook.
- [ ] Callbacks listen only to this chunk's own params. There's no `listen_globally` except
      for stage/host events, and no `add_dependency` on another chunk's param.
- [ ] A stage listener only sets this chunk's params, writes only on a real change, and
      waits for `CHUNK_LOADED`.
- [ ] Results other chunks need are `OUTPUT` params or prims, reached by a wire.

**Params**

- [ ] Numbers use `Slider` (`is_int=True` for ints), not `Spinbox`.
- [ ] Each param a person sees has a readable label and, where it isn't obvious, a
      `tooltip`.
- [ ] Session-only values are `TRANSIENT`; computed results are `OUTPUT`.

**`config()`**

- [ ] It exists and has a `description` that says what the chunk does and what it writes,
      not the default `"Chunk description..."`.
- [ ] The description opens with one summary line, then a blank line: the jobs the chunk
      does in task words, at most 90 characters, no "X chunk --" prefix, and nothing the
      text below it does not say. Check it with
      `usdchunks list <word>` using a word someone would search for; `tests/test_chunk_summary_lines.py`
      enforces the shape.
- [ ] It sets `category` (a `chunk.StandardCategory` member where one fits).
- [ ] It's self-contained: it uses only `usd_chunks` names, not module constants or
      helpers that need other imports.
- [ ] Third-party packages are listed in `requirements` with a version range. Host-only
      modules are imported under `try`/`except ImportError`, with `hosts=` set.
- [ ] `icon`, if set, is a name in `usd_chunks/resources/icons/node/` or a path to an image.

**Code**

- [ ] `from __future__ import annotations` comes first, and all imports are at module top
      level (`usdchunks deps` reads them statically).
- [ ] No Qt import. Main-thread hops use `node.get_scene().call_later`, and long bakes use
      `sim_bake`.
- [ ] Logging uses %-style args, never f-strings.
- [ ] Size: one job. More than about 500 lines is a reason to ask whether it should be two
      chunks or a helper. Many prims are authored through `sdf_authoring` in one
      `Sdf.ChangeBlock`; loops over points use numpy (`docs/fast-chunks.md`).

**Test**

- [ ] A test in `tests/` builds the chunk through `Scene` / `scene.add(...)` and asserts on
      the stage. It covers a re-cook after a param change and the empty input (nothing
      picked) without errors.
- [ ] It passes: `USD_CHUNK_DIRS=chunks pytest -n0 tests/test_<module>.py`.

## 3. Report

List the findings most important first, each with `file:line`, the rule it breaks, and
the fix in one line. Say which checks pass, so the user knows what you looked at. Give the
`ruff`, `deps` and test results as you saw them. A missing `config()` description, authoring
outside the chunk's layer, and a missing test always count as findings. Older chunks in
`chunks/` don't all follow every rule (some still use `Spinbox`), so don't copy a pattern
just because a neighbour does it.
