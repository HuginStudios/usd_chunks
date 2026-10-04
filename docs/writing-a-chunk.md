# Writing a chunk module

> In short: a chunk is a Python module. `main()` sets up its params, `execute(node)` does
> the work, and `config()` holds details like its `description` and `category`.

This page is about writing a new chunk. To make and wire chunks you already have from
Python, see [Scripting](scripting.md).

A chunk module is a plain `.py` file. Put it anywhere in a
[`USD_CHUNK_DIRS`](install.md#environment-variables) folder and it's found automatically.
You don't need to register it, write a `plugInfo.json`, or import it anywhere.

A module can define up to four top-level functions. Only `main()` is required.

| Function | Required | Called |
| --- | --- | --- |
| `main()` | yes | Once per **instance**, to build a `Chunk` object. Each node you add to the graph, and each chunk prim loaded from a file, is a separate `main()` call. |
| `config()` | no | Once, when the module is found, for its settings ([`ChunkConfig`](#config)). |
| `execute(node)` | no | Every time the chunk cooks. See [`execute()`](#execute). |
| `commit(node)` | no | Once per chunk, after everything has finished cooking. See [`commit()`](#commit). |

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

`main()` takes no arguments and must return a [`chunk.Chunk`][usd_chunks.chunk.Chunk].
A `Chunk` is a `Group` that also keeps track of its layer, its DAG node and its exec ports.
Create its params and groups here, and add their callbacks with `add_param` and
`add_callback`.

**`main()` runs once per instance, not once when the module is found.** Finding the module
only imports it and checks that `main()` exists. After that, `main()` is called every time
a chunk is created: once for each node you add, and once for each saved chunk prim when a
file is opened. So anything slow in `main()`, like building a lookup table, runs again for
every node. If that matters, cache it at module level.

**Don't change the stage in `main()`.** It runs *before* the chunk has its prim and its
own layer, so anything it writes lands in the wrong place. Anything that changes the stage
goes in a callback, in `execute()`, or in `commit()`.

[`docs/example.md`](example.md) has a longer example with notes on the param types,
groups, flags and callback signatures you can use in `main()`.

## `config()`

```python
from usd_chunks import chunk


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.MODELLING,
        description="Clears all authored transform ops on the target prim.",
    )
```

`config()` returns a [`chunk.DefaultConfig`][usd_chunks.chunk.DefaultConfig], a small
subclass of [`chunk_core.ChunkConfig`][usd_chunks.chunk_core.ChunkConfig]. It describes how
the chunk is loaded and how it shows up in menus. A module without `config()` gets the
defaults below.

| Field | Default | Meaning |
| --- | --- | --- |
| `category` | `"Chunks"` | Where the chunk appears in the create menu. Use a [`StandardCategory`][usd_chunks.chunk.StandardCategory] member (`MODELLING`, `DYNAMICS`, `TRANSFORM`, `UTILS`, `RIGS`, `USER`, `EXAMPLES`, `USD`, `PROPERTIES`, `USD_RENDER`, `USD_LAYER`, `CORE`, `MATERIALS`, `MTLX`) or a plain string for your own. |
| `description` | `"Chunk description..."` | Shown in tooltips and the create menu. `ChunkGroup`'s `doc` param uses the same text to describe what a saved network does, for a person or an AI reading the file. |
| `load_for_plugin` | `None` | Only load the chunk in one host plugin (e.g. `"chunk_usdview"`), if it only works there. |
| `requirements` | `[]` | pip packages the module needs, e.g. `["numpy>=2,<3"]`. If one is missing and the module fails to import, it's skipped instead of crashing. usdview and `usdchunks run` ask whether to install the missing ones into `USD_CHUNK_DEPENDENCY_DIR` (see [`USD_CHUNK_AUTO_INSTALL`](install.md#environment-variables)). |
| `required_modules` | `[]` | Other chunk *modules* that must be loaded for this one to work. |
| `interactive` | `False` | The chunk writes to a file layer that is kept, instead of its own layer being cleared and rebuilt on every cook. Use it for a chunk that makes a one-off edit rather than something that is re-cooked. |
| `cache_cook` | `False` | Keep this chunk's cooked result on disk and read it back instead of cooking again, when nothing it depends on has changed. For a slow cook whose result depends only on its params, the files they name and the stage's units. See [Caching a slow cook](#caching-a-slow-cook). |
| `hidden` | `False` | Left out of the create menu. For a type that is only saved and loaded, never created by hand, like `runtime`, the type a chunk built in Python is saved as (see [Building a chunk at runtime](runtime-chunks.md)). It still has to be a registered module, because that's how loading a file knows what to rebuild. |
| `terminal` | `False` | The chunk has no `outputs:chunk`, so it ends an exec chain. The [View](core-chunks.md#view) chunk uses this. |
| `exec_inputs` | `1` | How many numbered exec input ports the chunk has. `1` is the normal case: one `inputs:chunk` port, which can still take several wires. With more than `1`, each slot gets its own port and it matters which wire goes where. For example, Stage Expression Switch has a normal path on slot 0 and a matched path on slot 1, and only one of them runs. |
| `dynamic_exec_inputs` | `False` | The chunk adds a new numbered exec input port for each wire connected to it, plus one empty port for the next one. For chunks like Merge or Switch, where each incoming branch needs its own port. Unlike `exec_inputs`, the number of ports isn't fixed. |
| `outputs` | `[]` | Extra typed output ports beyond `outputs:chunk`, as `[(name, SdfTypeName), ...]`. |
| `variants` | `[]` | Extra entries in the chunk's submenu in the create menu, as `[(label, {param: value}), ...]`, or a function that returns that list. Use a function when the list is slow to build (for example, listing a shader registry). It then only runs the first time the menu is shown, not every time chunks are loaded. |
| `gizmos` | `[]` | Viewport handles that control this chunk's params. See [Chunk gizmos: the host contract](gizmo-host-contract.md). |

**Keep `config()` self-contained.** When the module is found, `config()` is run on its
own, with only the module's `usd_chunks` imports done. This way, reading a module's config
never runs the rest of its code. If `config()` uses a constant or helper from the module,
the first try fails with `NameError`. The registry then tries once more after running the
module's top-level assignments and `def`s. If any of those need a non-`usd_chunks` import,
that fails too, and the module falls back to the `DefaultConfig()` defaults: wrong
category, generic description, and only a warning in the log. If `config()` needs
something slow to build, pass a function to `variants` (see the table above) instead of a
constant built in the module.

## `execute()`

```python
def execute(node: chunk.Chunk) -> None:
    ...
```

`execute` is a **reserved name** at module level. Don't use it for a normal callback, or
it will be called every time the chunk cooks.

If a module has `execute(node)`, it runs every time the chunk cooks: when something it
depends on changes (`add_dependency`), when the chunk is loaded from a file, or when the
exec chain is run again. On load, param *values* are restored, but callbacks don't run
again (see below). `node` is the chunk itself, the same object `main()` returned. So
`execute` usually calls `node.get_stage()` or `node.get_edit_context()` and then changes
the stage.

It runs with a `Usd.EditContext` already set to the chunk's own layer (unless the chunk is
`interactive`, which writes somewhere else). The framework clears that layer right before
each `execute()`, so write the whole result every time. It must give the same result every
time it runs with the same inputs. It can run many times while the graph cooks, so keep it
fast.

You write `execute`; you never call it. The framework does, when the graph reaches the
chunk: changing a param or a wire marks what depends on it dirty, and each dirty chunk
cooks. If the module knows its result is out of date in a way no param change says --
say, it restored params its first cook could not see -- call `node.recook()` to cook again.

A common pattern is a chunk that does its real work in a param callback, with `execute`
only there to redo that work after the file is opened. This is why `execute` exists,
instead of doing everything in callbacks:

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

(the full version is [`chunks/transforms/reset_transform.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/transforms/reset_transform.py)).
A save only stores a chunk's **data** (params, values and wiring), never the code that
ran (see [*What a save keeps*](runtime-chunks.md#what-a-save-keeps)). `execute` is what
makes a chunk do its work again after the file is opened.

### Caching a slow cook

Before caching a slow cook, check whether it needs to be slow. Authoring thousands of prims
through the Usd API, or looping over points in Python, is usually 10× or more slower than it
has to be. See [Making a chunk fast](fast-chunks.md).

Because a cook with the same inputs always gives the same layer, a slow one doesn't have to
run again. Set `cache_cook=True` in `config()` and the result is kept on disk and read back
the next time the inputs match, including after the file is closed and opened again:

```python
def config():
    return chunk.DefaultConfig(cache_cook=True)
```

The cache compares everything it can see:

- every param's value (an expression counts as the value it resolves to), except labels
  and OUTPUT params, which the cook writes;
- the size and modification time of any file a param names;
- the module's own code and the files next to it;
- the stage's up axis and `metersPerUnit`.

If the result also depends on something that is not a param, for example geometry read
from an upstream chunk or files named inside another file, add a `cache_key(node)`
function that returns it. Anything that can be `repr()`ed works. Return `None` when it
can't be worked out, and the chunk cooks as usual:

```python
def cache_key(node):
    return [mesh_checksum(prim) for prim in input_meshes(node)]
```

Poly Reduce returns a checksum of the meshes it reads, and URDF Import returns the size and
date of each mesh file the `.urdf` names.

Some results are not made in a cook: a simulation bakes when you press Run, on a worker
thread. To cache one, take the entry when the work starts and store the layer when it is
done. The next cook with the same inputs reads it back, also after the file is opened
again. Taking the entry first means a param changed during the bake does not relabel it:

```python
def cache_key(node):
    return sim_bake.input_key(node)  # None in ordinary cooks: no automatic caching

entry = sim_bake.cache_entry(node, sim_bake.digest(job))  # None when caching is off
if entry is not None and entry.restore(node, layer):
    ...                               # read back: no bake
...                                   # else bake into the chunk's layer, then
if entry is not None:
    entry.store(node, layer)
```

`cache_key` answers only while `cache_entry` asks, because working out a bake's inputs can
take seconds, too slow for every cook. The RBD Solver and the Particle Solver do this.

Each chunk of such a module gets a **Cache Result** checkbox, on by default, to switch
caching off for that one node. Only cooks that take longer than about 50 ms are stored,
since reading a result back takes time too. A cook that changes its own input params, or
adds params, is never stored. For where the cache lives and how to switch it off, see
[`USD_CHUNKS_COOK_CACHE`](install.md#environment-variables).

### Long bakes: `sim_bake`

A simulation that takes seconds or minutes must not stop the host. Write the bake as a
generator and hand it to `sim_bake.run`. Read the scene first, then `yield sim_bake.WORKER`:
what follows runs on a worker thread, so it must not touch USD. `yield sim_bake.MAIN` comes
back to the main thread to write the result:

```python
from usd_chunks import sim_bake

def bake(ctx):
    job = read_inputs(stage)          # main thread
    yield sim_bake.WORKER
    result = solve(job, progress=ctx.progress, cancelled=ctx.cancelled)
    yield sim_bake.MAIN
    write_result(stage, result)       # main thread
    return result

sim_bake.run(node, bake, report=show_in_readout, finish=when_done)
```

Headless and under test there is no event loop to come back to, so the bake runs straight
through. `sim_bake.cancel(node)` stops it.

Some engines hold Python's GIL while they compute (Taichi does), which stops the UI even from
a thread. Call the solve through `sim_bake.in_subprocess(ctx, solve, **job)` to run it in a
separate Python process. `promote`, `keep` and `restore` handle the bake's layer: put it in
front of the layers it animates, and put it back after the next cook clears it.

### Skipping cooks nothing reached

*Experimental, off by default: set `USD_CHUNKS_SKIP_COOKS=1`.*

A change to a chunk dirties everything below it, but most of those chunks read nothing that
changed: a Scatter below a Light doesn't care that the Light turned. With skipping on, a
chunk that only the chunks above it changed doesn't cook unless one of them changed a prim
it reads.

What a chunk reads is not declared anywhere. During a cook, `node.get_stage()` returns a
stage that only holds the chunk's **inputs** (the prims its prim path params name, such as
PRIM_SELECTION params and `primpath_location`) and the prims its last cook wrote. So:

- Read the prims you need through a prim path param, not a path written into the code. A
  chunk that looks up anything else, or walks the whole stage (`Traverse()`), is noticed,
  cooked again on the whole stage, and never skipped from then on. It still works; it
  just always cooks.
- Framework work on the scene, like muting a gate's branches, goes through
  `node.get_scene_stage()`, which is always the whole scene. Module code shouldn't need it.

Anything the framework can't account for makes the chunks below cook, as they always have:
a rewire, a chunk disabled above, a change to which layers are muted or to the stage's
expression variables, an Edit Target repointed.

## `commit()`

```python
def commit(node: chunk.Chunk) -> None:
    ...
```

`commit` is for a chunk whose job is writing a *file*, not writing to its own USD layer,
for example Export USD or a render product chunk. It's separate from `execute` because they
run at different times:

- `execute` runs many times while the graph cooks and params change. It's the right place
  for fast USD edits, and the wrong place for writing to disk.
- `commit` runs once, **after** everything has finished cooking. `usdchunks run` calls it,
  and so does `Scene.commit()` from Python. usdview doesn't call it on its own; a chunk like
  Export USD has a button for that. Chunks commit in graph order, so a chunk that reads
  another chunk's output sees the finished result.

No edit context is set up for `commit`. It reads the final composed stage and writes to
disk. Modules without `commit` are skipped, since most chunks have nothing to write. A
disabled chunk, or one that isn't connected to the active [view](core-chunks.md#view),
never commits.

## Discovery, recap

Every folder in `USD_CHUNK_DIRS`, including subfolders, is searched for `.py` files.
Any file with a top-level `main()` is registered under its file name. There are
no other rules for folder layout or file names. [Environment
variables](install.md#environment-variables) explains which one wins when two folders have
a chunk with the same name.
