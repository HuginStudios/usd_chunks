# usd_chunks

![image](docs/assets/chunks_view.png)

## Intro

usd_chunks is a simple library for interfacing with USD. The aim is to lower the bar for users interested in working with USD. The target was primarily to extend [usdview](https://openusd.org/release/toolset.html#usdview) by providing UI elements in order to modify the stage interactively. This target has been extended to include USD as a whole, meaning one can use usd_chunks to interface with USD regardless of the application.

When creating plugins for usdview natively:
Each plugin need to define some boilerplate code as well as define a plugInfo.json in order to be recognized by usdview. The plugin author can write some Qt code for the UI. Although fairly simple, when creating and managing many plugins, this quickly becomes tedious. The plugin as well as the UI is also not application-agnostic, meaning the same code need to be rewritten for all supported applications. usd_chunks exists to solve this.

usd_chunks is a single plugin for USD that can load 'Chunks'. A Chunk is created using python and registered with the usd_chunks plugin, and optionally represented graphically. Without writing Qt and without registering the plugin with USD. The Chunk can in turn modify the stage however one likes, such as setting attributes, creating prims, import and export of files etc.

## Chunks
A 'Chunk' is primarily a container of parameters, groups and attributes. All data encapsulated in a chunk can be modified freely. Chunks, parameters and groups are similar and can be used similarly. Parameters hold values and are ultimately the things we can interact with. Any data modified on a chunk object will send a relevant notice for users to listen for through callbacks. Callbacks are python functions, commonly for modifying the stage. Any changes to the stage through a callback will be encapsulated in a relevant UsdLayer, as to not clutter irrelevant layers. As chunks are dynamic, callbacks can also be used to modify the chunks at runtime by adding / removing params, or otherise change the behaviour of the Chunk.

### Example
A simple Chunk module providing a button, which when pressed creates a cube prim. By default, the chunks are put in a dockable widget to the right of the viewport in usdview.

``` py
from usd_chunks import chunk
from pxr import Usd


def addCube(owner, notice, sender):
    # A simple callback, adding a cube to the stage. If the /cube prim exists, create a new one with unique name
    stage = owner.get_stage()
    count = 0
    while True:
        prim = stage.GetPrimAtPath(f"/cube{count}")
        if not prim.IsValid():
            prim = stage.DefinePrim(f"/cube{count}", "Cube")
            break
        count = count + 1


def main():
    # This function should return the chunk. In this case, the chunk contains a button parameter with a callback
    button = chunk.Button("add_cube", "Add Cube")
    button.add_callback(addCube)

    chunk_api = chunk.Chunk("Add Cubes")
    chunk_api.add_param(button)

    return chunk_api


def config():
    return chunk.DefaulConfig(category=chunk.StandardCategory.EXAMPLES)

```

![image](docs/assets/example_add_cube.png)

See more examples and use cases in [scripts](/scripts/)

A Chunk is defined in a python module like above and should be returned from a **main()** function of the same module. Similarly, a **config()** function may return a configuration of the chunk module. This module is found from the **USD_CHUNK_DIRS** environment variable, where .py files are scanned for a **main()** function returning a Chunk. 

## Params

Params are the leaves of a chunk — they hold the values you interact with. Each one maps
to a widget in the UI and, when its value differs from the default, to an `inputs:` attribute
on the chunk's prim.

| Param | Value | Notes |
| --- | --- | --- |
| `Slider` | `float` \| `int` | `min_value`, `max_value`, `is_int=True` for integers |
| `Menu` | `str` | `menu_items` is the list of choices |
| `Checkbox` | `bool` | |
| `LineEdit` | `str` | Single line |
| `TextEdit` | `str` | Multi-line; `rows` sets the height |
| `FileBrowser` | `str` \| `Sdf.AssetPath` | `extensions` and `pattern` filter the dialog |
| `ColorPicker` | `(r, g, b)` | Components in the 0–1 range |
| `Button` | — | Holds no value; use it with a callback |
| `Label` | `str` | Display only, not editable |
| `Separator` | — | Visual grouping only |
| `Custom` | any | For a custom widget |

Every param takes a name, and optionally a label:

``` py
divisions = chunk.Slider("divisions", "Divisions", min_value=1, max_value=32, is_int=True)
mode = chunk.Menu("mode", menu_items=["linear", "smooth"])
```

Read and write a value through `.value`:

``` py
divisions.value = 8
```

Prefer `Slider` over `Spinbox` for numeric values — `Slider` covers the integer case with
`is_int=True`, and is the type the rest of the toolkit is built and tested against.

### Param flags

`ChunkFlags.ParamFlags` adjusts how a param behaves:

- `OUTPUT` — a computed result. Hidden from the UI and not user-editable, but still
  readable by other chunks as `@ChunkName.param@`.
- `LABELED` — draw a label next to the widget.
- `FILE_EXISTING`, `DIRECTORY` — for `FileBrowser`: restrict to existing files, or to folders.
- `PRIM_SELECTION`, `PROP_SELECTION` — for `LineEdit`: the value is a prim or property path,
  so the UI can offer selection.
- `PY_SYNTAX_HIGHLIGHT`, `USD_SYNTAX_HIGHLIGHT` — for `TextEdit`.

`ChunkFlags.State` carries the current state instead: `HIDDEN`, `DISABLED`, `COLLAPSED`,
`SELECTED`, and `STALE` (a UI hint that a result no longer reflects its inputs — nothing
sets or clears it automatically).

## Groups

Groups hold params and other groups, giving a chunk its structure. A `Chunk` is itself a
container, so the same methods — `add_param`, `add_params`, `get_param`, `get_my_params` —
work at every level.

- `Group` — a plain container.
- `ListGroup` — a container whose children can be added and removed at runtime, for
  variable-length input.
- `Vector` — a fixed set of params treated as one value, for things like an XYZ triple.
  Expressions can reference the whole vector as `@Node.vecParam@`.

``` py
transform = chunk.Group("transform", "Transform")
transform.add_params([
    chunk.Slider("tx", min_value=-100, max_value=100),
    chunk.Slider("ty", min_value=-100, max_value=100),
])
chunk_api.add_param(transform)
```

`ChunkFlags.GroupFlags` controls presentation: `LABELED`, `COLLAPSABLE`, `FRAMED`, `LIST`,
and `CONTEXT`. `CONTEXT` is the notable one — a context group renders as a workspace holding
other chunks rather than as a node itself. In the Qt plugin that is a tab.

## How it works

**Discovery.** `USD_CHUNK_DIRS` is scanned recursively for `.py` files. Any module with a
`main()` function is registered as a chunk module. `main()` takes no arguments and returns a
`Chunk`. An optional `config()` returns a `DefaulConfig`, which sets the category the chunk
is filed under (`StandardCategory.MODELLING`, `UTILS`, `RIGS`, `PROPERTIES`, `USER`,
`EXAMPLES`) and whether it loads automatically. Chunks installed by a store are picked up the
same way from `GLOBAL_USDCHUNKS`, appended after `USD_CHUNK_DIRS` so your own chunks win a
name clash.

**Notices and callbacks.** Changing anything on a chunk object sends a notice. Subscribe with
`add_callback`, and the callback runs when that notice fires. This is what makes chunks
dynamic: a callback can add or remove params, change flags, or author to the stage.

``` py
def on_mode_changed(notice, sender):
    ...

mode.add_callback(on_mode_changed)
```

A callback may declare either of two signatures, and gets whichever it asks for:

- `(notice, sender)` — matches USD's `Tf.Notice`. This is what a method on a `Chunk`
  subclass receives, where `self` is already the chunk. Plain functions may declare it too.
- `(owner, notice, sender)` — the convenience form for module-level functions, where `owner`
  is the object `add_callback` was called on.

`owner` and `sender` are not the same thing. `owner` is the object you registered on;
`sender` is whatever dispatched the notice, which may differ when you pass an explicit
`sender=`, listen globally, or receive from a proxy.

**Execution.** Chunks wire together through `inputs:chunk` / `outputs:chunk` connections on
their prims, forming a DAG. When something changes, the affected chunks are re-run in
topological order by calling the module's `execute(node)` function — the counterpart to
`main()`, and where a chunk does its real work against the stage.

**Committing.** A chunk whose real work is writing a file defines `commit(node)` instead of,
or alongside, `execute(node)`. Where `execute` is a cook — cheap, idempotent, re-run
constantly while the DAG is being solved — `commit` runs once, in topological order, after
the solve has settled, and gets no edit context: it reads the composed stage and writes to
disk. Disabled chunks never commit, and a module without the hook is simply skipped.
`usdchunks run` drives it, so a network containing an Export USD chunk writes its configured
output headlessly, exactly as pressing Export does in the UI.

**Layers.** Authoring is split so chunk bookkeeping never pollutes your scene. The chunk prims
and their connections live in the session layer. The prims and values a chunk actually
produces go to its own layer, which is cleared and regenerated each time it runs — that is
what makes a procedural chunk idempotent. A chunk marked interactive writes to a file layer
instead, so its edits persist. `usdchunks run` follows exactly the same path headlessly.

## Reliance on attrs
We use [python-attrs](https://www.attrs.org/en/stable) extensively to get dataclass-like behaviour for all the chunk classes. The reasoning being that managing of chunk attributes become much easier, without having to define \_\_init\_\_ functions with heavy signatures. Chunk classes has many attributes, and thus this would fill the class definitions with unnecessary clutter. We also get attribute validation and conversion for free using attrs. Our base classes defines some standard attributes, and each higher lever of the inheritance hierarchy defines more specific attributes. All of these attributes are combined dynamically by attrs, creating the final class with a genertated \_\_init\_\_ function. 

This means we trade off some readability for cleaner code and easier maintanence, as the classes we interface with do not have explicit \_\_init\_\_ functions. We therefore rely on static type checking (using pylance) for hints on the signatures.

Most of the attrs interfacing has been abstracted away execpt for using attrs.field() for class attributes. Users do not need to think about this unless creating custom parameters or otherwise extending the chunk_core module.

## Setup

### Install

```
pip install usd-chunks
```

This pulls in `usd-core` and gives you the library plus the `usdchunks` command, with
no USD build required:

```
usdchunks run my_setup.usda
```

Note that the `usd-core` wheels on PyPI do not include usdview — it is not part of that
build. Using the usdview integration needs a full USD build that provides
`pxr.Usdviewq`; see below.

### Environment variables

**USD_CHUNK_DIRS** - Add your chunk directory to this envar. This can be a list of directories, separated by ; on windows, and : on unix systems. Nested directories are scanned for chunks as well. Chunks installed by a store are picked up automatically from **GLOBAL_USDCHUNKS** (default `~/.usdchunks/global`), which is appended after this.

**PXR_PLUGINPATH_NAME** - Only needed for the usdview integration. Point it at the installed plugin directory, which `usdchunks` will print for you:

```
usdchunks plugin-path
```

**PYTHONPATH** - Only needed when running from a clone rather than an install; add this repo's location so usdview can find the python modules.

## What opening a network can run

A `.usda` written by usd_chunks carries param values and wiring, not code. Opening one runs
**your** chunk modules — the `.py` files already on your `USD_CHUNK_DIRS` — selected by the
`info:chunkType` recorded on each chunk prim. A network from an untrusted source can name a
chunk you have and set its params; it cannot introduce a module you do not.

That still means a network decides what your chunks do, and some of them write files (Export
USD, the render chunks). Treat an unfamiliar network the way you would an unfamiliar project
file: open it, look at it, then run it.

Two things that would otherwise be surprising:

- **The Script Runner chunk executes the Python in its `code` param only when you press its
  Run button.** It has no `execute()`, so loading or cooking a network never runs it.
- **Chunk dependencies are never installed silently.** A chunk module may declare pip
  requirements, and nothing reaches your environment without an explicit confirmation.

## Build documentation

The reference pages are generated from the source, so usd_chunks has to be importable —
mkdocstrings and `griffe_chunks.py` introspect the modules rather than reading a static
copy. Install the docs extra and build:

```
pip install -e ".[docs]"
USD_CHUNK_DIRS=scripts mkdocs serve
```

That serves the site locally; `mkdocs build` writes it to `site/` instead. The docs are not
built in CI — publishing is a deliberate act, and the publish below builds the site anyway,
so that is where a breakage surfaces.

On Windows, `build_docs.bat` wraps both. With no arguments it serves locally; with
`--share` it builds and copies the site to a destination of your choosing — a UNC share, a
mapped drive, or a web root:

```
build_docs.bat --share \\server\share\usd_chunks_docs
build_docs.bat --share E:\www\usd_chunks --directory-urls
```

It adds and overwrites, never deletes. Pass `--mirror` to also remove files that are no
longer part of the site; that is refused unless the destination is empty or was published
by this script before, and publishing into anything that looks like a source checkout is
refused outright.

By default it builds flat filenames (`example.html`), which work both over `file://` and
over http. A browser opening the site from a share will not resolve a directory to its
`index.html`, so the default `/example/` style URLs would break navigation there. Once the
destination is definitely behind a web server such as nginx, pass `--directory-urls` for
the tidier form.

Either way the search box only works when the site is served over http, since the search
index is fetched by XHR and browsers block that for `file://` origins. Everything else
works from a plain fileshare.

Run `docs/gen_ref_nav.py` to regenerate the `docs/reference` markdown files.

## License

Apache License 2.0 — see [LICENSE](LICENSE). This is the same license OpenUSD itself
ships under, so usd_chunks is compatible with the ecosystem it plugs into.
