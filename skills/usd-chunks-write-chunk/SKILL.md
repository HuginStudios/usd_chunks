---
name: usd-chunks-write-chunk
description: Create a new usd_chunks chunk module (main(), execute(), config()) and a pytest test for it, following the chunks in chunks/. Use when asked to write, add or make a new chunk, node or tool for usd_chunks, or to turn a USD script into a chunk. Only once `usdchunks list <word>` shows no chunk that does the job: to use existing chunks, read usd-chunks-build-network instead.
---

# Write a chunk

A chunk is one Python module: `main()` builds its params, `execute(node)` cooks it into the
chunk's own layer, and `config()` says what it is. The full guide is
`docs/writing-a-chunk.md`; the param types are in `docs/chunk-basics.md`.

## 1. Check that no chunk does it already

Before writing anything, search for the job in its own words:

```
usdchunks list urdf          # every chunk whose name or description mentions it
usdchunks show urdf_import   # one in full, with its params
```

The repo has chunks for big jobs too: importing a URDF robot or a CAD file, posing a
robot, scattering on a surface. A chunk with a menu entry or a param that already does the
job needs no new one; build with it (the `usd-chunks-build-network` skill). Write a chunk
only for a step nothing does, and say which chunks you checked.

## 2. Pin down what it does

- Decide its **inputs** (which prims it reads, through prim-path params), its **params**
  (the values a person changes) and its **output** (the prims or attributes it writes, or an
  `OUTPUT` param other chunks read as `@Node.param@`).
- Decide where it goes. A user's own chunk goes in a folder on their `USD_CHUNK_DIRS`.
  `chunks/<category>/` is only for this repo's examples and fixtures (see CONTRIBUTING.md).
  Ask if it's unclear.
- The file name is the chunk's module name and its `scene.create.<name>()` function, so
  use `snake_case` and don't reuse an existing name.

## 3. Read two neighbours

Read one or two chunks in the same category that do something similar, and copy their
style. Small, current examples:

- `chunks/utils/random_value.py`: an `execute` that only sets an `OUTPUT` param.
- `chunks/usd/reference.py`: authors prims from a file and a prim-path param.
- `chunks/usd/prune_prim.py`: loops over target prims with
  `chunk_utils.author_in_variant_contexts`.

## 4. Write the module

```python
"""Make Grid chunk -- one line on what it does.

A few lines on anything a reader needs to know.
"""

from __future__ import annotations

from pxr import UsdGeom

from usd_chunks import chunk


def execute(node: chunk.Chunk) -> None:
    path = node.get_param("primpath_location").value
    if not path:
        return
    stage = node.get_stage()
    mesh = UsdGeom.Mesh.Define(stage, path)
    ...


def main():
    chunk_api = chunk.Chunk("Make Grid")
    chunk_api.add_params([
        chunk.LineEdit("primpath_location", "Location", default_value="/grid",
                       flags=chunk.ChunkFlags.ParamFlags.LABELED),
        chunk.Slider("divisions", "Divisions", default_value=8, min_value=1, max_value=64,
                     is_int=True, flags=chunk.ChunkFlags.ParamFlags.LABELED),
    ])
    return chunk_api


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        icon="cube",
        description="Makes a flat grid mesh at Location with the given number of divisions.",
    )
```

Rules, all from `docs/writing-a-chunk.md`:

- **`from __future__ import annotations` first** (ruff's `I002`), and every import at
  module top level. Guard a host-only import (`bpy`, `hou`) with `try`/`except ImportError`
  and set `hosts=[...]` in `config()`.
- **`main()`** takes no arguments, returns a `chunk.Chunk`, and only builds params, groups
  and callbacks. It never touches the stage. It runs once per node, so cache anything slow
  at module level.
- **`execute(node)`** runs inside the chunk's edit context, and its layer has already been
  cleared. Write the whole result every time, with no `Clear()`. Same params give the same
  result. Use `node.get_stage()` and author normally.
- **Read inputs through params.** A prim the chunk reads comes from a `LineEdit` with
  `PRIM_SELECTION` (or `primpath_location`). A hard-coded path or `stage.Traverse()` still
  works, but the chunk then cooks every time, even when nothing it reads changed.
- **Params:** `Slider` for numbers (`is_int=True` for ints), never `Spinbox`. Results other
  chunks read get the `OUTPUT` flag. A value that shouldn't be saved gets `TRANSIENT`.
- **Callbacks** listen to this chunk's own params only. Use them for buttons; anything that
  must be redone after the file opens belongs in `execute`.
- **`config()`** is self-contained: no module constants or helpers that need a non-`usd_chunks`
  import. Give it a `description` that opens with one **summary line**: the jobs the chunk
  does, in the words someone asking for them would use ("Copies a prim N times in a row,
  ring or spiral (stairs); ..."), at most 90 characters, no "X chunk --" prefix, then a blank
  line, then what it writes and its params. The summary line sums up that text: nothing in
  it the text below does not say. `usdchunks list` shows only that line, and it
  is what an agent picks a chunk by. Also give it a
  `category`, and an `icon` from `usd_chunks/resources/icons/node/` (file name without
  `.svg`). Third-party packages go in `requirements=["numpy>=2,<3"]` with a version range.
- **Writing files** (export, render) goes in `commit(node)`, not `execute`.
- **Many prims or points:** use `sdf_authoring.Writer` inside one `Sdf.ChangeBlock`, and
  numpy for the maths (`docs/fast-chunks.md`). A bake that takes seconds goes through
  `sim_bake.run`. Never import Qt; use `node.get_scene().call_later(fn)` to get back to the
  main thread.
- **Keep it small.** One job per chunk. If it grows past a few hundred lines, look for a
  second chunk hiding in it.

## 5. Write the test

Add `tests/test_<module>.py`. Build the chunk through the `Scene` API on an in-memory stage
and check what it wrote. The pattern from `tests/test_apply_schema.py`:

```python
"""Make Grid writes a mesh at its location and rebuilds it when a param changes."""

from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom

from usd_chunks.chunk_script import Scene


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    s = Scene(stage, "pytest")
    yield s
    s.close()


def test_it_writes_a_mesh_at_its_location(scene):
    scene.add("make_grid", primpath_location="/geo/grid", divisions=4)

    mesh = UsdGeom.Mesh(scene.get_stage().GetPrimAtPath("/geo/grid"))
    assert mesh
    assert len(mesh.GetPointsAttr().Get()) == 25


def test_a_new_location_moves_it(scene):
    grid = scene.add("make_grid", primpath_location="/a")
    grid.get_param("primpath_location").value = "/b"

    stage = scene.get_stage()
    assert stage.GetPrimAtPath("/b")
    assert not stage.GetPrimAtPath("/a")   # the old result was cleared
```

Test what a user would see: the prims and values on the stage, a re-cook after a param
change, and the empty or invalid input (nothing picked yet) not raising. Name each test as a
sentence about the behaviour. A chunk outside `chunks/` needs its folder on
`USD_CHUNK_DIRS` for `scene.add` to find it.

## 6. Check it

```
USD_CHUNK_DIRS=chunks pytest -n0 tests/test_<module>.py
ruff check chunks/<category>/<module>.py tests/test_<module>.py
usdchunks show <module>     # what someone looking for a chunk will see: description, params
```

If the chunk goes in this repo's `chunks/`, add it to `CHANGELOG.md` under Unreleased →
Added; when changing an existing one there, under Changed or Fixed, with `**Breaking:**`
first if saved networks or scripts using it now behave differently.

Then run the `usd-chunks-review-chunk` skill on the new module. Tell the user what you
ran and the result. Mention any test you couldn't run (for example, no `pxr` in this
Python).
