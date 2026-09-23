# Writing a chunk module

A chunk module is a plain `.py` file. Drop it anywhere under
[`USD_CHUNK_DIRS`](install.md#environment-variables) and it is discovered automatically —
there is no registration step, no `plugInfo.json`, nothing to import it from elsewhere.

A module can define up to four top-level functions. Only `main()` is required.

| Function | Required | Called |
| --- | --- | --- |
| `main()` | yes | Once, at discovery, to build the `Chunk` object. |
| `config()` | no | Once, at discovery, for load-time configuration ([`ChunkConfig`](#config)). |
| `execute(node)` | no | Whenever the chunk needs to (re)cook — see [`execute()`](#execute). |
| `commit(node)` | no | Once per chunk, after a solve has settled — see [`commit()`](#commit). |

## `main()`

```python
from usd_chunks import chunk


def main():
    button = chunk.Button("add_cube", "Add Cube")
    button.add_callback(add_cube)

    chunk_api = chunk.Chunk("Add Cubes")
    chunk_api.add_param(button)
    return chunk_api
```

`main()` takes no arguments and must return a [`chunk.Chunk`][usd_chunks.chunk.Chunk] — a
`Group` with some extra bookkeeping (it holds the layer, the DAG node, the exec ports).
Build its params and groups here and wire up their callbacks with `add_param` /
`add_callback`.

**Do not touch the stage or the scene from `main()`.** It runs at module discovery, before
a chunk has a prim or a stage to belong to — `chunk.get_scene()` and a chunk's
`get_stage()` are only meaningful once the chunk actually exists on one. Anything that
needs the stage belongs in a callback, in `execute()`, or in `commit()`.

See [`docs/example.md`](example.md) for a longer, annotated tour of the param types,
groups, flags and callback signatures available inside `main()`.

## `config()`

```python
from usd_chunks import chunk


def config():
    return chunk.DefaulConfig(
        category=chunk.StandardCategory.MODELLING,
        description="Clears all authored transform ops on the target prim.",
    )
```

`config()` returns a [`chunk.DefaulConfig`][usd_chunks.chunk.DefaulConfig] (a thin
subclass of [`chunk_core.ChunkConfig`][usd_chunks.chunk_core.ChunkConfig]) describing how
the chunk should be treated at load time and offered in menus. A module with no `config()`
gets the defaults below.

| Field | Default | Meaning |
| --- | --- | --- |
| `category` | `"Chunks"` | Where the chunk is filed in the create menu. Use a [`StandardCategory`][usd_chunks.chunk.StandardCategory] member (`MODELLING`, `DYNAMICS`, `TRANSFORM`, `UTILS`, `RIGS`, `USER`, `EXAMPLES`, `USD`, `PROPERTIES`, `USD_RENDER`, `USD_LAYER`, `CORE`, `MATERIALS`, `MTLX`) or a plain string for your own. |
| `description` | `"Chunk description..."` | Shown in tooltips and the create menu. `ChunkGroup`'s `doc` param reuses this same text as the network-interface description a human or an LLM reads to understand what a saved network does. |
| `load_for_plugin` | `None` | Restrict discovery to one host plugin (e.g. `"chunk_usdview"`), if the chunk only makes sense there. |
| `requirements` | `[]` | pip requirements the module needs, e.g. `["numpy>=2,<3"]`. A module that fails to import (because one is missing) is skipped rather than crashing discovery; with [`USD_CHUNK_AUTO_INSTALL`](install.md#environment-variables) set, missing ones are offered for install into `USD_CHUNK_DEPENDENCY_DIR`. |
| `required_modules` | `[]` | Other chunk *modules* this one depends on being registered. |
| `interactive` | `False` | The chunk writes to a file layer that persists, instead of its own layer being cleared and regenerated on every cook — see *Layers* in the [README](/README.md#how-it-works). Set this for a chunk whose job is a one-off authored edit rather than a repeatable cook. |
| `hidden` | `False` | Kept out of the create menu. For a type that only exists to be saved and reloaded rather than created by hand — `runtime`, the type a chunk built in the interpreter is written as (see *Building a chunk at runtime* in [`docs/index.md`](index.md)). It must still be a registered module, since that's how a reload finds what to rebuild a prim from. |
| `terminal` | `False` | No `outputs:chunk` is authored — the chunk ends an exec chain rather than passing it on. The [View](core-chunks.md#view) chunk sets this. |
| `exec_inputs` | `1` | How many *positional* exec input slots the chunk has. `1` is the ordinary case — one `inputs:chunk` port that can still carry several wires stacked on it. Above `1`, each slot gets its own port and which wire lands where is meaningful (a Stage Expression Filter's slot 0 is the stream it always forwards, slot 1 the branch it gates). |
| `dynamic_exec_inputs` | `False` | The chunk grows a fresh numbered exec input port per incoming wire, plus one empty trailing slot to attach the next branch to. For a chunk like Merge or Switch where every upstream branch needs to be its own port rather than several wires stacked on one — the slot count isn't known until the graph is wired, unlike `exec_inputs`. |
| `outputs` | `[]` | Extra typed output ports beyond `outputs:chunk`, as `[(name, SdfTypeName), ...]`. |
| `variants` | `[]` | Extra entries in the chunk's create-menu submenu, as `[(label, {param: value}), ...]`, or a callable returning that list. Pass a callable when building the list is expensive (enumerating a shader registry, say) — it then only runs the first time a menu is actually drawn, not on every headless discovery. |
| `gizmos` | `[]` | Viewport handles driven by this chunk's params — see [Chunk gizmos: the host contract](gizmo-host-contract.md). |

## `execute()`

```python
def execute(node: chunk.Chunk) -> None:
    ...
```

`execute` is a **reserved name** at module scope — don't give a plain callback this name,
even one that isn't meant to be the reactive hook, or the framework will call it as one.

If defined, `execute(node)` runs whenever the chunk needs to cook: when a dependency
changes (`add_dependency`), when the chunk is reloaded (its param *values* are restored on
load, but callback side effects are not replayed — see below), or as part of an explicit
re-run of the exec chain. `node` is the chunk itself — the same object `main()` returned —
so the usual body is `node.get_stage()` / `node.get_edit_context()` plus whatever the
chunk actually does to the stage.

It runs inside a `Usd.EditContext` already pointed at the chunk's own layer (unless the
chunk is `interactive`, which authors elsewhere), and it must be **idempotent**: cheap
enough to re-run constantly while the graph solves, and safe to run twice in a row with
the same inputs. The usual pattern is to clear the chunk's own layer first and re-author
from scratch, rather than trying to patch up whatever was there before.

A very common shape — and the reason `execute` exists at all, rather than only ever
running from callbacks — is a chunk whose *real* work already happens in a param callback,
with `execute` there purely to repeat that work after a reload:

```python
def _run(owner, notice, sender):
    ...
    UsdGeom.Xformable(prim).ClearXformOpOrder()


def execute(node: chunk.Chunk) -> None:
    """load_chunks calls this once per chunk after bulk-restoring its param values, so a
    reloaded chunk actually redoes its work — the real logic lives in _run, a Button
    callback, which load's value-restore deliberately does not trigger."""
    _run(node, None, None)


def main():
    chunk_api = chunk.Chunk("Reset Transform")
    ...
    run_btn.add_callback(_run)
    return chunk_api
```

(the full version is [`scripts/transforms/reset_transform.py`](/scripts/transforms/reset_transform.py)).
A save only ever records a chunk's **data** — its params, values and wiring — never the
code path that produced them (see *What a save keeps* in [`docs/index.md`](index.md)).
`execute` is what makes a reload behave, not just look, the way it did before saving.

## `commit()`

```python
def commit(node: chunk.Chunk) -> None:
    ...
```

`commit` is for a chunk whose real work is writing a *file*, as opposed to authoring into
its own USD layer — Export USD, a render-product chunk. It is a separate hook from
`execute` because the two run at different times for different reasons:

- `execute` is dispatched from inside the notice/DAG solve, over and over, as params
  settle — the right place for cheap, repeatable USD authoring, the wrong place for a disk
  write.
- `commit` is called once, by whoever drove the solve (`usdchunks run`, or a host's Commit
  action), **after** the solve has settled — every chunk in topological order, so a chunk
  that reads another chunk's output sees the finished result.

No edit context is opened around a `commit` call: it reads the fully composed stage and
writes to disk, rather than authoring into a layer. A module with no `commit` is simply
skipped — most chunks have nothing to write, and the hook is entirely opt-in. A disabled
chunk (or one blocked by sitting outside the active [view](core-chunks.md#view)'s tree)
never commits.

## Discovery, recap

`USD_CHUNK_DIRS` is scanned recursively for `.py` files ([Environment
variables](install.md#environment-variables) has the full precedence rules against
`GLOBAL_USDCHUNKS`). Any file whose `main()` returns a `Chunk` is registered under that
module's name — no directory structure or naming convention is required beyond that.
